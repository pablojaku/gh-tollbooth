"""Call-loop tests: the Caller, and Caller.fetch(), one GitHub request through the shared throttle.

Mock-first: tollbooth_http.send and token are replaced, so nothing leaves the machine and `gh` never
runs. The Throttle is given a FakeClock, so waits finish instantly.
Run: uv run pytest
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import tollbooth_core as core
import tollbooth_http as http
import tollbooth_loop as loop
import tollbooth_throttle as throttling
from _fixtures import FakeClock, answer, primary, secondary, settings

TOKEN = "ghx_FAKEtokenFAKEtoken"
OK = answer(200, body=b'{"ok": 1}')
SPENT_GRAPHQL = answer(200, {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": int(FakeClock().now() + 20),
                             "X-RateLimit-Resource": "graphql"},
                       json.dumps({"errors": [{"type": "RATE_LIMITED"}]}).encode())


class LoopCase(unittest.TestCase):
    """A fake clock, a Throttle on a temporary folder, and a fake GitHub that answers from `self.answers`."""

    def setUp(self):
        self.clock = FakeClock()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name) / "state"
        self.throttle = throttling.Throttle(self.dir, self.clock)
        self.caller = loop.Caller("lab", None, self.throttle)
        self.answers = []
        self.patch(core, "SETTINGS", settings(deadline=100, max_wait=60))
        self.send = self.patch(http, "send", mock.Mock(side_effect=lambda *a, **k: self.next_answer()))
        self.token = self.patch(http, "token", mock.Mock(return_value=TOKEN))

    def patch(self, obj, name, value):
        p = mock.patch.object(obj, name, value)
        p.start()
        self.addCleanup(p.stop)
        return value

    def next_answer(self):
        got = self.answers.pop(0) if self.answers else OK
        if isinstance(got, Exception):
            raise got
        return got

    def fetch(self, method="GET", path="repos/o/r", **kwargs):
        return self.caller.fetch(method, path, **kwargs)

    def statuses(self):
        return [c.status for c in self.throttle.entries()]


class Answers(LoopCase):
    def test_an_answer_that_is_not_a_limit_comes_back_as_it_is(self):
        for resp in (OK, answer(404), answer(403, {"X-RateLimit-Remaining": "4000"}), answer(401)):
            self.answers = [resp]
            self.assertIs(self.fetch(), resp)
        self.assertEqual((self.statuses(), self.throttle.used("lab")), ([200, 404, 403, 401], 4))

    def test_the_token_is_asked_for_once_and_kept_out_of_repr(self):
        self.fetch()
        self.fetch()
        self.assertEqual((self.token.call_count, self.caller.token()), (1, TOKEN))
        self.assertNotIn(TOKEN, repr(self.caller))

    def test_the_request_gets_the_time_left_at_most_90_s(self):
        self.fetch()
        self.assertEqual(self.send.call_args.kwargs["timeout"], 90)
        self.patch(core, "SETTINGS", settings(deadline=50, max_wait=60))
        self.fetch()
        self.assertEqual(self.send.call_args.kwargs["timeout"], 49.5)  # 50 s, less core's 0.5 s call gap

    def test_the_log_shows_what_was_asked(self):
        self.fetch("POST", "graphql", json_body={"query": "{ viewer { login } }"})
        self.assertEqual([c.summary for c in self.throttle.entries()], ["POST graphql { viewer { login } }"])


class NothingCharged(LoopCase):
    def test_a_request_it_would_refuse(self):
        with self.assertRaises(ValueError):
            self.fetch("DELETE", "repos/o/r")
        self.assertEqual((self.send.call_count, self.throttle.used("lab")), (0, 0))

    def test_no_token(self):
        self.token.side_effect = http.NoToken("no GitHub token: run `gh auth login`")
        with self.assertRaises(http.NoToken):
            self.fetch()
        self.assertEqual((self.send.call_count, self.throttle.used("lab")), (0, 0))


class Limits(LoopCase):
    def test_a_primary_limit_waits_for_the_reset_and_sends_again(self):
        self.answers = [primary(self.clock.now() + 20)]
        self.assertIs(self.fetch(), OK)
        self.assertEqual((self.clock.sleeps, self.statuses()), ([22], [403, 200]))  # the reset, plus 2 s

    def test_a_secondary_limit_blocks_every_bucket(self):
        self.answers = [secondary(30)]
        sleep = self.clock.sleep
        seen = []

        def sleep_and_look(seconds):
            seen.append([round(throttling.Throttle(self.dir, self.clock).blocked_for(b)) for b in throttling.BUCKETS])
            sleep(seconds)
        with mock.patch.object(self.clock, "sleep", sleep_and_look):
            self.assertIs(self.fetch(), OK)
        self.assertEqual(seen[0], [30, 30, 30, 30])

    def test_a_secondary_limit_that_names_no_wait_backs_off_60_s(self):
        self.answers = [secondary()]
        self.fetch()
        self.assertEqual(self.clock.sleeps, [60])

    def test_a_spent_graphql_limit_is_a_primary_limit_on_graphql(self):
        self.answers = [SPENT_GRAPHQL]
        self.assertIs(self.fetch("POST", "graphql", json_body={"query": "{ viewer { login } }"}), OK)
        self.assertEqual(self.clock.sleeps, [22])

    def test_a_limit_on_a_resource_the_throttle_does_not_track_is_exit_75_and_not_shared(self):
        self.answers = [primary(self.clock.now() + 20, resource="dependency_sbom")]
        with self.assertRaises(core.TollboothError) as cm:
            self.fetch("GET", "repos/o/r/dependency-graph/sbom")
        self.assertEqual(cm.exception.code, core.EX_LIMITED)
        self.assertIn("dependency_sbom", str(cm.exception))
        self.assertEqual(self.throttle.blocked_for("core"), 0.0)

    def test_a_limit_that_ends_the_call_carries_githubs_answer(self):
        far = primary(self.clock.now() + 3000)  # longer than TOLLBOOTH_MAX_WAIT
        self.answers = [far]
        with self.assertRaises(loop.StoppedOnAnswer) as cm:
            self.fetch()
        self.assertEqual((cm.exception.code, cm.exception.answer), (core.EX_LIMITED, far))


class GitHubGaveUp(LoopCase):
    def test_a_502_on_a_graphql_query_is_exit_3_with_advice_and_githubs_answer(self):
        gave_up = answer(502, body=b'{"message": "Server Error"}')
        self.answers = [gave_up]
        with self.assertRaises(loop.StoppedOnAnswer) as cm:
            self.fetch("POST", "graphql", json_body={"query": "{ a }"})
        self.assertEqual((cm.exception.code, cm.exception.answer), (core.EX_HEAVY, gave_up))
        self.assertIn("Make it lighter", str(cm.exception))
        self.assertEqual(self.statuses(), [502])  # charged and logged: it was sent

    def test_a_504_on_rest_is_exit_3_with_a_plain_message(self):
        self.answers = [answer(504)]
        with self.assertRaises(core.TollboothError) as cm:
            self.fetch()
        self.assertEqual(cm.exception.code, core.EX_HEAVY)
        self.assertIn("Try again later", str(cm.exception))

    def test_no_answer_is_charged_logged_with_status_0_and_raised(self):
        self.answers = [http.TransportError("timeout", "no answer in 90 s")]
        with self.assertRaises(http.TransportError):
            self.fetch()
        self.assertEqual((self.statuses(), self.throttle.used("lab")), ([0], 1))



class Check(LoopCase):
    """Caller.check(): fail before any work, sending nothing."""

    def test_a_used_up_budget_is_exit_4(self):
        self.caller.budget = 1
        self.fetch()
        with self.assertRaises(core.TollboothError) as cm:
            self.caller.check("core")
        self.assertEqual((cm.exception.code, self.send.call_count), (core.EX_BUDGET, 1))

    def test_a_long_block_is_exit_75_and_a_short_one_passes(self):
        self.answers = [secondary(30)]
        self.fetch()  # blocks every bucket for 30 s, waits, and sends again
        throttling.Throttle(self.dir, self.clock)._mark("search", {"search": self.clock.now() + 120})
        with self.assertRaises(core.TollboothError) as cm:
            self.caller.check("search")
        self.assertEqual(cm.exception.code, core.EX_LIMITED)
        self.assertNotIsInstance(cm.exception, loop.StoppedOnAnswer)  # no answer: nothing was sent
        self.caller.check("core")
        self.assertEqual(self.send.call_count, 2)


if __name__ == "__main__":
    unittest.main()
