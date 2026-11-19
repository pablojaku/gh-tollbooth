"""Shared-state tests: settings, the vanished-folder retry and the machine-wide lock.

The lock tests use real time with short deadlines. Another agent is a second open file on the lock in
this process: an flock lock belongs to an open file, so it contends exactly as another process would.
Run: uv run pytest
"""
import errno
import fcntl
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import tollbooth_core as core
import tollbooth_throttle as throttling
from filelock import FileLock, Timeout


class Settings(unittest.TestCase):
    """TOLLBOOTH_* settings: a bad value is ignored with one line on stderr, and the default is used."""

    def settings(self, **env):
        """Settings read from exactly `env`: the TOLLBOOTH_* variables of this machine don't leak in."""
        clean = {k: v for k, v in os.environ.items() if not k.startswith("TOLLBOOTH_")}
        clean.update({f"TOLLBOOTH_{k.upper()}": v for k, v in env.items()})
        with mock.patch.dict(os.environ, clean, clear=True):
            return core.Settings()

    def test_the_state_dir_is_used_only_if_it_is_an_absolute_path(self):
        default = Path.home() / ".cache" / "gh-tollbooth"
        for value, want in (("/srv/tb", Path("/srv/tb")), ("~/tb", Path.home() / "tb"), ("", default)):
            with self.assertNoLogs("gh-tollbooth"):
                self.assertEqual(self.settings(state_dir=value).state_dir, want, value)
        for value in ("tb", "./tb"):
            with self.assertLogs("gh-tollbooth", "WARNING") as logs:
                self.assertEqual(self.settings(state_dir=value).state_dir, default, value)
            self.assertEqual(len(logs.output), 1, value)
            self.assertIn("not an absolute path", logs.output[0])

    def test_a_deadline_that_is_not_whole_seconds_is_ignored_with_one_line(self):
        for bad in ("150.5", "soon", "0", "-5", "9" * 30, " 45", "045", "4_5", "45.0", "1000000"):
            with self.assertLogs("gh-tollbooth", "WARNING") as logs:
                self.assertEqual(self.settings(deadline=bad).deadline, 100, bad)
            self.assertEqual(len(logs.output), 1, bad)
            self.assertIn("TOLLBOOTH_DEADLINE", logs.output[0])

    def test_a_whole_number_is_used_and_an_unset_value_is_the_default(self):
        with self.assertNoLogs("gh-tollbooth"):
            self.assertEqual(self.settings(deadline="45").deadline, 45)
            self.assertEqual(self.settings(deadline="999999").deadline, 999_999)
            self.assertEqual(self.settings(deadline="").deadline, 100)

    def test_the_line_on_stderr(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith("TOLLBOOTH_")}
        env.update(TOLLBOOTH_DEADLINE="soon", PYTHONPATH=str(Path(core.__file__).parent))
        p = subprocess.run([sys.executable, "-c", "import tollbooth_core"], env=env, capture_output=True, text=True)
        self.assertEqual(p.stderr, "gh-tollbooth: ignoring TOLLBOOTH_DEADLINE='soon' "
                                   "(not a whole number of seconds); using 100\n")


class LockCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name) / "state"
        self.throttle = throttling.Throttle(self.state)

    def held_elsewhere(self):
        """The lock, taken through a second open file, as another agent's process would."""
        holder = open(self.throttle.lock_path, "a+")
        self.addCleanup(holder.close)
        fcntl.flock(holder, fcntl.LOCK_EX)
        return holder

    def is_free(self):
        probe = FileLock(self.throttle.lock_path, thread_local=False)
        try:
            probe.acquire(blocking=False)
        except Timeout:
            return False
        probe.release()
        return True


class Lock(LockCase):
    def test_only_one_holder_at_a_time(self):
        with self.throttle.hold(time.monotonic() + 10):
            self.assertFalse(self.is_free())
        self.assertTrue(self.is_free())

    def test_released_when_the_holder_fails(self):
        with self.assertRaises(RuntimeError):
            with self.throttle.hold(time.monotonic() + 10):
                raise RuntimeError("boom")
        self.assertTrue(self.is_free())

    def test_waiting_gives_up_busy_at_the_deadline(self):
        self.held_elsewhere()
        start = time.monotonic()
        with self.assertNoLogs("gh-tollbooth"), self.assertRaises(core.TollboothError) as cm:
            with self.throttle.hold(start + 0.5):
                self.fail("got a lock that is held")
        self.assertEqual(cm.exception.code, core.EX_BUSY)
        self.assertAlmostEqual(time.monotonic() - start, 0.5, delta=0.3)

    def test_waiter_gets_in_once_the_lock_is_released_and_says_it_waited(self):
        holder = self.held_elsewhere()
        release = threading.Timer(0.6, fcntl.flock, (holder, fcntl.LOCK_UN))
        start = time.monotonic()
        release.start()
        with mock.patch.object(throttling, "WAIT_NOTICE", 0.2), self.assertLogs("gh-tollbooth", "INFO") as logs:
            with self.throttle.hold(start + 10):
                waited = time.monotonic() - start
        release.join()
        self.assertGreaterEqual(waited, 0.6)
        self.assertEqual(len(logs.output), 1)
        self.assertIn("waiting for another agent", logs.output[0])

    def test_a_waiter_follows_the_lock_file_when_it_is_replaced(self):
        # Someone replaces the lock file while we wait on it, and a third agent holds the new one.
        # Getting the old, replaced file's lock must not count as holding the lock. The swap is one
        # rename: a moment with no lock file would let the waiter make one and lock it (the known limit).
        old = self.held_elsewhere()
        steps = []

        def replace():
            time.sleep(0.2)
            spare = self.throttle.lock_path.with_name("lock.new")
            with open(spare, "a+") as new:
                fcntl.flock(new, fcntl.LOCK_EX)
                os.replace(spare, self.throttle.lock_path)
                fcntl.flock(old, fcntl.LOCK_UN)
                steps.append("old lock released")
                time.sleep(0.5)
                steps.append("new lock released")
        third = threading.Thread(target=replace)
        third.start()
        with self.throttle.hold(time.monotonic() + 10):
            steps.append("got the lock")
        third.join()
        self.assertEqual(steps, ["old lock released", "new lock released", "got the lock"])

    def test_a_deleted_state_folder_is_made_again(self):
        shutil.rmtree(self.state)
        with self.throttle.hold(time.monotonic() + 10):
            self.assertTrue(self.throttle.lock_path.exists())


class InFolder(unittest.TestCase):
    """_in_folder: a file step whose folder vanishes is retried, FS_RETRIES times at most."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.folder = Path(tmp.name) / "state"

    def step(self, *errors):
        """A file step that raises `errors` one by one, then returns "done"; and the list of its calls."""
        calls = []

        def run():
            calls.append(1)
            if len(calls) <= len(errors):
                raise errors[len(calls) - 1]
            return "done"
        return run, calls

    def test_a_vanished_folder_is_made_again_and_the_step_tried_again(self):
        for code in throttling.VANISHED:
            run, calls = self.step(OSError(code, "gone"), OSError(code, "gone"))
            self.assertEqual(throttling._in_folder(self.folder, run), "done")
            self.assertEqual(len(calls), 3, code)
            self.assertTrue(self.folder.is_dir())

    def test_any_other_error_goes_to_the_caller_at_once(self):
        run, calls = self.step(PermissionError(errno.EACCES, "no"))
        with self.assertRaises(PermissionError):
            throttling._in_folder(self.folder, run)
        self.assertEqual(len(calls), 1)

    def test_the_last_error_goes_to_the_caller_after_fs_retries_tries(self):
        run, calls = self.step(*[FileNotFoundError(errno.ENOENT, "gone")] * 10)
        with self.assertRaises(FileNotFoundError):
            throttling._in_folder(self.folder, run)
        self.assertEqual(len(calls), throttling.FS_RETRIES)


if __name__ == "__main__":
    unittest.main()
