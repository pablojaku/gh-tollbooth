"""Turn tests: one call's time budget (Deadline) and one turn through the throttle (Throttle.turn).

Mock-first: a FakeClock stands in for both clocks, so no test waits.
Run: uv run pytest
"""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import tollbooth_core as core
import tollbooth_throttle as throttling
from _fixtures import FakeClock
from filelock import FileLock, Timeout


class ClockCase(unittest.TestCase):
    """A FakeClock, and settings fixed at a 100 s deadline and a 60 s max wait."""

    def setUp(self):
        self.clock = FakeClock()
        p = mock.patch.object(core, "SETTINGS", core.Settings(deadline=100, max_wait=60))
        p.start()
        self.addCleanup(p.stop)


class Deadline(ClockCase):
    def test_left_counts_down_on_the_monotonic_clock(self):
        d = throttling.Deadline(self.clock)
        self.assertEqual(d.left(), 100)
        self.clock.wall -= 3600  # a wall-clock step changes nothing
        self.clock.sleep(30)
        self.assertEqual(d.left(), 70)

    def test_a_wait_fits_only_if_a_request_can_still_start_after_it(self):
        d = throttling.Deadline(self.clock)
        self.assertTrue(d.fits(85))  # 15 s are left for the request
        self.assertFalse(d.fits(85.1))
        short = throttling.Deadline(self.clock, seconds=20)
        self.assertTrue(short.fits(10))  # half of a short deadline is kept for the request
        self.assertFalse(short.fits(10.1))

    def test_a_wait_that_fits_is_slept_and_a_limit_wait_says_so(self):
        d = throttling.Deadline(self.clock)
        with self.assertNoLogs("gh-tollbooth"):
            d.wait(2.5)  # a call gap
        with self.assertLogs("gh-tollbooth", "INFO") as logs:
            d.wait(20, limit="search")
        self.assertEqual(self.clock.sleeps, [2.5, 20])
        self.assertEqual(logs.output, ["INFO:gh-tollbooth:search is rate-limited; waiting 20 s (lock released)"])

    def test_a_call_gap_that_does_not_fit_is_busy(self):
        d = throttling.Deadline(self.clock)
        self.clock.sleep(90)
        with self.assertRaises(core.TollboothError) as cm:
            d.wait(2.5)
        self.assertEqual(cm.exception.code, core.EX_BUSY)
        self.assertEqual(self.clock.sleeps, [90])  # nothing more was slept

    def test_a_limit_longer_than_max_wait_is_exit_75_at_once(self):
        with self.assertRaises(core.TollboothError) as cm:
            throttling.Deadline(self.clock).wait(61, limit="search")
        self.assertEqual(cm.exception.code, core.EX_LIMITED)
        self.assertIn("search is rate-limited until", str(cm.exception))
        self.assertEqual(self.clock.sleeps, [])

    def test_a_limit_that_outlasts_the_deadline_is_exit_75_not_76(self):
        # it is a rate limit that doesn't fit, so the agent must stop, not retry
        d = throttling.Deadline(self.clock)
        self.clock.sleep(60)
        with self.assertRaises(core.TollboothError) as cm:
            d.wait(30, limit="core")
        self.assertEqual(cm.exception.code, core.EX_LIMITED)
        d.wait(25, limit="core")  # this one fits: 15 s are left after it
        self.assertEqual(self.clock.sleeps, [60, 25])


class TurnCase(ClockCase):
    """A Throttle on a temporary state folder, with the fake clock; `sent` records each send's time left."""

    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name) / "state"
        self.throttle = throttling.Throttle(self.dir, self.clock)
        self.sent = []

    def other_agent(self):
        return throttling.Throttle(self.dir, self.clock)

    def send(self, left):
        self.sent.append(round(left, 3))
        return "answer", 200, None

    def turn(self, bucket="core", budget=None, deadline=None, send=None):
        deadline = deadline or throttling.Deadline(self.clock)
        return self.throttle.turn("lab", budget, bucket, deadline, "GET x", send or self.send)

    def lock_is_free(self):
        probe = FileLock(self.throttle.lock_path, thread_local=False)
        try:
            probe.acquire(blocking=False)
        except Timeout:
            return False
        probe.release()
        return True


class Turn(TurnCase):
    def test_one_turn_sends_once_holding_the_lock_and_records_it(self):
        held = []

        def send(left):
            held.append(not self.lock_is_free())
            return "answer", 200, None
        self.assertEqual(self.turn(send=send), "answer")
        self.assertEqual(held, [True])
        self.assertTrue(self.lock_is_free())
        (call,) = self.throttle.entries()
        self.assertEqual((call.label, call.bucket, call.status, call.summary), ("lab", "core", 200, "GET x"))
        self.assertEqual(self.throttle.used("lab"), 1)
        self.assertAlmostEqual(self.throttle.spacing_gap("core"), throttling.SPACING["core"])

    def test_the_call_gap_is_waited_before_the_next_request(self):
        self.turn(bucket="search")
        self.turn(bucket="search")
        self.assertEqual(self.clock.sleeps, [throttling.SPACING["search"]])

    def test_send_is_given_the_time_left(self):
        deadline = throttling.Deadline(self.clock)
        self.clock.sleep(30)
        self.turn(deadline=deadline)
        self.assertEqual(self.sent, [70])

    def test_a_failed_send_is_still_charged_spaced_and_logged_with_status_0(self):
        def no_answer(left):
            raise RuntimeError("no answer")
        with self.assertRaises(RuntimeError):
            self.turn(send=no_answer)
        self.assertEqual((self.throttle.used("lab"), [c.status for c in self.throttle.entries()]), (1, [0]))
        self.assertGreater(self.throttle.spacing_gap("core"), 0)
        self.assertTrue(self.lock_is_free())


class Checks(TurnCase):
    def test_a_short_block_is_waited_out_without_the_lock(self):
        self.other_agent()._mark("core", {"core": self.clock.now() + 20}, spaced=False)
        with self.assertLogs("gh-tollbooth", "INFO"):
            self.turn()
        self.assertEqual((self.clock.sleeps, len(self.sent)), ([20], 1))

    def test_a_block_longer_than_max_wait_is_exit_75_before_anything(self):
        self.other_agent()._mark("core", {"core": self.clock.now() + 120}, spaced=False)
        with self.assertRaises(core.TollboothError) as cm:
            self.turn()
        self.assertEqual(cm.exception.code, core.EX_LIMITED)
        self.assertEqual((self.sent, self.clock.sleeps, self.throttle.used("lab")), ([], [], 0))

    def test_a_used_up_budget_is_exit_4_and_sends_nothing(self):
        self.turn(budget=1)
        with self.assertRaises(core.TollboothError) as cm:
            self.turn(budget=1)
        self.assertEqual((cm.exception.code, len(self.sent)), (core.EX_BUDGET, 1))

    def test_the_budget_is_checked_again_under_the_lock(self):
        # calls at the same time can't overrun a budget: another agent charges between the two checks
        self.throttle.check("lab", 1, "core")
        self.other_agent()._charge("lab")
        with self.assertRaises(core.TollboothError) as cm:
            self.throttle._attempt("lab", 1, "core", throttling.Deadline(self.clock), "GET x", self.send)
        self.assertEqual((cm.exception.code, self.sent), (core.EX_BUDGET, []))


class AnotherRound(TurnCase):
    """Another agent got in while we waited for the lock: the turn goes round again."""

    def test_a_block_recorded_meanwhile(self):
        self.turn(bucket="search")
        self.clock.sleeps.clear()
        sleep = self.clock.sleep

        def sleep_while_another_agent_is_blocked(seconds):
            sleep(seconds)
            if len(self.clock.sleeps) == 1:  # during the call gap
                self.other_agent()._mark("search", {"search": self.clock.now() + 10}, spaced=False)
        with mock.patch.object(self.clock, "sleep", sleep_while_another_agent_is_blocked):
            self.turn(bucket="search")
        self.assertEqual((self.clock.sleeps, len(self.sent)), ([throttling.SPACING["search"], 10], 2))

    def test_a_long_call_gap_landed_meanwhile(self):
        hold = self.throttle.hold
        landed = []

        def hold_after_another_agents_code_search(at):
            if not landed:
                landed.append(1)
                self.other_agent()._mark("code_search")
            return hold(at)
        with mock.patch.object(self.throttle, "hold", hold_after_another_agents_code_search):
            self.turn(bucket="code_search")
        self.assertEqual((self.clock.sleeps, len(self.sent)), ([throttling.SPACING["code_search"]], 1))


class Limits(TurnCase):
    """What a rate-limited answer does: blocks every agent, waits, sends again, or ends the call."""

    def answers(self, *limits):
        """A send that answers with each Limit in turn (None: not limited), then with no limit."""
        queue = list(limits)

        def send(left):
            self.sent.append(round(left, 3))
            limit = queue.pop(0) if queue else None
            return "answer", 403 if limit else 200, limit
        return send

    def test_a_primary_limit_blocks_its_bucket_for_every_agent_then_sends_again(self):
        seen = []
        sleep = self.clock.sleep

        def sleep_and_look(seconds):
            seen.append(round(self.other_agent().blocked_for("search"), 3))
            sleep(seconds)
        with mock.patch.object(self.clock, "sleep", sleep_and_look), self.assertLogs("gh-tollbooth", "INFO"):
            self.assertEqual(self.turn("search", send=self.answers(throttling.Limit("search", ("search",), 20))),
                             "answer")
        self.assertEqual((seen[0], self.clock.sleeps[0], len(self.sent)), (20, 20, 2))
        self.assertEqual([c.status for c in self.throttle.entries()], [403, 200])

    def test_a_secondary_limit_blocks_every_bucket(self):
        every = throttling.Limit("every bucket", tuple(throttling.SPACING), 30)
        with self.assertRaises(core.TollboothError):
            self.turn(deadline=throttling.Deadline(self.clock, seconds=30), send=self.answers(every))
        for b in throttling.SPACING:
            self.assertAlmostEqual(self.other_agent().blocked_for(b), 30, delta=0.01)

    def test_a_wait_longer_than_max_wait_is_exit_75_and_the_block_stays_for_every_agent(self):
        with self.assertRaises(core.TollboothError) as cm:
            self.turn(send=self.answers(throttling.Limit("core", ("core",), 3000)))
        self.assertEqual((cm.exception.code, len(self.sent)), (core.EX_LIMITED, 1))
        self.assertAlmostEqual(self.other_agent().blocked_for("core"), 3000, delta=0.01)

    def test_the_fourth_limited_answer_is_exit_75(self):
        one = throttling.Limit("core", ("core",), 1)
        with self.assertRaises(throttling.StoppedOnAnswer) as cm:
            self.turn(send=self.answers(one, one, one, one))
        self.assertEqual((cm.exception.code, len(self.sent)), (core.EX_LIMITED, 4))
        self.assertEqual(cm.exception.answer, "answer")  # send's result, for the command to show

    def test_a_limit_on_a_resource_the_throttle_does_not_track_is_not_shared(self):
        with self.assertRaises(core.TollboothError) as cm:
            self.turn(send=self.answers(throttling.Limit("dependency_sbom", (), 30)))
        self.assertEqual(cm.exception.code, core.EX_LIMITED)
        self.assertIn("dependency_sbom is rate-limited", str(cm.exception))
        self.assertEqual((len(self.sent), self.other_agent().blocked_for("core")), (1, 0.0))

    def test_the_backoff_doubles_across_calls_and_agents_until_an_answer_is_not_limited(self):
        unnamed = throttling.Limit("every bucket", tuple(throttling.SPACING), None)
        with self.assertRaises(core.TollboothError):  # waits 60 s, then 120 s is over TOLLBOOTH_MAX_WAIT
            self.turn(send=self.answers(unnamed, unnamed))
        self.assertEqual(self.clock.sleeps, [60])
        self.clock.sleep(200)
        with self.assertRaises(core.TollboothError):  # another agent's call: 240 s, at once
            self.other_agent().turn("lab", None, "core", throttling.Deadline(self.clock), "GET y",
                                    self.answers(unnamed))
        self.assertAlmostEqual(self.other_agent().blocked_for("core"), 240, delta=0.01)
        self.clock.sleep(241)
        self.turn()  # GitHub answers: the backoff starts over
        with self.assertRaises(core.TollboothError):
            self.turn(deadline=throttling.Deadline(self.clock, seconds=30), send=self.answers(unnamed))
        self.assertAlmostEqual(self.other_agent().blocked_for("core"), 60, delta=0.01)


if __name__ == "__main__":
    unittest.main()
