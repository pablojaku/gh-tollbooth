"""tollbooth_core: what every gh-tollbooth module shares: settings, exit codes and errors.

Settings are TOLLBOOTH_* environment variables, read by pydantic-settings. A bad value is ignored with
one line on stderr and the default is used: a typo in one agent's environment must not stop its calls.
The clock seam: a Clock gives the wall clock (times shared between processes), the monotonic clock (a
call's own deadline) and sleep; SystemClock in production, a fake in the tests.
Not here: the lock and the shared state (tollbooth_throttle), talking to GitHub (tollbooth_http) and
the command line (tollbooth_cli).
"""
from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any, Protocol

from pydantic import (AfterValidator, BeforeValidator, Field, ValidationError, ValidationInfo,
                      ValidatorFunctionWrapHandler, WrapValidator)
from pydantic_settings import BaseSettings, SettingsConfigDict

log = logging.getLogger("gh-tollbooth")
if not log.handlers:  # each notice is one "gh-tollbooth: ..." line on stderr
    _stderr = logging.StreamHandler()
    _stderr.setFormatter(logging.Formatter("gh-tollbooth: %(message)s"))
    log.addHandler(_stderr)
    log.setLevel(logging.INFO)
    log.propagate = False

DEFAULT_DEADLINE = 100     # seconds per GitHub call; under the Bash tool's 120 s default
DEFAULT_MAX_WAIT = 60      # seconds; the longest rate-limit block or backoff one call sleeps through
# 1 = no token, no answer from GitHub, or no state folder; 75 = GitHub rate limit (stop, report the reset time);
# 76 = gh-tollbooth busy or out of time (retry once later)
EX_FAIL, EX_USAGE, EX_HEAVY, EX_BUDGET, EX_LIMITED, EX_BUSY = 1, 2, 3, 4, 75, 76


class TollboothError(Exception):
    def __init__(self, msg: str, code: int):
        super().__init__(msg)
        self.code = code


# ---------- the clock seam ----------

class Clock(Protocol):
    """Where gh-tollbooth gets the time. SystemClock in production; tests pass a fake that never sleeps."""

    def now(self) -> float:
        """Wall clock: the times shared between processes (state.db)."""
        ...

    def mono(self) -> float:
        """Monotonic clock: a call's own deadline, so a wall-clock step can't bend it."""
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


# ---------- settings ----------

def _default_state_dir() -> Path:
    return Path.home() / ".cache" / "gh-tollbooth"


def _whole_seconds(value: Any) -> Any:
    """Digits only: pydantic alone would also take " 45", "4_5" and "45.0"."""
    if isinstance(value, str) and not re.fullmatch(r"[1-9][0-9]*", value):
        raise ValueError("not a whole number of seconds")
    return value


def _absolute(path: Path) -> Path:
    path = path.expanduser()
    if not path.is_absolute():
        raise ValueError("not an absolute path")
    return path


def _or_default(default: Callable[[], Any], why: str, shown: str, cut: int) -> WrapValidator:
    """Validate a setting; if it is bad, log one line (the value cut to `cut` characters) and use the default."""
    def validate(value: Any, handler: ValidatorFunctionWrapHandler, info: ValidationInfo) -> Any:
        try:
            return handler(value)
        except ValidationError:
            log.warning("ignoring TOLLBOOTH_%s=%r (%s); using %s", str(info.field_name).upper(), str(value)[:cut],
                        why, shown)
            return default()
    return WrapValidator(validate)


class Settings(BaseSettings):
    """The TOLLBOOTH_* environment variables. An empty one counts as unset."""

    model_config = SettingsConfigDict(env_prefix="TOLLBOOTH_", env_ignore_empty=True)

    # seconds one GitHub call may take, waits included; 1 to 999,999
    deadline: Annotated[int, Field(ge=1, le=999_999), BeforeValidator(_whole_seconds),
                        _or_default(lambda: DEFAULT_DEADLINE, "not a whole number of seconds",
                                    str(DEFAULT_DEADLINE), 20)] = DEFAULT_DEADLINE
    # seconds; the longest rate-limit block or backoff one call sleeps through; 1 to 999,999
    max_wait: Annotated[int, Field(ge=1, le=999_999), BeforeValidator(_whole_seconds),
                        _or_default(lambda: DEFAULT_MAX_WAIT, "not a whole number of seconds",
                                    str(DEFAULT_MAX_WAIT), 20)] = DEFAULT_MAX_WAIT
    # the folder every agent shares; an absolute path, so all agents find the same one
    state_dir: Annotated[Path, AfterValidator(_absolute),
                         _or_default(_default_state_dir, "not an absolute path", "~/.cache/gh-tollbooth", 40)
                         ] = Field(default_factory=_default_state_dir)


SETTINGS = Settings()
