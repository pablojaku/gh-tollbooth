"""tollbooth_core: what every gh-tollbooth module shares. Stdlib only, Python 3.9+.

- settings: the state folder, each bucket's call spacing, the per-call deadline;
- exit codes and TollboothError, with the messages for "busy" (76) and "rate-limited" (75);
- infer_bucket(): which of GitHub's rate-limit counters a path is counted against;
- the clock seam: Clock, with SystemClock in production and a fake in the tests.

A bucket is one of GitHub's separate rate-limit counters (GitHub says "resource"). This tool uses
four: graphql, core, search and code_search. The shared state itself (lock, blocks, budgets, call
log) and the rules for one turn at GitHub are in tollbooth_throttle.

Times shared between processes use the wall clock (Clock.now), since a monotonic clock is not the
same in every process. A caller's own deadline uses the monotonic clock (Clock.mono).
"""
from __future__ import annotations

import datetime as dt
import os
import re
import sys
import time
from pathlib import Path
from typing import Protocol


def eprint(*a):
    print(*a, file=sys.stderr, flush=True)


def env_seconds(name: str, default: int) -> int:
    """A whole number of seconds (1 or more) from the environment; anything else is ignored with one line."""
    value = os.environ.get(name, "")
    if re.fullmatch(r"[1-9][0-9]{0,5}", value):
        return int(value)
    if value:
        eprint(f"gh-tollbooth: ignoring {name}={value[:20]!r} (not a whole number of seconds); using {default}")
    return default


STATE_DIR = Path(os.environ.get("TOLLBOOTH_STATE_DIR", str(Path.home() / ".cache" / "gh-tollbooth")))
SPACING = {"graphql": 1.0, "core": 0.5, "search": 2.2, "code_search": 6.5}  # seconds between calls, per bucket
DEADLINE = env_seconds("TOLLBOOTH_DEADLINE", 100)   # per GitHub call; under the Bash tool's 120 s default
LABEL_RE = re.compile(r"[A-Za-z0-9_.-]{1,40}")  # always use .fullmatch: "$" would accept a trailing newline
BLOCK_SANITY = 3700      # seconds; GitHub limits reset hourly, so a longer block is capped or stale
# 1 = no token, no answer from GitHub, or no state folder; 75 = GitHub rate limit (stop, report the reset time);
# 76 = gh-tollbooth busy or out of time (retry once later)
EX_FAIL, EX_USAGE, EX_HEAVY, EX_BUDGET, EX_LIMITED, EX_BUSY = 1, 2, 3, 4, 75, 76


class TollboothError(Exception):
    def __init__(self, msg: str, code: int):
        super().__init__(msg)
        self.code = code


def infer_bucket(path: str) -> str:
    """GitHub's rate-limit bucket for a relative API path. GitHub routes `SEARCH/x` like `search/x`."""
    ep = path.lstrip("/").lower()
    if ep == "graphql":
        return "graphql"
    if ep.startswith("search/code"):
        return "code_search"
    return "search" if ep.startswith("search/") else "core"


# ---------- the clock seam ----------

class Clock(Protocol):
    """Where gh-tollbooth gets the time. SystemClock in production; tests pass a fake that never sleeps."""

    def now(self) -> float:
        """Wall clock: every time shared between processes (state.json), budgets, logs."""
        ...

    def mono(self) -> float:
        """Process-local monotonic clock: only this process's own deadline, so clock steps can't bend it."""
        ...

    def sleep(self, seconds: float) -> None:
        ...


class SystemClock:
    def now(self) -> float:
        return time.time()

    def mono(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        time.sleep(max(0.0, seconds))


SYSTEM_CLOCK = SystemClock()


def clock_in(seconds: float, clock: Clock) -> str:
    """Wall-clock time `seconds` from now, for messages."""
    return dt.datetime.fromtimestamp(clock.now() + seconds).strftime("%H:%M:%S")


def busy(why: str) -> TollboothError:
    return TollboothError(f"{why}; gave up so this command doesn't hang (TOLLBOOTH_DEADLINE {DEADLINE} s). "
                          "gh-tollbooth is busy, not rate-limited: wait a minute and retry once.", EX_BUSY)


def limited(what: str, seconds: float, clock: Clock) -> TollboothError:
    return TollboothError(f"{what} is rate-limited until {clock_in(seconds, clock)}. Stop and report; "
                          "do not retry in a loop.", EX_LIMITED)
