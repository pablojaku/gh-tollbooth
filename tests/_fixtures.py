"""Shared test fixtures: canned GitHub answers, settings and a fake clock. pytest puts scripts/ on the import
path (pyproject.toml)."""
from __future__ import annotations

import contextlib
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest import mock

import tollbooth_core as core
import tollbooth_http as http

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
WALL0 = 1_800_000_000.0
MONO0 = 10_000.0


def answer(status: int, headers: dict | None = None, body: bytes = b"{}") -> http.Response:
    """A GitHub answer as tollbooth_http.send returns it: header names in lower case."""
    return http.Response(status, {k.lower(): str(v) for k, v in (headers or {}).items()}, body)


def primary(reset: float, status: int = 403, resource: str = "core") -> http.Response:
    return answer(status, {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": int(reset),
                           "X-RateLimit-Resource": resource})


def secondary(retry_after: int | None = None, status: int = 429) -> http.Response:
    return answer(status, {"Retry-After": retry_after} if retry_after is not None else {})


@contextlib.contextmanager
def clean_env(**env: str) -> Iterator[None]:
    """os.environ without this machine's TOLLBOOTH_* variables, plus TOLLBOOTH_<NAME> for each of `env`."""
    clean = {k: v for k, v in os.environ.items() if not k.upper().startswith("TOLLBOOTH_")}
    clean.update({f"TOLLBOOTH_{k.upper()}": v for k, v in env.items()})
    with mock.patch.dict(os.environ, clean, clear=True):
        yield


def settings(**fields: Any) -> core.Settings:
    """Settings with `fields` set and the rest at their defaults, whatever TOLLBOOTH_* this machine has."""
    with clean_env():
        return core.Settings(**fields)


class FakeClock:
    """Wall and monotonic clocks that move only when a test says so: sleep() moves both and records how
    long it slept; `wall` can be set alone, like a wall-clock step."""

    def __init__(self):
        self.wall, self.mono_t = WALL0, MONO0
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self.wall

    def mono(self) -> float:
        return self.mono_t

    def sleep(self, seconds: float) -> None:
        seconds = max(0.0, seconds)
        self.sleeps.append(round(seconds, 3))
        self.wall += seconds
        self.mono_t += seconds
