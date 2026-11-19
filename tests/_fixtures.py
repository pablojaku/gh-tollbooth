"""Shared test fixtures: canned GitHub answers and a fake clock (wall + monotonic)."""
import importlib.machinery
import importlib.util
import sys
from pathlib import Path
from typing import Optional

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))
import tollbooth_http as http  # noqa: E402  # pyright: ignore[reportMissingImports]

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
    """Wall and monotonic clocks that advance together on sleep(); wall can be stepped independently."""

    def __init__(self):
        self.wall, self.mono_t = WALL0, MONO0
        self.sleeps = []

    def now(self):
        return self.wall

    def mono(self):
        return self.mono_t

    def sleep(self, s):
        s = max(0.0, s)
        self.sleeps.append(round(s, 3))
        self.wall += s
        self.mono_t += s
