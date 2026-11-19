"""Turn tests: one call's time budget (Deadline).

Mock-first: a FakeClock stands in for both clocks, so no test waits.
Run: uv run pytest
"""
import unittest
from unittest import mock

import tollbooth_core as core
import tollbooth_throttle as throttling
from _fixtures import FakeClock


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


if __name__ == "__main__":
    unittest.main()
