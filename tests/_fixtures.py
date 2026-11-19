"""Shared test fixtures: canned GitHub answers. pytest puts scripts/ on the import path (pyproject.toml)."""
from __future__ import annotations

from pathlib import Path

import tollbooth_http as http

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
WALL0 = 1_800_000_000.0


def answer(status: int, headers: dict | None = None, body: bytes = b"{}") -> http.Response:
    """A GitHub answer as tollbooth_http.send returns it: header names in lower case."""
    return http.Response(status, {k.lower(): str(v) for k, v in (headers or {}).items()}, body)


def primary(reset: float, status: int = 403, resource: str = "core") -> http.Response:
    return answer(status, {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": int(reset),
                           "X-RateLimit-Resource": resource})


def secondary(retry_after: int | None = None, status: int = 429) -> http.Response:
    return answer(status, {"Retry-After": retry_after} if retry_after is not None else {})


class FakeClock:
    """A wall clock that moves only when a test says so: sleep() moves it on, `wall` can be set."""

    def __init__(self):
        self.wall = WALL0

    def now(self) -> float:
        return self.wall

    def sleep(self, seconds: float) -> None:
        self.wall += max(0.0, seconds)
