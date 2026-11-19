"""Shared-file tests: the state folder can vanish, state.json can be broken, calls.log is trimmed,
and many processes write at once without losing anything.

Mock-first: the Throttle is given a FakeClock (ManyProcesses uses real processes and the real clock).
Races reach the state store's private writers, the module's own seam; rules are in test_throttle.
Run: python3 -m unittest discover -s tests -v
"""
import fcntl
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

from _fixtures import SCRIPTS, ThrottleCase, logged
import tollbooth_throttle as throttling  # noqa: E402  # pyright: ignore[reportMissingImports]


class FolderVanishes(ThrottleCase):
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
        with mock.patch.object(throttling, "open", self.vanishing_before(open, times=2), create=True):
            self.turn()
        self.assertEqual(self.throttle.used("lab"), 1)

    def test_before_the_new_state_file_is_renamed_into_place(self):
        with mock.patch.object(throttling.os, "replace", self.vanishing_before(os.replace)):
            self.turn()
        self.assertEqual(self.throttle.used("lab"), 1)
        self.assertEqual([p.name for p in self.throttle.dir.iterdir() if p.suffix == ".tmp"], [])

    def test_before_a_log_line_is_appended(self):
        appending = lambda path, mode="r", *a, **k: mode == "ab"
        with mock.patch.object(throttling, "open", self.vanishing_before(open, when=appending), create=True):
            self.turn()
        self.assertEqual(len(self.throttle.entries()), 1)

    def test_macos_creating_a_file_in_a_folder_being_deleted(self):
        # macOS reports this race as EINVAL ("Invalid argument"), not as a missing folder
        failures = [OSError(22, "Invalid argument"), OSError(22, "Invalid argument")]

        def racy_open(*args, **kwargs):
            if failures:
                raise failures.pop()
            return open(*args, **kwargs)
        good = throttling.tempfile.mkstemp(dir=self.throttle.dir, prefix="state.json.", suffix=".tmp")
        with mock.patch.object(throttling, "open", racy_open, create=True), \
                mock.patch.object(throttling.tempfile, "mkstemp", side_effect=[OSError(22, "Invalid argument")] * 2
                                  + [good] + [throttling.tempfile.mkstemp(dir=self.throttle.dir, suffix=".tmp")]):
            self.turn()
        self.assertEqual(self.throttle.used("lab"), 1)

    def test_a_log_line_that_cannot_be_written_is_dropped(self):
        # the log is informational: failing to write it must not fail the command
        def no_append(path, mode="r", *args, **kwargs):
            if mode == "ab":
                raise PermissionError(13, "Permission denied")
            return open(path, mode, *args, **kwargs)
        with mock.patch.object(throttling, "open", no_append, create=True):
            self.assertEqual(self.turn(), "answer")
        state_lock = open(self.throttle.dir / "state.lock", "a+")
        self.addCleanup(state_lock.close)
        fcntl.flock(state_lock, fcntl.LOCK_EX)
        logged(self.throttle, "lab", "core", 200, 0.1, "GET y")  # gives up waiting for the state lock
        self.assertEqual(self.throttle.entries(), [])


class StateFile(ThrottleCase):
    def test_a_missing_or_broken_file_reads_as_empty(self):
        self.assertEqual(self.throttle.state(), {})
        self.throttle.state_path.write_text("{not json")
        self.assertEqual(self.throttle.state(), {})
        self.turn()  # and is replaced by a good one
        self.assertEqual(json.loads(self.throttle.state_path.read_text())["core"], self.clock.now())

    def test_a_wrong_shaped_file_reads_as_empty_and_is_repaired(self):
        shapes = ["[]", "null", "5", '"x"', '{"blocked:core": "x"}', '{"blocked:core": 1e999}', '{"core": "x"}',
                  '{"core": true}', '{"core": NaN}', '{"charges": [1]}', '{"charges": {"lab": 5}}',
                  '{"charges": {"lab": {"zz": 1}}}', '{"charges": {"lab": {"500000": "many"}}}',
                  '{"charges": {"lab": {"500000": -3, "²": 1}}}', '{"charges": {"lab": {"500000": true}}}',
                  '{"blocked:core": 1%s}' % ("0" * 400), '{"core": 1%s}' % ("0" * 400)]
        for shape in shapes:
            self.throttle.state_path.write_text(shape)
            self.clock.sleeps.clear()
            self.assertLessEqual(self.throttle.blocked_for("core"), 0, shape)
            self.throttle.wait_turn("core", self.clock.mono() + 100)
            self.assertEqual(self.clock.sleeps, [], shape)  # no call gap either
            self.assertEqual(self.throttle.used("lab"), 0, shape)
            self.turn(bucket="graphql")
            self.assertEqual(self.throttle.used("lab"), 1, shape)
            self.throttle.state_path.write_text(shape)
            self.turn(bucket="graphql", status=403, blocks={"core": 30})
            self.assertAlmostEqual(self.throttle.blocked_for("core"), 30, delta=0.01, msg=shape)
            self.assertIsInstance(json.loads(self.throttle.state_path.read_text()), dict, shape)
            self.clock.sleep(31)

    def test_temp_files_left_by_a_killed_writer_are_removed(self):
        # a writer killed between creating its temp file and renaming it leaves the temp file behind
        d = self.throttle.dir
        for name in ("state.json.dead1.tmp", "calls.log.dead2.tmp", "state.json.live.tmp", "notes.tmp"):
            (d / name).write_text("x")
        an_hour_ago = time.time() - 3600
        for name in ("state.json.dead1.tmp", "calls.log.dead2.tmp", "notes.tmp"):
            os.utime(d / name, (an_hour_ago, an_hour_ago))
        self.turn()
        left = sorted(p.name for p in d.iterdir() if p.suffix == ".tmp")
        self.assertEqual(left, ["notes.tmp", "state.json.live.tmp"])

    def test_spacing_blocks_and_charges_share_the_file_without_clobbering(self):
        self.turn(status=403, blocks={"search": 30})
        self.turn()
        st = self.throttle.state()
        self.assertEqual(set(st), {"charges", "core", "blocked:search"})
        self.assertEqual(self.throttle.used("lab"), 2)


class CallLog(ThrottleCase):
    def test_entries_come_back_as_columns(self):
        self.turn(bucket="search", summary="GET search/repositories?q=x", secs=1.234)
        (e,) = self.throttle.entries()
        self.assertEqual(len(e), 7)
        self.assertEqual((e[1], e[3], e[4], e[5], e[6]), ("lab", "search", "200", "1.2", "GET search/repositories?q=x"))

    def test_one_call_is_one_line(self):
        self.turn(bucket="graphql", summary="POST graphql query {\n\tviewer { login }\n}" + "x" * 300)
        (e,) = self.throttle.entries()
        self.assertEqual(len(e), 7)
        self.assertEqual(len(e[6]), 160)
        self.assertTrue(e[6].startswith("POST graphql query { viewer { login } }"))

    def small_log(self):
        """A 10 KB cap and 50 kept lines instead of 2 MB and 5000."""
        for name, value in (("LOG_MAX_BYTES", 10_000), ("LOG_KEEP_LINES", 50)):
            p = mock.patch.object(throttling, name, value)
            p.start()
            self.addCleanup(p.stop)

    def test_the_log_is_trimmed_to_its_newest_lines(self):
        self.small_log()
        self.throttle.log_path.write_text("".join(f"old\t{i}\n" for i in range(2000)))
        self.turn(summary="GET rate_limit")
        self.assertEqual(len(self.throttle.entries()), 50)
        self.assertEqual(self.throttle.entries()[-1][6], "GET rate_limit")

    def test_a_log_of_few_huge_lines_is_cut_to_whole_lines_under_the_cap(self):
        # otherwise a log that its newest lines can't bring under the cap is rewritten on every call
        self.small_log()
        self.throttle.log_path.write_bytes(b"x" * 25_000 + b"\n")
        self.turn(summary="GET first")
        self.assertEqual([e[6] for e in self.throttle.entries()], ["GET first"])
        inode = self.throttle.log_path.stat().st_ino
        self.turn(summary="GET second")
        self.assertEqual(self.throttle.log_path.stat().st_ino, inode)  # appended to, not rewritten
        self.throttle.log_path.write_bytes(b"x" * 25_000)  # no line break at all
        self.turn(summary="GET third")
        self.assertLess(self.throttle.log_path.stat().st_size, 10_000)

    def test_control_characters_never_come_back_from_the_log(self):
        # another agent's query text must not drive the terminal of whoever runs `status`
        self.turn(summary="GET q=\x1b[2J\x1b]0;title\x07 end\x00\x7f\x9b")
        self.assertNotIn(b"\x1b", self.throttle.log_path.read_bytes())
        with open(self.throttle.log_path, "ab") as f:
            f.write(b"2026\tlab\t1\tcore\t200\t0.1\tGET \x1b[31mred\n")  # from a writer that did not clean it
        first, second = self.throttle.entries()
        self.assertEqual((first[6], second[6]), ("GET q=?[2J?]0;title? end???", "GET ?[31mred"))

    def test_no_log_yet(self):
        self.assertEqual(self.throttle.entries(), [])

    def test_a_foreign_byte_in_the_log_breaks_neither_reading_nor_trimming(self):
        self.turn(summary="GET café")
        with open(self.throttle.log_path, "ab") as f:
            f.write(b"2026\tlab\t1\tcore\t200\t0.1\tGET caf\xe9\n")  # Latin-1, from some other writer
        self.assertEqual([len(e) for e in self.throttle.entries()], [7, 7])
        self.assertEqual(self.throttle.entries()[0][6], "GET café")
        self.small_log()
        with open(self.throttle.log_path, "ab") as f:
            f.write(b"".join(b"old\t%d\n" % i for i in range(2000)) + b"2026\tlab\t1\tcore\t200\t0.1\tGET caf\xe9\n")
        self.turn(summary="GET after")
        entries = self.throttle.entries()
        self.assertEqual((len(entries), entries[-2][6], entries[-1][6]), (50, "GET caf�", "GET after"))


class ManyProcesses(unittest.TestCase):
    """Real processes, real clock: the state writers are safe with or without the big lock."""

    WORKER = """
import sys, time
sys.path.insert(0, sys.argv[1])
import tollbooth_throttle as throttling
t = throttling.Throttle(sys.argv[2])
for _ in range(40):
    if sys.argv[3] == "hold":
        with t._hold(time.monotonic() + 60):
            t._charge("lab"); t._charge("lab"); t._mark("core"); t._log("lab", "core", 200, 0.1, "GET x")
    else:
        t._charge("lab"); t._charge("lab"); t._mark("core"); t._log("lab", "core", 200, 0.1, "GET x")
"""

    def run_workers(self, mode):
        with tempfile.TemporaryDirectory() as d:
            procs = [subprocess.Popen([sys.executable, "-c", self.WORKER, str(SCRIPTS), d, mode],
                                      stderr=subprocess.PIPE, text=True) for _ in range(6)]
            errors = [p.communicate()[1][-300:] for p in procs]
            self.assertEqual([p.returncode for p in procs], [0] * 6, errors)
            t = throttling.Throttle(Path(d))
            self.assertEqual((t.used("lab"), len(t.entries())), (480, 240))
            self.assertEqual([f.name for f in Path(d).iterdir() if f.name.endswith(".tmp")], [])

    def test_writers_outside_the_lock_lose_nothing(self):
        self.run_workers("free")

    def test_writers_inside_the_lock_lose_nothing(self):
        self.run_workers("hold")

    TRIMMING_WORKER = """
import sys
sys.path.insert(0, sys.argv[1])
import tollbooth_throttle as throttling
throttling.LOG_MAX_BYTES = 0  # every line starts a trim...
t = throttling.Throttle(sys.argv[2])
t._trim_log = lambda: t._replace(t.log_path, t.log_path.read_bytes())  # ...that keeps every line
for i in range(50):
    t._log("lab", "core", 200, 0.1, "GET %d" % i)
"""

    def test_lines_appended_while_another_agent_trims_are_kept(self):
        with tempfile.TemporaryDirectory() as d:
            procs = [subprocess.Popen([sys.executable, "-c", self.TRIMMING_WORKER, str(SCRIPTS), d],
                                      stderr=subprocess.PIPE, text=True) for _ in range(6)]
            errors = [p.communicate()[1][-300:] for p in procs]
            self.assertEqual([p.returncode for p in procs], [0] * 6, errors)
            self.assertEqual(len(throttling.Throttle(Path(d)).entries()), 300)


if __name__ == "__main__":
    unittest.main()
