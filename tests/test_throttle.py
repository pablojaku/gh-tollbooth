"""Throttle tests: one turn (check, wait_turn, attempt) and the rules it applies: the lock, call
spacing, shared blocks and budgets. Shared-file races are in test_shared_files.

Mock-first: the Throttle is given a FakeClock, so waits finish instantly.
Run: python3 -m unittest discover -s tests -v
"""
import contextlib
import fcntl
import io
import json
import shutil
import unittest

from _fixtures import ThrottleCase, hold_lock_elsewhere, lock_is_free, recorded
import tollbooth_core as core  # noqa: E402  # pyright: ignore[reportMissingImports]
import tollbooth_throttle as throttling  # noqa: E402  # pyright: ignore[reportMissingImports]


class Lock(ThrottleCase):
    def test_the_request_is_sent_holding_the_lock(self):
        seen = []
        self.turn(send=lambda left: (seen.append(lock_is_free(self.throttle)), 200, {}))
        self.assertEqual(seen, [False])
        self.assertTrue(lock_is_free(self.throttle))

    def test_released_when_the_request_fails(self):
        def boom(left):
            raise RuntimeError("boom")
        with self.assertRaises(RuntimeError):
            self.turn(send=boom)
        self.assertTrue(lock_is_free(self.throttle))

    def test_waiting_gives_up_busy_at_the_deadline(self):
        hold_lock_elsewhere(self, self.throttle)
        start = self.clock.mono()
        with self.assertRaises(core.TollboothError) as cm:
            self.other_agent().attempt("lab", None, "core", start + 3, "GET x", lambda left: self.fail("sent"))
        self.assertEqual(cm.exception.code, core.EX_BUSY)
        self.assertAlmostEqual(self.clock.mono() - start, 3, delta=0.25)

    def test_a_wall_clock_step_does_not_move_the_deadline(self):
        hold_lock_elsewhere(self, self.throttle)

        def stepping(s):
            self.clock.wall -= 600  # the wall clock jumps back on every poll
        self.clock.on_sleep = stepping
        start = self.clock.mono()
        with self.assertRaises(core.TollboothError):
            self.throttle.attempt("lab", None, "core", start + 3, "GET x", lambda left: self.fail("sent"))
        self.assertLess(self.clock.mono() - start, 4)

    def test_waiter_gets_in_once_the_lock_is_released_and_says_it_waited(self):
        holder = hold_lock_elsewhere(self, self.throttle)

        def release_later(s):
            if self.clock.mono() - start > 6:
                fcntl.flock(holder, fcntl.LOCK_UN)
        start = self.clock.mono()
        self.clock.on_sleep = release_later
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(self.turn(), "answer")
        self.assertEqual(err.getvalue().count("waiting for another agent"), 1)

    def test_a_waiter_follows_the_lock_file_when_it_is_replaced(self):
        # Someone deletes the lock file while we wait on it, and a third agent locks the new one.
        # Getting the old, deleted file's lock must not count as holding the lock.
        old = hold_lock_elsewhere(self, self.throttle)
        third, steps = [], []

        def meanwhile(s):
            if not third:
                self.throttle.lock_path.unlink()
                third.append(open(self.throttle.lock_path, "a+"))
                self.addCleanup(third[0].close)
                fcntl.flock(third[0], fcntl.LOCK_EX)
                fcntl.flock(old, fcntl.LOCK_UN)
                steps.append("old lock released")
            elif len(steps) == 1 and self.clock.mono() - start > 2:
                fcntl.flock(third[0], fcntl.LOCK_UN)
                steps.append("new lock released")
        start = self.clock.mono()
        self.clock.on_sleep = meanwhile
        self.turn(send=lambda left: (steps.append("got the lock"), 200, {}))
        self.assertEqual(steps, ["old lock released", "new lock released", "got the lock"])

    def test_works_after_the_state_folder_is_deleted(self):
        self.turn()
        shutil.rmtree(self.throttle.dir)
        self.turn()
        self.assertEqual((self.throttle.used("lab"), len(self.throttle.entries())), (1, 1))


class Check(ThrottleCase):
    def test_a_used_up_budget_fails_before_any_work(self):
        self.turn(label="lab")
        self.refused(core.EX_BUDGET, self.throttle.check, "lab", 1, "core")
        self.throttle.check("lab", 2, "core")
        self.throttle.check("lab", None, "core")  # no budget, no limit
        self.throttle.check("other", 1, "core")   # budgets are per label

    def test_a_block_longer_than_max_wait_fails_at_once(self):
        recorded(self.throttle, "search", {"search": throttling.MAX_WAIT + 1})
        self.refused(core.EX_LIMITED, self.throttle.check, "lab", None, "search")
        self.throttle.check("lab", None, "core")  # other buckets are free
        self.assertEqual(self.clock.sleeps, [])


class WaitTurn(ThrottleCase):
    def test_a_short_block_is_slept_through(self):
        recorded(self.throttle, "graphql", {"graphql": 20})
        self.throttle.wait_turn("graphql", self.clock.mono() + 100)
        self.assertEqual(self.clock.sleeps[0], 20)

    def test_a_block_too_long_or_past_the_deadline_fails_without_sleeping(self):
        recorded(self.throttle, "graphql", {"graphql": throttling.MAX_WAIT + 5})
        self.refused(core.EX_LIMITED, self.throttle.wait_turn, "graphql", self.clock.mono() + 1000)
        recorded(self.throttle, "core", {"core": 50})
        self.refused(core.EX_LIMITED, self.throttle.wait_turn, "core", self.clock.mono() + 30)
        self.assertEqual(self.clock.sleeps, [])

    def test_a_gap_that_leaves_no_time_for_a_request_fails_busy(self):
        recorded(self.throttle, "code_search")
        self.refused(core.EX_BUSY, self.throttle.wait_turn, "code_search", self.clock.mono() + 10)
        self.assertEqual(self.clock.sleeps, [])


class Attempt(ThrottleCase):
    def test_the_call_is_charged_and_logged(self):
        self.assertEqual(self.turn(status=201, summary="GET repos/o/r"), "answer")
        self.assertEqual(self.throttle.used("lab"), 1)
        (e,) = self.throttle.entries()
        self.assertEqual((e[1], e[3], e[4], e[6]), ("lab", "core", "201", "GET repos/o/r"))

    def test_the_call_is_charged_before_the_request_is_sent(self):
        used = []
        self.turn(send=lambda left: (used.append(self.throttle.used("lab")), 200, {}))
        self.assertEqual(used, [1])

    def test_a_request_that_raises_is_still_charged_spaced_and_logged(self):
        def no_answer(left):
            raise OSError("no answer")
        with self.assertRaises(OSError):
            self.turn(send=no_answer)
        self.assertEqual((self.throttle.used("lab"), self.throttle.entries()[0][4]), (1, "0"))
        self.throttle.wait_turn("core", self.clock.mono() + 100)
        self.assertEqual(self.clock.sleeps, [core.SPACING["core"]])

    def test_the_request_gets_the_time_left(self):
        lefts = []
        self.throttle.attempt("lab", None, "core", self.clock.mono() + 42, "GET x",
                              lambda left: (lefts.append(left), 200, {}))
        self.assertEqual(lefts, [42])

    def test_none_when_the_bucket_was_blocked_while_waiting_for_the_lock(self):
        recorded(self.other_agent(), "core", {"core": 10})
        self.assertIsNone(self.turn(send=lambda left: self.fail("sent")))
        self.assertEqual((self.throttle.used("lab"), self.throttle.entries()), (0, []))

    def test_none_when_a_long_gap_appeared_while_waiting_for_the_lock(self):
        recorded(self.other_agent(), "code_search")
        self.assertIsNone(self.turn(bucket="code_search", send=lambda left: self.fail("sent")))
        self.assertEqual((self.throttle.used("lab"), self.clock.sleeps), (0, []))

    def test_a_short_gap_is_slept_under_the_lock(self):
        recorded(self.other_agent(), "core")
        self.assertEqual(self.turn(), "answer")
        self.assertEqual(self.clock.sleeps, [core.SPACING["core"]])

    def test_the_budget_is_checked_again_under_the_lock(self):
        # another agent with the same label spent the budget after this one's check()
        self.throttle.check("lab", 1, "core")
        self.turn(throttle=self.other_agent(), label="lab")
        self.refused(core.EX_BUDGET, self.turn, None, "lab", "core", 200, None, 1)
        self.assertEqual(self.throttle.used("lab"), 1)


class Spacing(ThrottleCase):
    def test_the_gap_is_what_is_left_of_the_buckets_spacing(self):
        self.throttle.wait_turn("search", self.clock.mono() + 100)
        self.assertEqual(self.clock.sleeps, [])  # no call yet
        self.turn(bucket="search")
        self.other_agent().wait_turn("search", self.clock.mono() + 100)
        self.assertAlmostEqual(self.clock.sleeps.pop(), core.SPACING["search"])
        self.turn(bucket="search")
        self.clock.wall += 1.0
        self.throttle.wait_turn("search", self.clock.mono() + 100)
        self.assertAlmostEqual(self.clock.sleeps.pop(), core.SPACING["search"] - 1.0)
        self.turn(bucket="search")
        self.clock.wall += 5.0
        self.throttle.wait_turn("search", self.clock.mono() + 100)
        self.assertEqual(self.clock.sleeps, [])

    def test_buckets_are_spaced_separately(self):
        self.turn(bucket="code_search")
        self.throttle.wait_turn("core", self.clock.mono() + 100)
        self.assertEqual(self.clock.sleeps, [])

    def test_a_last_call_time_in_the_future_waits_nothing(self):
        # written before a backward clock step
        self.throttle.state_path.write_text(json.dumps({"core": self.clock.now() + 25}))
        self.throttle.wait_turn("core", self.clock.mono() + 100)
        self.assertEqual(self.clock.sleeps, [])


class Blocks(ThrottleCase):
    def test_a_block_is_seen_by_every_agent(self):
        self.assertLessEqual(self.throttle.blocked_for("core"), 0)
        self.turn(bucket="graphql", status=429, blocks={b: 30 for b in core.SPACING})
        for b in core.SPACING:
            self.assertAlmostEqual(self.other_agent().blocked_for(b), 30, delta=0.01)
        self.clock.sleep(31)
        self.assertLessEqual(self.throttle.blocked_for("core"), 0)

    def test_a_shorter_block_never_shortens_a_longer_one(self):
        self.turn(bucket="core", status=403, blocks={"core": 60})
        self.turn(throttle=self.other_agent(), bucket="graphql", status=403, blocks={"core": 10})
        self.assertAlmostEqual(self.throttle.blocked_for("core"), 60, delta=0.01)

    def test_a_block_longer_than_an_hour_is_capped_not_dropped(self):
        self.turn(status=403, blocks={"core": 7200})  # e.g. Retry-After: 7200
        self.assertAlmostEqual(self.other_agent().blocked_for("core"), core.BLOCK_SANITY, delta=0.01)
        self.clock.sleep(core.BLOCK_SANITY + 1)
        self.assertLessEqual(self.throttle.blocked_for("core"), 0)

    def test_a_far_future_block_in_the_file_is_ignored_and_overwritten(self):
        # left by a clock step or a reboot: GitHub limits reset hourly, so it can't be real
        self.throttle.state_path.write_text(json.dumps({"blocked:core": self.clock.now() + 7200}))
        self.assertEqual(self.throttle.blocked_for("core"), 0.0)
        self.turn(status=403, blocks={"core": 60})
        self.assertAlmostEqual(self.throttle.blocked_for("core"), 60, delta=0.01)


class Budgets(ThrottleCase):
    def test_charges_are_counted_per_label(self):
        for _ in range(3):
            self.turn(label="lab")
        self.turn(throttle=self.other_agent(), label="other")
        self.assertEqual((self.throttle.used("lab"), self.throttle.used("other"), self.throttle.used("new")), (3, 1, 0))

    def test_charges_age_out_after_a_day(self):
        self.turn(label="lab")
        self.clock.wall += 23 * 3600
        self.assertEqual(self.throttle.used("lab"), 1)
        self.clock.wall += 3 * 3600
        self.assertEqual(self.throttle.used("lab"), 0)
        self.turn(label="other")  # the next charge drops the aged-out label from the file
        self.assertNotIn("lab", self.throttle.state()["charges"])

    def test_a_charge_in_the_future_is_not_counted(self):
        # left by a clock that ran ahead: without an upper limit it would count against the label for a year
        self.clock.wall += 365 * 24 * 3600
        self.turn(label="lab")
        self.clock.wall -= 365 * 24 * 3600
        self.assertEqual(self.throttle.used("lab"), 0)
        self.turn(label="other")  # the next charge drops it from the file
        self.assertNotIn("lab", self.throttle.state()["charges"])
        self.turn(label="lab")
        self.clock.wall -= 1800  # a small step back, into the hour before, keeps a charge just made
        self.assertEqual(self.throttle.used("lab"), 1)

    def test_budgets_do_not_depend_on_the_log(self):
        self.turn(label="lab")
        self.throttle.log_path.write_text("")
        self.assertEqual(self.throttle.used("lab"), 1)


if __name__ == "__main__":
    unittest.main()
