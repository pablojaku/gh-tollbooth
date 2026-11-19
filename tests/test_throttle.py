"""Shared-state tests: the machine-wide lock, call spacing, blocks, budgets and the call log.

Mock-first: FakeClock replaces tollbooth_core._now/_mono/_sleep, so waits finish instantly.
Run: python3 -m unittest discover -s tests -v
"""
import contextlib
import fcntl
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from _fixtures import SCRIPTS, FakeClock, load_cli

cli = load_cli("tollbooth_throttle_cli")
core = cli.core


class ClockCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clock = FakeClock()
        for name, fn in (("_now", self.clock.now), ("_mono", self.clock.mono), ("_sleep", self.clock.sleep)):
            p = mock.patch.object(core, name, fn)
            p.start()
            self.addCleanup(p.stop)
        self.throttle = core.Throttle(Path(self.tmp.name) / "state")

    def other_agent(self):
        """A second Throttle on the same state folder, as another process would have."""
        return core.Throttle(Path(self.tmp.name) / "state")


class Lock(ClockCase):
    def held_elsewhere(self):
        holder = open(self.throttle.lock_path, "a+")
        fcntl.flock(holder, fcntl.LOCK_EX)
        self.addCleanup(holder.close)
        return holder

    def is_free(self):
        with open(self.throttle.lock_path, "a+") as fh:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return False
            fcntl.flock(fh, fcntl.LOCK_UN)
            return True

    def test_only_one_holder_at_a_time(self):
        with self.throttle.hold(self.clock.mono() + 10):
            self.assertFalse(self.is_free())
        self.assertTrue(self.is_free())

    def test_released_when_the_holder_fails(self):
        with self.assertRaises(RuntimeError):
            with self.throttle.hold(self.clock.mono() + 10):
                raise RuntimeError("boom")
        self.assertTrue(self.is_free())

    def test_waiting_gives_up_busy_at_the_deadline(self):
        self.held_elsewhere()
        start = self.clock.mono()
        with self.assertRaises(core.TollboothError) as cm:
            with self.other_agent().hold(start + 3):
                self.fail("got a lock that is held")
        self.assertEqual(cm.exception.code, core.EX_BUSY)
        self.assertAlmostEqual(self.clock.mono() - start, 3, delta=0.25)

    def test_a_wall_clock_step_does_not_move_the_deadline(self):
        self.held_elsewhere()

        def stepping_sleep(s):
            self.clock.wall -= 600  # the wall clock jumps back on every poll
            self.clock.sleep(s)
        start = self.clock.mono()
        with mock.patch.object(core, "_sleep", stepping_sleep), self.assertRaises(core.TollboothError):
            with self.throttle.hold(start + 3):
                pass
        self.assertLess(self.clock.mono() - start, 4)

    def test_waiter_gets_in_once_the_lock_is_released_and_says_it_waited(self):
        holder = self.held_elsewhere()

        def sleep_then_release(s):
            self.clock.sleep(s)
            if self.clock.mono() - start > 6:
                fcntl.flock(holder, fcntl.LOCK_UN)
        start = self.clock.mono()
        err = io.StringIO()
        with mock.patch.object(core, "_sleep", sleep_then_release), contextlib.redirect_stderr(err):
            with self.throttle.hold(start + 60):
                pass
        self.assertEqual(err.getvalue().count("waiting for another agent"), 1)

    def test_a_waiter_follows_the_lock_file_when_it_is_replaced(self):
        # Someone deletes the lock file while we wait on it, and a third agent locks the new one.
        # Getting the old, deleted file's lock must not count as holding the lock.
        old = self.held_elsewhere()
        third, steps = [], []

        def sleeper(s):
            self.clock.sleep(s)
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
        with mock.patch.object(core, "_sleep", sleeper):
            with self.throttle.hold(start + 60):
                steps.append("got the lock")
        self.assertEqual(steps, ["old lock released", "new lock released", "got the lock"])

    def test_works_after_the_state_folder_is_deleted(self):
        self.throttle.charge("lab")
        shutil.rmtree(self.throttle.dir)
        with self.throttle.hold(self.clock.mono() + 10):
            self.throttle.charge("lab")
            self.throttle.log("lab", "core", 200, 0.1, "GET x")
        self.assertEqual((self.throttle.used("lab"), len(self.throttle.entries())), (1, 1))


class FolderVanishes(ClockCase):
    """Someone deletes the state folder (rm -rf ~/.cache/gh-tollbooth) between two file operations."""

    def vanishing_before(self, fn, times=1, when=lambda *a, **k: True):
        """`fn`, except that the state folder is deleted just before its first `times` matching calls."""
        left = [times]

        def wrapper(*args, **kwargs):
            if left[0] > 0 and when(*args, **kwargs):
                left[0] -= 1
                shutil.rmtree(self.throttle.dir, ignore_errors=True)
            return fn(*args, **kwargs)
        return wrapper

    def test_before_a_lock_file_is_opened(self):
        with mock.patch.object(core, "open", self.vanishing_before(open, times=2), create=True):
            with self.throttle.hold(self.clock.mono() + 10):
                self.throttle.charge("lab")
        self.assertEqual(self.throttle.used("lab"), 1)

    def test_before_the_new_state_file_is_renamed_into_place(self):
        with mock.patch.object(core.os, "replace", self.vanishing_before(os.replace)):
            self.throttle.charge("lab")
        self.assertEqual(self.throttle.used("lab"), 1)
        self.assertEqual([p.name for p in self.throttle.dir.iterdir() if p.suffix == ".tmp"], [])

    def test_before_a_log_line_is_appended(self):
        appending = lambda path, mode="r", *a, **k: mode == "ab"
        with mock.patch.object(core, "open", self.vanishing_before(open, when=appending), create=True):
            self.throttle.log("lab", "core", 200, 0.1, "GET x")
        self.assertEqual(len(self.throttle.entries()), 1)

    def test_macos_creating_a_file_in_a_folder_being_deleted(self):
        # macOS reports this race as EINVAL ("Invalid argument"), not as a missing folder
        failures = [OSError(22, "Invalid argument"), OSError(22, "Invalid argument")]

        def racy_open(*args, **kwargs):
            if failures:
                raise failures.pop()
            return open(*args, **kwargs)
        with mock.patch.object(core, "open", racy_open, create=True):
            with self.throttle.hold(self.clock.mono() + 10):
                pass
        with mock.patch.object(core.tempfile, "mkstemp", side_effect=[OSError(22, "Invalid argument")] * 2
                               + [core.tempfile.mkstemp(dir=self.throttle.dir, prefix="state.json.", suffix=".tmp")]):
            self.throttle.charge("lab")
        self.assertEqual(self.throttle.used("lab"), 1)

    def test_a_log_line_that_cannot_be_written_is_dropped(self):
        # the log is informational: failing to write it must not fail the command
        def no_append(path, mode="r", *args, **kwargs):
            if mode == "ab":
                raise PermissionError(13, "Permission denied")
            return open(path, mode, *args, **kwargs)
        with mock.patch.object(core, "open", no_append, create=True):
            self.throttle.log("lab", "core", 200, 0.1, "GET x")
        state_lock = open(self.throttle.dir / "state.lock", "a+")
        self.addCleanup(state_lock.close)
        fcntl.flock(state_lock, fcntl.LOCK_EX)
        self.throttle.log("lab", "core", 200, 0.1, "GET y")  # gives up waiting for the state lock
        self.assertEqual(self.throttle.entries(), [])


class Spacing(ClockCase):
    def test_gap_is_what_is_left_of_the_buckets_spacing(self):
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
        st = self.throttle.state()
        st["core"] = self.clock.now() + 25  # written before a backward clock step
        self.throttle._save(st)
        self.assertEqual(self.throttle.spacing_gap("core"), 0.0)

    def test_an_unspaced_mark_leaves_no_gap(self):
        self.throttle.mark("graphql", spaced=False)  # a cache hit
        self.assertEqual(self.throttle.spacing_gap("graphql"), 0.0)


class Blocks(ClockCase):
    def test_a_block_is_seen_by_every_agent(self):
        self.assertLessEqual(self.throttle.blocked_for("core"), 0)
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
        self.assertAlmostEqual(self.other_agent().blocked_for("core"), core.BLOCK_SANITY, delta=0.01)
        self.clock.sleep(core.BLOCK_SANITY + 1)
        self.assertLessEqual(self.throttle.blocked_for("core"), 0)

    def test_a_far_future_block_in_the_file_is_ignored_and_overwritten(self):
        # left by a clock step or a reboot: GitHub limits reset hourly, so it can't be real
        self.throttle._save({"blocked:core": self.clock.now() + 7200})
        self.assertEqual(self.throttle.blocked_for("core"), 0.0)
        self.throttle.mark("core", {"core": self.clock.now() + 60})
        self.assertAlmostEqual(self.throttle.blocked_for("core"), 60, delta=0.01)


class Budgets(ClockCase):
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
        self.throttle.charge("other")  # the next charge drops the aged-out label from the file
        self.assertNotIn("lab", self.throttle.state()["charges"])

    def test_a_charge_in_the_future_is_not_counted(self):
        # left by a clock that ran ahead: without an upper limit it would count against the label for a year
        self.clock.wall += 365 * 24 * 3600
        self.throttle.charge("lab")
        self.clock.wall -= 365 * 24 * 3600
        self.assertEqual(self.throttle.used("lab"), 0)
        self.throttle.charge("other")  # the next charge drops it from the file
        self.assertNotIn("lab", self.throttle.state()["charges"])
        self.throttle.charge("lab")
        self.clock.wall -= 1800  # a small step back, into the hour before, keeps a charge just made
        self.assertEqual(self.throttle.used("lab"), 1)

    def test_budgets_do_not_depend_on_the_log(self):
        self.throttle.charge("lab")
        self.throttle.log("lab", "core", 200, 0.1, "GET repos/o/r")
        self.throttle.log_path.write_text("")
        self.assertEqual(self.throttle.used("lab"), 1)


class StateFile(ClockCase):
    def test_a_missing_or_broken_file_reads_as_empty(self):
        self.assertEqual(self.throttle.state(), {})
        self.throttle.state_path.write_text("{not json")
        self.assertEqual(self.throttle.state(), {})
        self.throttle.mark("core")  # and is replaced by a good one
        self.assertEqual(json.loads(self.throttle.state_path.read_text()), {"core": self.clock.now()})

    def test_a_wrong_shaped_file_reads_as_empty_and_is_repaired(self):
        shapes = ["[]", "null", "5", '"x"', '{"blocked:core": "x"}', '{"blocked:core": 1e999}', '{"core": "x"}',
                  '{"core": true}', '{"core": NaN}', '{"charges": [1]}', '{"charges": {"lab": 5}}',
                  '{"charges": {"lab": {"zz": 1}}}', '{"charges": {"lab": {"500000": "many"}}}',
                  '{"charges": {"lab": {"500000": -3, "\u00b2": 1}}}', '{"charges": {"lab": {"500000": true}}}',
                  '{"blocked:core": 1%s}' % ("0" * 400), '{"core": 1%s}' % ("0" * 400)]
        for shape in shapes:
            self.throttle.state_path.write_text(shape)
            self.assertLessEqual(self.throttle.blocked_for("core"), 0, shape)
            self.assertEqual(self.throttle.spacing_gap("core"), 0.0, shape)
            self.assertEqual(self.throttle.used("lab"), 0, shape)
            hour = self.throttle.charge("lab")
            self.assertEqual(self.throttle.used("lab"), 1, shape)
            self.throttle.refund("lab", hour)
            self.throttle.state_path.write_text(shape)
            self.throttle.mark("core", {"core": self.clock.now() + 30})
            self.assertAlmostEqual(self.throttle.blocked_for("core"), 30, delta=0.01, msg=shape)
            self.assertIsInstance(json.loads(self.throttle.state_path.read_text()), dict, shape)

    def test_temp_files_left_by_a_killed_writer_are_removed(self):
        # a writer killed between creating its temp file and renaming it leaves the temp file behind
        d = self.throttle.dir
        for name in ("state.json.dead1.tmp", "calls.log.dead2.tmp", "state.json.live.tmp", "notes.tmp"):
            (d / name).write_text("x")
        an_hour_ago = time.time() - 3600
        for name in ("state.json.dead1.tmp", "calls.log.dead2.tmp", "notes.tmp"):
            os.utime(d / name, (an_hour_ago, an_hour_ago))
        self.throttle.mark("core")
        self.assertEqual(sorted(p.name for p in d.iterdir() if p.suffix == ".tmp"), ["notes.tmp", "state.json.live.tmp"])

    def test_spacing_blocks_and_charges_share_the_file_without_clobbering(self):
        self.throttle.charge("lab")
        self.throttle.mark("core", {"search": self.clock.now() + 30})
        self.throttle.charge("lab")
        st = self.throttle.state()
        self.assertEqual(set(st), {"charges", "core", "blocked:search"})
        self.assertEqual(self.throttle.used("lab"), 2)


class CallLog(ClockCase):
    def test_entries_come_back_as_columns(self):
        self.throttle.log("lab", "search", 200, 1.234, "GET search/repositories?q=x")
        (e,) = self.throttle.entries()
        self.assertEqual(len(e), 7)
        self.assertEqual((e[1], e[3], e[4], e[5], e[6]), ("lab", "search", "200", "1.2", "GET search/repositories?q=x"))

    def test_one_call_is_one_line(self):
        self.throttle.log("lab", "graphql", 200, 0.1, "POST graphql query {\n\tviewer { login }\n}" + "x" * 300)
        (e,) = self.throttle.entries()
        self.assertEqual(len(e), 7)
        self.assertEqual(len(e[6]), 160)
        self.assertTrue(e[6].startswith("POST graphql query { viewer { login } }"))

    def small_log(self):
        """A 10 KB cap and 50 kept lines instead of 2 MB and 5000."""
        for name, value in (("LOG_MAX_BYTES", 10_000), ("LOG_KEEP_LINES", 50)):
            p = mock.patch.object(core, name, value)
            p.start()
            self.addCleanup(p.stop)

    def test_the_log_is_trimmed_to_its_newest_lines(self):
        self.small_log()
        self.throttle.log_path.write_text("".join(f"old\t{i}\n" for i in range(2000)))
        self.throttle.log("lab", "core", 200, 0.1, "GET rate_limit")
        self.assertEqual(len(self.throttle.entries()), 50)
        self.assertEqual(self.throttle.entries()[-1][6], "GET rate_limit")

    def test_a_log_of_few_huge_lines_is_cut_to_whole_lines_under_the_cap(self):
        # otherwise a log that its newest lines can't bring under the cap is rewritten on every call
        self.small_log()
        self.throttle.log_path.write_bytes(b"x" * 25_000 + b"\n")
        self.throttle.log("lab", "core", 200, 0.1, "GET first")
        self.assertEqual([e[6] for e in self.throttle.entries()], ["GET first"])
        inode = self.throttle.log_path.stat().st_ino
        self.throttle.log("lab", "core", 200, 0.1, "GET second")
        self.assertEqual(self.throttle.log_path.stat().st_ino, inode)  # appended to, not rewritten
        self.throttle.log_path.write_bytes(b"x" * 25_000)  # no line break at all
        self.throttle.log("lab", "core", 200, 0.1, "GET third")
        self.assertLess(self.throttle.log_path.stat().st_size, 10_000)

    def test_control_characters_never_come_back_from_the_log(self):
        # another agent's query text must not drive the terminal of whoever runs `status`
        self.throttle.log("lab", "core", 200, 0.1, "GET q=\x1b[2J\x1b]0;title\x07 end\x00\x7f\x9b")
        self.assertNotIn(b"\x1b", self.throttle.log_path.read_bytes())
        with open(self.throttle.log_path, "ab") as f:
            f.write(b"2026\tlab\t1\tcore\t200\t0.1\tGET \x1b[31mred\n")  # from a writer that did not clean it
        first, second = self.throttle.entries()
        self.assertEqual((first[6], second[6]), ("GET q=?[2J?]0;title? end???", "GET ?[31mred"))

    def test_no_log_yet(self):
        self.assertEqual(self.throttle.entries(), [])

    def test_a_foreign_byte_in_the_log_breaks_neither_reading_nor_trimming(self):
        self.throttle.log("lab", "core", 200, 0.1, "GET caf\u00e9")
        with open(self.throttle.log_path, "ab") as f:
            f.write(b"2026\tlab\t1\tcore\t200\t0.1\tGET caf\xe9\n")  # Latin-1, from some other writer
        self.assertEqual([len(e) for e in self.throttle.entries()], [7, 7])
        self.assertEqual(self.throttle.entries()[0][6], "GET caf\u00e9")
        self.small_log()
        with open(self.throttle.log_path, "ab") as f:
            f.write(b"".join(b"old\t%d\n" % i for i in range(2000)) + b"2026\tlab\t1\tcore\t200\t0.1\tGET caf\xe9\n")
        self.throttle.log("lab", "core", 200, 0.1, "GET after")
        entries = self.throttle.entries()
        self.assertEqual((len(entries), entries[-2][6], entries[-1][6]), (50, "GET caf\ufffd", "GET after"))


class Deadline(unittest.TestCase):
    def test_a_value_that_is_not_whole_seconds_is_ignored_with_one_line(self):
        for bad in ("150.5", "soon", "0", "-5", "9" * 30):
            err = io.StringIO()
            with mock.patch.dict(os.environ, {"TOLLBOOTH_DEADLINE": bad}), contextlib.redirect_stderr(err):
                self.assertEqual(core._env_seconds("TOLLBOOTH_DEADLINE", 100), 100, bad)
            self.assertEqual(len(err.getvalue().splitlines()), 1, bad)
            self.assertIn("TOLLBOOTH_DEADLINE", err.getvalue())

    def test_a_whole_number_is_used_and_an_unset_value_is_the_default(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            with mock.patch.dict(os.environ, {"TOLLBOOTH_DEADLINE": "45"}):
                self.assertEqual(core._env_seconds("TOLLBOOTH_DEADLINE", 100), 45)
            with mock.patch.dict(os.environ, {"TOLLBOOTH_DEADLINE": ""}):
                self.assertEqual(core._env_seconds("TOLLBOOTH_DEADLINE", 100), 100)
        self.assertEqual(err.getvalue(), "")


class ManyProcesses(unittest.TestCase):
    """Real processes, real clock: the state writers are safe with or without the big lock."""

    WORKER = """
import sys, time
sys.path.insert(0, sys.argv[1])
import tollbooth_core as core
t = core.Throttle(sys.argv[2])
for _ in range(40):
    if sys.argv[3] == "hold":
        with t.hold(time.monotonic() + 60):
            hour = t.charge("lab"); t.charge("lab"); t.refund("lab", hour); t.mark("core"); t.log("lab", "core", 200, 0.1, "GET x")
    else:
        hour = t.charge("lab"); t.charge("lab"); t.refund("lab", hour); t.mark("core"); t.log("lab", "core", 200, 0.1, "GET x")
"""

    def run_workers(self, mode):
        with tempfile.TemporaryDirectory() as d:
            procs = [subprocess.Popen([sys.executable, "-c", self.WORKER, str(SCRIPTS), d, mode],
                                      stderr=subprocess.PIPE, text=True) for _ in range(6)]
            errors = [p.communicate()[1][-300:] for p in procs]
            self.assertEqual([p.returncode for p in procs], [0] * 6, errors)
            t = core.Throttle(d)
            self.assertEqual((t.used("lab"), len(t.entries())), (240, 240))
            self.assertEqual([f.name for f in Path(d).iterdir() if f.name.endswith(".tmp")], [])

    def test_writers_outside_the_lock_lose_nothing(self):
        self.run_workers("free")

    def test_writers_inside_the_lock_lose_nothing(self):
        self.run_workers("hold")

    TRIMMING_WORKER = """
import sys
sys.path.insert(0, sys.argv[1])
import tollbooth_core as core
core.LOG_MAX_BYTES = 0  # every line starts a trim...
t = core.Throttle(sys.argv[2])
t._trim_log = lambda: t._replace(t.log_path, t.log_path.read_bytes())  # ...that keeps every line
for i in range(50):
    t.log("lab", "core", 200, 0.1, "GET %d" % i)
"""

    def test_lines_appended_while_another_agent_trims_are_kept(self):
        with tempfile.TemporaryDirectory() as d:
            procs = [subprocess.Popen([sys.executable, "-c", self.TRIMMING_WORKER, str(SCRIPTS), d],
                                      stderr=subprocess.PIPE, text=True) for _ in range(6)]
            errors = [p.communicate()[1][-300:] for p in procs]
            self.assertEqual([p.returncode for p in procs], [0] * 6, errors)
            self.assertEqual(len(core.Throttle(d).entries()), 300)


if __name__ == "__main__":
    unittest.main()
