"""tollbooth_core: what every gh-tollbooth process on this machine shares: settings, exit codes and the lock.

Settings are TOLLBOOTH_* environment variables, read by pydantic-settings. A bad value is ignored with
one line on stderr and the default is used: a typo in one agent's environment must not stop its calls.
The machine-wide lock is a filelock FileLock (flock on Unix) on `lock` in the shared state folder.
Not here: talking to GitHub (tollbooth_http) and the rules for one turn (spacing, blocks, budgets).
"""
from __future__ import annotations

import errno
import logging
import re
import time
from collections.abc import Callable, Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated, Any, TypeVar

from filelock import FileLock, Timeout
from pydantic import (AfterValidator, BeforeValidator, Field, ValidationError, ValidationInfo,
                      ValidatorFunctionWrapHandler, WrapValidator)
from pydantic_settings import BaseSettings, SettingsConfigDict
from tenacity import retry, retry_if_exception, stop_after_attempt

T = TypeVar("T")

log = logging.getLogger("gh-tollbooth")
if not log.handlers:  # each notice is one "gh-tollbooth: ..." line on stderr
    _stderr = logging.StreamHandler()
    _stderr.setFormatter(logging.Formatter("gh-tollbooth: %(message)s"))
    log.addHandler(_stderr)
    log.setLevel(logging.INFO)
    log.propagate = False

DEFAULT_DEADLINE = 100     # seconds per GitHub call; under the Bash tool's 120 s default
FS_RETRIES = 5             # tries for a file step whose folder someone deletes (rm -rf ~/.cache/gh-tollbooth)
# How a file step fails when the folder is deleted under it: not found, mkdir losing a race (exists),
# and on macOS, creating a file in a folder that is being deleted (invalid argument)
VANISHED = (errno.ENOENT, errno.EEXIST, errno.EINVAL)
LOCK_POLL = 0.2            # seconds between tries for a held lock
WAIT_NOTICE = 5            # seconds of waiting for the lock before one "waiting" line on stderr
# 1 = no token, no answer from GitHub, or no state folder; 75 = GitHub rate limit (stop, report the reset time);
# 76 = gh-tollbooth busy or out of time (retry once later)
EX_FAIL, EX_USAGE, EX_HEAVY, EX_BUDGET, EX_LIMITED, EX_BUSY = 1, 2, 3, 4, 75, 76


class TollboothError(Exception):
    def __init__(self, msg: str, code: int):
        super().__init__(msg)
        self.code = code


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
    # the folder every agent shares; an absolute path, so all agents find the same one
    state_dir: Annotated[Path, AfterValidator(_absolute),
                         _or_default(_default_state_dir, "not an absolute path", "~/.cache/gh-tollbooth", 40)
                         ] = Field(default_factory=_default_state_dir)


SETTINGS = Settings()


def _busy(why: str) -> TollboothError:
    return TollboothError(f"{why}; gave up so this command doesn't hang (TOLLBOOTH_DEADLINE {SETTINGS.deadline} s). "
                          "gh-tollbooth is busy, not rate-limited: wait a minute and retry once.", EX_BUSY)


def _vanished(e: BaseException) -> bool:
    return isinstance(e, OSError) and e.errno in VANISHED


@retry(retry=retry_if_exception(_vanished), stop=stop_after_attempt(FS_RETRIES), reraise=True)
def _in_folder(folder: Path, step: Callable[[], T]) -> T:
    """Run a file step in `folder`, making the folder first; if it vanished meanwhile, try again.

    After FS_RETRIES tries, the last error goes to the caller.
    """
    folder.mkdir(parents=True, exist_ok=True)
    return step()


# ---------- shared throttle ----------

class Throttle:
    """The machine-wide lock, in the shared state folder."""

    def __init__(self, state_dir: Path | None = None):
        self.dir = SETTINGS.state_dir if state_dir is None else Path(state_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.lock_path = self.dir / "lock"

    @contextmanager
    def hold(self, deadline: float) -> Generator[Throttle, None, None]:
        """Hold the machine-wide lock; give up (exit 76) at `deadline`, a time.monotonic() value.

        filelock tries every LOCK_POLL seconds and opens the file again on each try, so a waiter follows a
        lock file that was deleted or replaced. One line on stderr says so after WAIT_NOTICE seconds.
        There is no first-come queue. Callers sleep their call gap outside the lock, so a latecomer gets in
        between a busy agent's calls. Add a queue if agents starve.
        Known limit: if the lock file is deleted while held, a later agent can't see the holder and
        the two overlap once.
        """
        lock = FileLock(self.lock_path, thread_local=False)

        def wait_until(limit: float) -> None:
            _in_folder(self.dir, lambda: lock.acquire(timeout=max(0.0, limit - time.monotonic()),
                                                      poll_interval=LOCK_POLL))

        notice_at = time.monotonic() + WAIT_NOTICE
        try:
            try:
                wait_until(min(deadline, notice_at))
            except Timeout:
                if notice_at >= deadline:
                    raise
                log.info("waiting for another agent's GitHub call to finish...")
                wait_until(deadline)
        except Timeout:
            raise _busy("another agent held the GitHub lock too long") from None
        try:
            yield self
        finally:
            lock.release()
