"""Shared-state tests: settings read from the environment.

Run: uv run pytest
"""
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

import tollbooth_core as core


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


if __name__ == "__main__":
    unittest.main()
