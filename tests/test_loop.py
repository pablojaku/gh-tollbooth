"""Call-loop tests: one GitHub request through the shared throttle.

Mock-first: tollbooth_http.send and tollbooth_http.token are replaced, so nothing leaves the machine.
The throttle is given a FakeClock, so waits finish instantly; RealThreads uses the real clock and lock.
Run: python3 -m unittest discover -s tests -v
"""
import fcntl
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from _fixtures import TOKEN, ThrottleCase, answer, hold_lock_elsewhere, http, lock_is_free, primary, recorded, secondary
import tollbooth_core as core  # noqa: E402  # pyright: ignore[reportMissingImports]
import tollbooth_loop as loop  # noqa: E402  # pyright: ignore[reportMissingImports]
import tollbooth_throttle as throttling  # noqa: E402  # pyright: ignore[reportMissingImports]

OK = answer(200, body=b'{"ok": 1}')


class LoopCase(ThrottleCase):
    def setUp(self):
        super().setUp()
        self.patch(http, "token", mock.Mock(return_value=TOKEN))

    def patch(self, obj, name, value):
        p = mock.patch.object(obj, name, value)
        p.start()
        self.addCleanup(p.stop)

    def caller(self, label="t", budget=None):
        return loop.Caller(label, budget, self.other_agent())

    def fetch(self, answers, caller=None, method="GET", path="graphql", **kwargs):
        """Caller.fetch() with GitHub's answers (or exceptions) in order. Returns (answer, send mock, caller)."""
        caller = caller or self.caller()
        if method == "GET" and path == "graphql":
            method, kwargs = "POST", {"json_body": {"query": "{ viewer { login } }"}, **kwargs}
        with mock.patch.object(http, "send", side_effect=list(answers)) as send:
            return caller.fetch(method, path, **kwargs), send, caller

    def fetch_refused(self, code, answers, **kwargs):
        return self.refused(code, self.fetch, answers, **kwargs)

    def blocks(self, caller):
        return {k: v for k, v in caller.throttle.state().items() if k.startswith("blocked:")}

    def lock_probe(self, throttle, free):
        """An on_sleep hook that records whether the machine-wide lock is free during each sleep."""
        return lambda s: free.append(lock_is_free(throttle))


class Requests(LoopCase):
    def test_an_answer_that_is_not_a_limit_is_returned_as_it_is(self):
        for status in (200, 404, 401, 422, 500):
            reply = answer(status, body=b'{"message": "x"}')
            resp, send, caller = self.fetch([reply], method="GET", path="repos/o/r")
            self.assertIs(resp, reply)
            self.assertEqual(send.call_count, 1)
            self.assertEqual(self.blocks(caller), {})

    def test_the_request_is_sent_as_asked_and_logged(self):
        _, send, caller = self.fetch([OK], method="GET", path="repos/o/r/readme", accept="application/vnd.github.raw")
        args, kwargs = send.call_args
        self.assertEqual(args, ("GET", "repos/o/r/readme", TOKEN))
        self.assertEqual((kwargs["accept"], kwargs["json_body"]), ("application/vnd.github.raw", None))
        self.assertLessEqual(kwargs["timeout"], loop.CALL_TIMEOUT)
        (entry,) = caller.throttle.entries()
        self.assertEqual((entry[1], entry[3], entry[4], entry[6]), ("t", "core", "200", "GET repos/o/r/readme"))
        self.assertEqual(caller.throttle.used("t"), 1)

    def test_a_graphql_request_logs_its_document(self):
        _, _, caller = self.fetch([OK], json_body={"query": "query {\n  viewer { login }\n}", "variables": {}},
                               method="POST", path="graphql")
        self.assertEqual(caller.throttle.entries()[0][6], "POST graphql query { viewer { login } }")

    def test_a_request_it_would_never_send_costs_nothing(self):
        for method, path, kwargs in (("PUT", "repos/o/r", {}), ("GET", "//evil.example/x", {}),
                                     ("POST", "repos/o/r/issues", {"json_body": {"title": "x"}})):
            caller = self.caller()
            with self.assertRaises(ValueError):
                self.fetch([OK], caller=caller, method=method, path=path, **kwargs)
            t = caller.throttle
            self.assertEqual((t.used("t"), t.entries(), t.state()), (0, [], {}))

    def test_the_token_is_asked_for_once_and_kept_out_of_reprs(self):
        caller = self.caller()
        with mock.patch.object(http, "token", return_value=TOKEN) as token:
            for _ in range(2):
                self.fetch([OK], caller=caller, method="GET", path="repos/o/r")
        self.assertEqual(token.call_count, 1)
        self.assertNotIn(TOKEN, repr(caller))

    def test_the_request_record_keeps_the_token_out_of_its_repr(self):
        req = loop._Request("GET", "repos/o/r", TOKEN, None, None, "core", "GET repos/o/r")
        self.assertNotIn(TOKEN, repr(req))

    def test_a_command_that_sends_nothing_never_asks_for_the_token(self):
        with mock.patch.object(http, "token") as token:
            caller = self.caller("lab", budget=5)
            caller.check("graphql")
        token.assert_not_called()

    def test_check_refuses_a_spent_budget_or_a_long_block(self):
        caller = self.caller("lab", budget=1)
        self.fetch([OK], caller=caller, method="GET", path="repos/o/r")
        self.refused(core.EX_BUDGET, caller.check, "core")
        caller = self.caller("other")
        recorded(caller.throttle, "graphql", {"graphql": throttling.MAX_WAIT + 30})
        self.refused(core.EX_LIMITED, caller.check, "graphql")
        caller.check("core")

    def test_no_token_costs_nothing(self):
        caller = self.caller()
        with mock.patch.object(http, "token", side_effect=http.NoToken("no GitHub token")):
            with self.assertRaises(http.NoToken):
                self.fetch([OK], caller=caller)
        self.assertEqual((caller.throttle.used("t"), caller.throttle.state()), (0, {}))

    def test_no_answer_is_charged_logged_and_raised(self):
        # the request may have reached GitHub, so it is paid for and spaced like any other
        caller = self.caller()
        with self.assertRaises(http.TransportError):
            self.fetch([http.TransportError("timeout", "no answer in 90 s")], caller=caller, method="GET",
                       path="repos/o/r")
        self.assertEqual(caller.throttle.used("t"), 1)
        self.assertEqual(caller.throttle.entries()[0][4], "0")
        self.clock.sleeps.clear()
        caller.throttle.wait_turn("core", self.clock.mono() + 100)  # the next call in core waits its gap
        self.assertTrue(self.clock.sleeps and self.clock.sleeps[0] > 0, self.clock.sleeps)

    def test_the_call_is_charged_before_the_request_is_sent(self):
        caller = self.caller("lab", budget=1)
        with self.assertRaises(KeyboardInterrupt):
            self.fetch([KeyboardInterrupt()], caller=caller, method="GET", path="repos/o/r")
        self.assertEqual(caller.throttle.used("lab"), 1)


class Limits(LoopCase):
    """What GitHub answered is shared with every agent, through the blocks in state.json."""

    def test_a_429_with_no_wait_blocks_every_bucket_then_retries(self):
        start = self.clock.now()
        resp, send, caller = self.fetch([secondary(), OK])
        self.assertEqual((resp, send.call_count), (OK, 2))
        for b in core.SPACING:
            self.assertAlmostEqual(self.blocks(caller)["blocked:" + b], start + 60, delta=2)
        self.assertIn(60, self.clock.sleeps)

    def test_retry_after_is_honored(self):
        resp, _, _ = self.fetch([secondary(30, status=403), OK])
        self.assertIs(resp, OK)
        self.assertIn(30, self.clock.sleeps)

    def test_a_primary_limit_waits_for_githubs_reset(self):
        resp, send, caller = self.fetch([primary(self.clock.now() + 30), OK], method="GET", path="repos/o/r")
        self.assertEqual((resp, send.call_count), (OK, 2))
        self.assertIn(32, [round(s) for s in self.clock.sleeps])
        self.assertEqual(list(self.blocks(caller)), ["blocked:core"])

    def test_a_primary_limit_blocks_the_bucket_github_names(self):
        _, _, caller = self.fetch([primary(self.clock.now() + 30, resource="search"), OK], method="GET",
                                  path="repos/o/r")
        self.assertEqual(list(self.blocks(caller)), ["blocked:search"])
        _, _, caller = self.fetch([primary(self.clock.now() + 30, resource="dependency_sbom"), OK],
                               method="GET", path="repos/o/r")
        self.assertIn("blocked:core", self.blocks(caller))  # a bucket it doesn't track: the request's own

    def test_a_graphql_200_with_errors_and_nothing_left_is_a_primary_limit(self):
        spent = {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(int(self.clock.now() + 20)),
                 "X-RateLimit-Resource": "graphql"}
        resp, send, _ = self.fetch([answer(200, spent, b'{"errors": [{"type": "RATE_LIMITED"}]}'), OK])
        self.assertEqual((resp, send.call_count), (OK, 2))
        self.assertIn(22, [round(s) for s in self.clock.sleeps])

    def test_other_graphql_200s_are_answers(self):  # the cases themselves are classify's tests
        spent = {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(int(self.clock.now() + 20))}
        reply = answer(200, spent, b'{"data": {"viewer": null}}')  # the last allowed call worked
        resp, send, caller = self.fetch([reply])
        self.assertEqual((resp, send.call_count, self.blocks(caller)), (reply, 1, {}))

    def test_error_text_never_blocks(self):
        body = b'{"message": "API rate limit exceeded for user ID 1. You have exceeded a secondary rate limit"}'
        left = {"X-RateLimit-Remaining": "4000", "X-RateLimit-Reset": str(int(self.clock.now() + 900))}
        for status in (403, 404, 422):
            resp, send, caller = self.fetch([answer(status, left, body)], method="GET", path="repos/o/r")
            self.assertEqual((resp.status, send.call_count, self.blocks(caller)), (status, 1, {}))

    def test_a_502_or_504_is_too_heavy(self):
        for status in (502, 504):
            caller = self.caller(f"t{status}")
            error = self.fetch_refused(core.EX_HEAVY, [answer(status, body=b"<html>")], caller=caller)
            self.assertIn("lighter", str(error))
            self.assertEqual(caller.throttle.used(f"t{status}"), 1)

    def test_a_rest_502_or_504_exits_3_without_graphql_advice(self):
        for status in (502, 504):
            caller = self.caller(f"r{status}")
            error = self.fetch_refused(core.EX_HEAVY, [answer(status, body=b"<html>")], caller=caller, method="GET",
                                 path="repos/o/r")
            self.assertIn("timed out on GET repos/o/r", str(error))
            self.assertNotIn("aliases", str(error))
            self.assertEqual(caller.throttle.used(f"r{status}"), 1)

    def test_a_wait_too_long_to_show_as_a_time_still_ends_with_one_message(self):
        caller = self.caller()
        error = self.fetch_refused(core.EX_LIMITED, [secondary(10 ** 12, status=403)], caller=caller)  # past year 9999
        self.assertIn("every bucket is rate-limited until", str(error))
        for until in self.blocks(caller).values():  # capped at about an hour, as every block is
            self.assertLessEqual(until - self.clock.now(), core.BLOCK_SANITY + 0.01)


class Backoff(LoopCase):
    def test_a_repeated_429_doubles_the_wait_then_gives_up(self):
        start = self.clock.now()
        self.fetch_refused(core.EX_LIMITED, [secondary()] * 4)
        self.assertEqual(self.clock.sleeps.count(60), 1)
        self.assertAlmostEqual(self.caller().throttle.blocked_for("core") + self.clock.now() - start, 60 + 120, delta=3)

    def test_at_most_four_limited_answers_per_call(self):
        self.patch(throttling, "MAX_WAIT", 10_000)
        self.patch(core, "DEADLINE", 100_000)
        self.fetch_refused(core.EX_LIMITED, [secondary()] * 10)
        self.assertEqual(len(self.caller().throttle.entries()), loop.MAX_ANSWERS)

    def test_a_wait_is_never_slept_past_the_deadline(self):
        self.patch(core, "DEADLINE", 30)
        self.fetch_refused(core.EX_LIMITED, [secondary()])
        self.assertNotIn(60, self.clock.sleeps)

    def test_another_agent_waits_for_a_published_block(self):
        a, b = self.caller("a"), self.caller("b")
        recorded(a.throttle, "graphql", {bk: 30 for bk in core.SPACING})
        _, send, _ = self.fetch([OK], caller=b, method="GET", path="repos/o/r")
        self.assertIn(30, self.clock.sleeps)
        self.assertEqual(send.call_count, 1)

    def test_the_lock_is_free_during_a_backoff(self):
        caller, free = self.caller(), []
        self.clock.on_sleep = self.lock_probe(caller.throttle, free)
        self.fetch([secondary(), OK], caller=caller)
        self.assertTrue(free and all(free), free)


class Deadlines(LoopCase):
    def test_a_block_longer_than_max_wait_fails_at_once(self):
        caller = self.caller()
        recorded(caller.throttle, "graphql", {"graphql": throttling.MAX_WAIT + 30})
        self.fetch_refused(core.EX_LIMITED, [OK], caller=caller)
        self.assertEqual(self.clock.sleeps, [])

    def test_a_block_past_the_deadline_fails_at_once(self):
        caller = self.caller()
        recorded(caller.throttle, "graphql", {"graphql": 50})
        self.patch(core, "DEADLINE", 30)
        self.fetch_refused(core.EX_LIMITED, [OK], caller=caller)
        self.assertEqual(self.clock.sleeps, [])

    def test_a_block_is_waited_out_without_the_lock(self):
        caller, free = self.caller(), []
        recorded(caller.throttle, "graphql", {"graphql": 20})
        self.clock.on_sleep = self.lock_probe(caller.throttle, free)
        self.fetch([OK], caller=caller)
        self.assertTrue(free and free[0], free)

    def test_waiting_for_the_lock_gives_up_busy(self):
        caller = self.caller()
        hold_lock_elsewhere(self, caller.throttle)
        self.patch(core, "DEADLINE", 3)
        self.fetch_refused(core.EX_BUSY, [OK], caller=caller)

    def test_a_wall_clock_step_does_not_move_the_deadline(self):
        caller = self.caller()
        hold_lock_elsewhere(self, caller.throttle)
        self.patch(core, "DEADLINE", 3)

        def stepping(s):
            self.clock.wall -= 600  # the wall clock jumps back on every poll
        self.clock.on_sleep = stepping
        start = self.clock.mono()
        self.fetch_refused(core.EX_BUSY, [OK], caller=caller)
        self.assertLess(self.clock.mono() - start, 4)

    def test_the_request_timeout_shrinks_after_waiting(self):
        caller = self.caller()
        recorded(caller.throttle, "graphql", {"graphql": 30})
        _, send, _ = self.fetch([OK], caller=caller)
        self.assertLessEqual(send.call_args.kwargs["timeout"], core.DEADLINE - 30 + 0.01)

    def test_no_request_starts_with_too_little_time_left(self):
        caller = self.caller()
        recorded(caller.throttle, "graphql", {"graphql": 20})
        self.patch(core, "DEADLINE", 30)  # 10 s left after the wait, under MIN_CALL_TIME
        self.fetch_refused(core.EX_BUSY, [OK], caller=caller)

    def test_a_short_deadline_still_leaves_time_for_a_request(self):
        self.patch(core, "DEADLINE", 8)  # under MIN_CALL_TIME: half of it is kept for the request
        _, send, _ = self.fetch([OK])
        self.assertLessEqual(send.call_args.kwargs["timeout"], 8)


class Spacing(LoopCase):
    def test_spacing_waits_but_never_longer_than_spacing(self):
        caller = self.caller()
        self.patch(core, "SPACING", {k: 2.0 for k in core.SPACING})
        recorded(caller.throttle, "core")
        self.fetch([OK], caller=caller, method="GET", path="repos/o/r")
        self.assertIn(2.0, self.clock.sleeps)
        self.clock.sleeps.clear()
        st = caller.throttle.state()
        st["core"] = self.clock.now() + 25  # a last-call time in the future
        caller.throttle.state_path.write_text(json.dumps(st))
        self.fetch([OK], caller=caller, method="GET", path="repos/o/r")
        self.assertTrue(all(s <= 2.0 for s in self.clock.sleeps), self.clock.sleeps)

    def test_a_code_search_gap_is_slept_without_the_lock(self):
        caller, free = self.caller(), []
        recorded(caller.throttle, "code_search")
        self.clock.on_sleep = self.lock_probe(caller.throttle, free)
        self.fetch([OK], caller=caller, method="GET", path="search/code?q=x")
        self.assertTrue(free and all(free), free)
        self.assertIn(6.5, self.clock.sleeps)

    def test_a_long_gap_that_reappears_under_the_lock_is_slept_without_it(self):
        caller, free, first = self.caller(), [], [True]  # another agent's code search lands while we wait for the lock
        holder = hold_lock_elsewhere(self, caller.throttle)
        probe = self.lock_probe(caller.throttle, free)

        def sleeper(s):
            if first[0]:
                first[0] = False
                recorded(caller.throttle, "code_search")
                fcntl.flock(holder, fcntl.LOCK_UN)
                return
            probe(s)
        self.clock.on_sleep = sleeper
        self.fetch([OK], caller=caller, method="GET", path="search/code?q=x")
        long_sleeps = [ok for ok, s in zip(free, self.clock.sleeps[1:]) if s > throttling.SPACING_UNDER_LOCK]
        self.assertTrue(long_sleeps and all(long_sleeps), (free, self.clock.sleeps))

    def test_a_gap_is_never_slept_past_the_deadline(self):
        caller = self.caller()
        recorded(caller.throttle, "code_search")
        self.patch(core, "DEADLINE", 10)
        self.fetch_refused(core.EX_BUSY, [OK], caller=caller, method="GET", path="search/code?q=x")
        self.assertEqual(self.clock.sleeps, [])


class Blocks(LoopCase):
    def test_a_block_recorded_while_waiting_for_the_lock_is_honored(self):
        caller, first = self.caller(), [True]
        holder = hold_lock_elsewhere(self, caller.throttle)

        def sleeper(s):
            if first[0]:
                first[0] = False
                recorded(caller.throttle, "graphql", {"graphql": 10})
                fcntl.flock(holder, fcntl.LOCK_UN)
        self.clock.on_sleep = sleeper
        resp, send, _ = self.fetch([OK], caller=caller)
        self.assertEqual((resp, send.call_count), (OK, 1))
        self.assertTrue(any(9 <= s <= 10.5 for s in self.clock.sleeps), self.clock.sleeps)


class Budgets(LoopCase):
    def test_calls_are_charged_and_stop_at_the_budget(self):
        caller = self.caller("lab", budget=2)
        self.fetch([OK], caller=caller)
        self.fetch([OK], caller=caller, method="GET", path="repos/o/r")
        with mock.patch.object(http, "send") as send:
            with self.assertRaises(core.TollboothError) as cm:
                caller.fetch("GET", "repos/o/r")
        self.assertEqual(cm.exception.code, core.EX_BUDGET)
        send.assert_not_called()

    def test_a_limited_answer_is_charged(self):
        caller = self.caller("lab")
        self.fetch([secondary(5), OK], caller=caller)
        self.assertEqual(caller.throttle.used("lab"), 2)


class RealThreads(unittest.TestCase):
    """Real lock, real clock, real threads: budgets can't overrun, and a latecomer isn't starved."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for obj, name, value in ((core, "SPACING", {k: 0 for k in core.SPACING}),
                                 (http, "token", mock.Mock(return_value=TOKEN))):
            p = mock.patch.object(obj, name, value)
            p.start()
            self.addCleanup(p.stop)

    def caller(self, label, budget=None):
        return loop.Caller(label, budget, throttling.Throttle(Path(self.tmp.name) / "state"))

    def test_no_budget_overrun(self):
        sent, errors = [], []

        def slow_send(*a, **k):
            sent.append(1)
            time.sleep(0.2)
            return OK

        def worker():
            try:
                self.caller("par", 2).fetch("GET", "repos/o/r")
            except core.TollboothError as e:
                errors.append(e.code)

        with mock.patch.object(http, "send", side_effect=slow_send):
            threads = [threading.Thread(target=worker) for _ in range(6)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        self.assertEqual((len(sent), errors), (2, [core.EX_BUDGET] * 4))

    def test_a_latecomer_gets_in_between_a_busy_agents_calls(self):
        # No queue: the busy agent sleeps its call gap without the lock, which is when a latecomer gets in.
        order = []

        def slow_send(method, path, *a, **k):
            order.append(path)
            time.sleep(0.1)
            return OK

        def run(label, n):
            caller = self.caller(label)
            for i in range(n):
                caller.fetch("GET", f"{label}/{i}")

        with mock.patch.object(core, "SPACING", {k: 0.3 for k in core.SPACING}), \
                mock.patch.object(http, "send", side_effect=slow_send):
            busy = threading.Thread(target=run, args=("A", 6))
            busy.start()
            time.sleep(0.25)
            late = threading.Thread(target=run, args=("C", 1))
            late.start()
            busy.join()
            late.join()
        self.assertLessEqual(order.index("C/0"), 3, order)


if __name__ == "__main__":
    unittest.main()
