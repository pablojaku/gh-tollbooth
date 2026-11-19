"""Shared test fixtures: canned GitHub answers, a fake clock (wall + monotonic) and another agent's footprints."""
import fcntl
import importlib.machinery
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Callable, Dict, List, Optional

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))
import tollbooth_core as core  # noqa: E402  # pyright: ignore[reportMissingImports]
import tollbooth_http as http  # noqa: E402  # pyright: ignore[reportMissingImports]
import tollbooth_throttle as throttling  # noqa: E402  # pyright: ignore[reportMissingImports]

TOKEN = "ghx_FAKEtokenFAKEtoken"
WALL0 = 1_800_000_000.0
MONO0 = 10_000.0


def load_cli(name: str):
    """Import scripts/gh-tollbooth (no .py suffix) as a module."""
    loader = importlib.machinery.SourceFileLoader(name, str(SCRIPTS / "gh-tollbooth"))
    spec = importlib.util.spec_from_loader(name, loader)
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def answer(status: int, headers: Optional[dict] = None, body: bytes = b"{}") -> http.Response:
    """A GitHub answer as tollbooth_http.send returns it: header names in lower case."""
    return http.Response(status, {k.lower(): str(v) for k, v in (headers or {}).items()}, body)


def primary(reset: float, status: int = 403, resource: str = "core") -> http.Response:
    return answer(status, {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": int(reset),
                           "X-RateLimit-Resource": resource})


def secondary(retry_after: Optional[int] = None, status: int = 429) -> http.Response:
    return answer(status, {"Retry-After": retry_after} if retry_after is not None else {})


class FakeClock:
    """The test adapter at the clock seam (tollbooth_core.Clock): wall and monotonic clocks that advance
    together on sleep(), which returns at once. The wall clock can be stepped on its own.

    on_sleep, if set, runs after each sleep: a test uses it to act while the code under test waits
    (release a lock, record a block, check whether the lock is free).
    """

    def __init__(self):
        self.wall, self.mono_t = WALL0, MONO0
        self.sleeps: List[float] = []
        self.on_sleep: Optional[Callable[[float], None]] = None

    def now(self) -> float:
        return self.wall

    def mono(self) -> float:
        return self.mono_t

    def sleep(self, seconds: float) -> None:
        s = max(0.0, seconds)
        self.sleeps.append(round(s, 3))
        self.wall += s
        self.mono_t += s
        if self.on_sleep is not None:
            self.on_sleep(s)


# What another agent's finished call leaves in the shared files. Arranging state this way reaches
# into the throttle's private writers on purpose: it is setup, never what a test checks.

def recorded(throttle, bucket: str, blocks: Optional[Dict[str, float]] = None) -> None:
    """Another agent's call in `bucket` (its time, for spacing) and the blocks {bucket: seconds from now}."""
    now = throttle.clock.now()
    throttle._mark(bucket, {b: now + s for b, s in (blocks or {}).items()})


def logged(throttle, label: str, bucket: str, status: int, secs: float, summary: str) -> None:
    """Another agent's line in the call log."""
    throttle._log(label, bucket, status, secs, summary)


def lock_is_free(throttle) -> bool:
    """True if nobody holds the machine-wide lock right now."""
    with open(throttle.lock_path, "a+") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        fcntl.flock(fh, fcntl.LOCK_UN)
        return True


def hold_lock_elsewhere(case: unittest.TestCase, throttle):
    """Take the machine-wide lock as another agent would, until the test ends. Returns the open file."""
    holder = open(throttle.lock_path, "a+")
    fcntl.flock(holder, fcntl.LOCK_EX)
    case.addCleanup(holder.close)
    return holder


class ThrottleCase(unittest.TestCase):
    """A Throttle on a temporary state folder, with a FakeClock."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clock = FakeClock()
        self.throttle = self.other_agent()

    def other_agent(self):
        """A Throttle on the shared state folder, as another process would have."""
        return throttling.Throttle(Path(self.tmp.name) / "state", clock=self.clock)

    def turn(self, throttle=None, label="lab", bucket="core", status=200, blocks=None, budget=None,
             summary=None, secs=0.0, send=None):
        """One turn, whose request answers `status` and asks for `blocks` {bucket: seconds}."""
        def answered(left):
            self.clock.mono_t += secs  # the request takes `secs`
            return "answer", status, blocks or {}
        return (throttle or self.throttle).attempt(label, budget, bucket, self.clock.mono() + 100,
                                                   summary or f"GET {bucket}", send or answered)

    def refused(self, code, fn, *args, **kwargs):
        with self.assertRaises(core.TollboothError) as cm:
            fn(*args, **kwargs)
        self.assertEqual(cm.exception.code, code)
        return cm.exception
