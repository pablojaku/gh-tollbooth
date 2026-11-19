"""Shared-state tests: call spacing, rate-limit blocks and budgets, all in state.db.

Mock-first: FakeClock replaces tollbooth_core.wall_clock, so times are exact and no test waits.
Run: uv run pytest
"""
import os
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import tollbooth_core as core
from _fixtures import FakeClock


class StateCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name) / "state"
        self.clock = FakeClock()
        p = mock.patch.object(core, "wall_clock", self.clock.now)
        p.start()
        self.addCleanup(p.stop)
        self.throttle = core.Throttle(self.dir)

    def other_agent(self):
        """A second Throttle on the same state folder, as another process would have."""
        return core.Throttle(self.dir)

    def rows(self, sql: str) -> list:
        """Rows straight from state.db, to see what is stored."""
        con = sqlite3.connect(self.throttle.db_path)
        try:
            return con.execute(sql).fetchall()
        finally:
            con.close()


class Spacing(StateCase):
    def test_the_gap_is_what_is_left_of_the_buckets_spacing(self):
        self.assertEqual(self.throttle.spacing_gap("search"), 0.0)  # no call yet
        self.throttle.mark("search")
        self.assertAlmostEqual(self.other_agent().spacing_gap("search"), core.SPACING["search"])
        self.clock.sleep(1.0)
        self.assertAlmostEqual(self.throttle.spacing_gap("search"), core.SPACING["search"] - 1.0)
        self.clock.sleep(5.0)
        self.assertEqual(self.throttle.spacing_gap("search"), 0.0)

    def test_buckets_are_spaced_separately(self):
        self.throttle.mark("code_search")
        self.assertEqual(self.throttle.spacing_gap("core"), 0.0)

    def test_a_last_call_time_in_the_future_waits_nothing(self):
        self.clock.wall += 25
        self.throttle.mark("core")  # written before a backward clock step
        self.clock.wall -= 25
        self.assertEqual(self.throttle.spacing_gap("core"), 0.0)

    def test_an_unspaced_mark_leaves_no_gap(self):
        self.throttle.mark("graphql", spaced=False)  # a cache hit
        self.assertEqual(self.throttle.spacing_gap("graphql"), 0.0)


class Blocks(StateCase):
    def test_a_block_is_seen_by_every_agent(self):
        self.assertEqual(self.throttle.blocked_for("core"), 0.0)
        self.throttle.mark("graphql", {b: self.clock.now() + 30 for b in core.SPACING})
        for b in core.SPACING:
            self.assertAlmostEqual(self.other_agent().blocked_for(b), 30, delta=0.01)
        self.clock.sleep(31)
        self.assertLessEqual(self.throttle.blocked_for("core"), 0)

    def test_a_shorter_block_never_shortens_a_longer_one(self):
        self.throttle.mark("core", {"core": self.clock.now() + 60})
        self.other_agent().mark("core", {"core": self.clock.now() + 10})
        self.assertAlmostEqual(self.throttle.blocked_for("core"), 60, delta=0.01)

    def test_a_block_longer_than_an_hour_is_capped_not_dropped(self):
        self.throttle.mark("core", {"core": self.clock.now() + 7200})  # e.g. Retry-After: 7200
        self.assertAlmostEqual(self.other_agent().blocked_for("core"), core.BLOCK_CAP, delta=0.01)
        self.clock.wall -= 60  # the wall clock steps back a minute: the capped block must still count
        self.assertAlmostEqual(self.throttle.blocked_for("core"), core.BLOCK_CAP + 60, delta=0.01)
        self.clock.sleep(core.BLOCK_CAP + 61)
        self.assertLessEqual(self.throttle.blocked_for("core"), 0)

    def test_a_block_too_far_ahead_is_ignored_and_overwritten(self):
        # left by a clock step or a reboot: GitHub limits reset hourly, so it can't be real
        self.clock.wall += 7200
        self.throttle.mark("core", {"core": self.clock.now() + 3600})
        self.clock.wall -= 7200
        self.assertEqual(self.throttle.blocked_for("core"), 0.0)
        self.throttle.mark("core", {"core": self.clock.now() + 60})
        self.assertAlmostEqual(self.throttle.blocked_for("core"), 60, delta=0.01)


class Budgets(StateCase):
    def test_charges_are_counted_per_label(self):
        for _ in range(3):
            self.throttle.charge("lab")
        self.other_agent().charge("other")
        self.assertEqual((self.throttle.used("lab"), self.throttle.used("other"), self.throttle.used("new")), (3, 1, 0))

    def test_a_refund_takes_back_one_charge_of_that_hour(self):
        hour = self.throttle.charge("lab")
        self.throttle.charge("lab")
        self.throttle.refund("lab", hour)
        self.assertEqual(self.throttle.used("lab"), 1)
        self.throttle.refund("lab", hour)
        self.throttle.refund("lab", hour)  # never below zero
        self.throttle.refund("nobody", hour)
        self.assertEqual(self.throttle.used("lab"), 0)

    def test_charges_age_out_after_a_day(self):
        self.throttle.charge("lab")
        self.clock.wall += 23 * 3600
        self.assertEqual(self.throttle.used("lab"), 1)
        self.clock.wall += 3 * 3600
        self.assertEqual(self.throttle.used("lab"), 0)
        self.throttle.charge("other")  # the next charge deletes the old one
        self.assertEqual(self.rows("SELECT label FROM charge"), [("other",)])

    def test_a_charge_in_the_future_is_not_counted(self):
        # left by a clock that ran ahead: without an upper limit it would count against the label for a year
        self.clock.wall += 365 * 24 * 3600
        self.throttle.charge("lab")
        self.clock.wall -= 365 * 24 * 3600
        self.assertEqual(self.throttle.used("lab"), 0)
        self.throttle.charge("other")  # the next charge deletes it
        self.assertEqual(self.rows("SELECT label FROM charge"), [("other",)])
        self.throttle.charge("lab")
        self.clock.wall -= 1800  # a small step back, into the hour before, keeps a charge just made
        self.assertEqual(self.throttle.used("lab"), 1)


class StateFile(StateCase):
    def test_spacing_blocks_and_charges_share_the_file_without_clobbering(self):
        self.throttle.charge("lab")
        self.throttle.mark("core", {"search": self.clock.now() + 30})
        self.throttle.charge("lab")
        self.assertEqual(self.throttle.used("lab"), 2)
        self.assertAlmostEqual(self.throttle.blocked_for("search"), 30, delta=0.01)
        self.assertAlmostEqual(self.throttle.spacing_gap("core"), core.SPACING["core"])

    @unittest.skipIf(os.geteuid() == 0, "root can open a file without permission")
    def test_a_file_that_cannot_be_opened_is_an_error_not_empty(self):
        self.throttle.mark("core", {"core": self.clock.now() + 60})
        self.throttle.db_path.chmod(0)
        self.addCleanup(self.throttle.db_path.chmod, 0o644)
        with self.assertRaises(core.TollboothError) as cm:
            self.throttle.blocked_for("core")
        self.assertEqual(cm.exception.code, core.EX_FAIL)
        self.assertIn("TOLLBOOTH_STATE_DIR", str(cm.exception))
        self.throttle.db_path.chmod(0o644)
        self.assertAlmostEqual(self.throttle.blocked_for("core"), 60, delta=0.01)  # the block survived

    def test_a_file_that_is_not_a_database_is_moved_aside_and_started_again(self):
        self.throttle.db_path.write_bytes(b"not a database " * 100)
        with self.assertLogs("gh-tollbooth", "WARNING") as logs:
            self.assertEqual(self.throttle.blocked_for("core"), 0.0)
        self.assertIn("state.db.broken", logs.output[0])
        self.assertTrue((self.dir / "state.db.broken").exists())
        self.throttle.mark("core", {"core": self.clock.now() + 30})
        self.assertAlmostEqual(self.throttle.blocked_for("core"), 30, delta=0.01)

    def test_a_busy_file_is_exit_76(self):
        holder = sqlite3.connect(self.throttle.db_path, isolation_level=None)
        self.addCleanup(holder.close)
        holder.execute("BEGIN IMMEDIATE")  # another agent in the middle of a change
        with mock.patch.object(core, "STATE_WAIT", 0.1), self.assertRaises(core.TollboothError) as cm:
            self.throttle.charge("lab")
        self.assertEqual(cm.exception.code, core.EX_BUSY)


class FolderVanishes(StateCase):
    """Someone deletes the state folder (rm -rf ~/.cache/gh-tollbooth) around a step."""

    def deleting_after_connect(self, open_file_first: bool):
        """sqlite3.connect, except that the first connection's folder is deleted right after it is made."""
        real, left = sqlite3.connect, [1]

        def connect(*args, **kwargs):
            con = real(*args, **kwargs)
            if left[0]:
                left[0] = 0
                if open_file_first:
                    con.execute("SELECT count(*) FROM sqlite_master").fetchone()
                shutil.rmtree(self.dir)
            return con
        return mock.patch.object(core.sqlite3, "connect", connect)

    def test_before_a_step(self):
        shutil.rmtree(self.dir)
        self.throttle.charge("lab")
        self.assertEqual(self.throttle.used("lab"), 1)

    def test_before_the_file_is_opened(self):
        with self.deleting_after_connect(open_file_first=False):
            self.throttle.charge("lab")
        self.assertEqual(self.throttle.used("lab"), 1)

    def test_while_the_file_is_open(self):
        self.throttle.charge("lab")
        with self.deleting_after_connect(open_file_first=True):
            self.throttle.charge("lab")
        self.assertEqual(self.throttle.used("lab"), 1)  # the folder, and the first charge, are gone


if __name__ == "__main__":
    unittest.main()
