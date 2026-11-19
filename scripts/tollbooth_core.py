"""tollbooth_core: what every gh-tollbooth process on this machine shares: settings, the lock and the state.

Settings are TOLLBOOTH_* environment variables, read by pydantic-settings. A bad value is ignored with
one line on stderr and the default is used: a typo in one agent's environment must not stop its calls.
The machine-wide lock is a filelock FileLock (flock on Unix) on `lock` in the shared state folder.
The shared state (call times, blocks, budget charges and the call log) is one SQLite file there, state.db.
Not here: talking to GitHub (tollbooth_http).
"""
from __future__ import annotations

import errno
import logging
import os
import re
import sqlite3
import time
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass
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
SPACING = {"graphql": 1.0, "core": 0.5, "search": 2.2, "code_search": 6.5}  # seconds between calls, per bucket
BLOCK_CAP = 3600           # seconds; GitHub limits reset hourly, so a longer wait is recorded as one hour...
BLOCK_STALE = 3700         # ...and a block further ahead than this can't be real (a clock step): it is ignored
BUDGET_WINDOW_HOURS = 24   # a budget counts the charges of this hour and the 24 before it
STATE_WAIT = 10            # seconds to wait for another agent's change to state.db (each is one short transaction)
LOG_KEEP = 5000            # calls kept in the call log; older ones are deleted
LOG_SUMMARY = 160          # characters kept of what each call asked
CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")
# 1 = no token, no answer from GitHub, or no state folder; 75 = GitHub rate limit (stop, report the reset time);
# 76 = gh-tollbooth busy or out of time (retry once later)
EX_FAIL, EX_USAGE, EX_HEAVY, EX_BUDGET, EX_LIMITED, EX_BUSY = 1, 2, 3, 4, 75, 76

# The tables of state.db, made on first use. STRICT: SQLite refuses a value of the wrong type, so readers
# don't have to check what they get back.
SCHEMA = """
CREATE TABLE IF NOT EXISTS last_call (bucket TEXT PRIMARY KEY, at REAL NOT NULL) STRICT;
CREATE TABLE IF NOT EXISTS block (bucket TEXT PRIMARY KEY, until REAL NOT NULL) STRICT;
CREATE TABLE IF NOT EXISTS charge (label TEXT NOT NULL, hour INTEGER NOT NULL, n INTEGER NOT NULL CHECK (n >= 0),
                                   PRIMARY KEY (label, hour)) STRICT;
CREATE TABLE IF NOT EXISTS call (id INTEGER PRIMARY KEY, at REAL NOT NULL, label TEXT NOT NULL, pid INTEGER NOT NULL,
                                 bucket TEXT NOT NULL, status INTEGER NOT NULL, secs REAL NOT NULL,
                                 summary TEXT NOT NULL) STRICT;
"""


class TollboothError(Exception):
    def __init__(self, msg: str, code: int):
        super().__init__(msg)
        self.code = code


@dataclass(frozen=True)
class Call:
    """One row of the call log."""
    at: float       # wall-clock Unix time
    label: str
    pid: int
    bucket: str
    status: int     # HTTP status, 0 if GitHub gave no answer
    secs: float
    summary: str    # what was asked, cut to LOG_SUMMARY characters, control characters as "?"


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


def wall_clock() -> float:
    """Wall-clock Unix time: every time shared between processes (state.db) uses it. Tests replace it."""
    return time.time()


def _busy(why: str) -> TollboothError:
    return TollboothError(f"{why}; gave up so this command doesn't hang (TOLLBOOTH_DEADLINE {SETTINGS.deadline} s). "
                          "gh-tollbooth is busy, not rate-limited: wait a minute and retry once.", EX_BUSY)


def _printable(text: str) -> str:
    """`text` with control characters as "?", so nothing in the log can drive a terminal."""
    return CONTROL_RE.sub("?", text)


def _vanished(e: BaseException) -> bool:
    if isinstance(e, sqlite3.OperationalError):  # SQLite can't open, read or write a file whose folder was deleted
        code = getattr(e, "sqlite_errorcode", 0)
        return (code & 0xFF) in (sqlite3.SQLITE_CANTOPEN, sqlite3.SQLITE_IOERR) or code == sqlite3.SQLITE_READONLY_DBMOVED
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
    """The machine-wide lock and the shared state, in the shared state folder."""

    def __init__(self, state_dir: Path | None = None):
        self.dir = SETTINGS.state_dir if state_dir is None else Path(state_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.lock_path = self.dir / "lock"
        self.db_path = self.dir / "state.db"

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

    # ---- state.db: one short transaction per step, safe with or without hold() ----

    def _db(self, work: Callable[[sqlite3.Connection], T], write: bool = True) -> T:
        """Run `work` in one transaction on state.db and close the connection.

        A write starts with BEGIN IMMEDIATE, SQLite's write lock, so what it reads and what it writes are
        one step for every agent; another agent waits up to STATE_WAIT seconds, then gets exit 76. A deleted
        folder is made again (_in_folder). A file that isn't a database is moved to state.db.broken.
        """
        def once() -> T:
            con = sqlite3.connect(self.db_path, timeout=STATE_WAIT, isolation_level=None)
            try:
                con.executescript(SCHEMA)
                con.execute("BEGIN IMMEDIATE" if write else "BEGIN")
                result = work(con)
                con.execute("COMMIT")
                return result
            finally:
                con.close()  # an unfinished transaction is rolled back

        try:
            return _in_folder(self.dir, once)
        except sqlite3.DatabaseError as e:
            code = getattr(e, "sqlite_errorcode", 0) & 0xFF
            if code == sqlite3.SQLITE_BUSY:
                raise _busy("another agent held the state file too long") from None
            if code not in (sqlite3.SQLITE_NOTADB, sqlite3.SQLITE_CORRUPT):
                raise TollboothError(f"can't use {self.db_path} ({e}); fix its permissions or set "
                                     "TOLLBOOTH_STATE_DIR to a folder you own", EX_FAIL) from None
        log.warning("%s is not a SQLite database; moved it to state.db.broken and started a new one", self.db_path)
        self.db_path.replace(self.db_path.with_name("state.db.broken"))
        return _in_folder(self.dir, once)

    def blocked_for(self, bucket: str) -> float:
        """Seconds until a recorded block on `bucket` ends (0 or less if none). Blocks over an hour ahead are stale."""
        row = self._db(lambda con: con.execute("SELECT until FROM block WHERE bucket = ?", (bucket,)).fetchone(),
                       write=False)
        left = row[0] - wall_clock() if row else 0.0
        return 0.0 if left > BLOCK_STALE else left

    def spacing_gap(self, bucket: str) -> float:
        """Seconds to wait before the next call in `bucket` (at most SPACING; a last call in the future waits 0)."""
        row = self._db(lambda con: con.execute("SELECT at FROM last_call WHERE bucket = ?", (bucket,)).fetchone(),
                       write=False)
        elapsed = wall_clock() - row[0] if row else SPACING[bucket]
        return SPACING[bucket] - elapsed if 0 <= elapsed < SPACING[bucket] else 0.0

    def mark(self, bucket: str, blocks: dict[str, float] | None = None, spaced: bool = True) -> None:
        """Record a call's time (for spacing) and blocks {bucket: until} that every agent must honor.

        A block is cut to BLOCK_CAP from now. A shorter block never shortens a longer one, unless the stored
        one is stale (more than BLOCK_STALE ahead).
        """
        def record(con: sqlite3.Connection) -> None:
            now = wall_clock()
            if spaced:
                con.execute("INSERT INTO last_call VALUES (?, ?) ON CONFLICT (bucket) DO UPDATE SET at = excluded.at",
                            (bucket, now))
            for b, until in (blocks or {}).items():
                con.execute("INSERT INTO block VALUES (:b, :until) ON CONFLICT (bucket) DO UPDATE SET until = "
                            "CASE WHEN until - :now > :stale THEN excluded.until ELSE max(until, excluded.until) END",
                            {"b": b, "until": min(until, now + BLOCK_CAP), "now": now, "stale": BLOCK_STALE})
        self._db(record)

    # ---- budgets: per label and wall-clock hour, charged before the call starts ----

    def charge(self, label: str) -> int:
        """Charge one call to `label` in this hour and return the hour (for refund). Old charges are deleted."""
        hour = int(wall_clock() // 3600)

        def record(con: sqlite3.Connection) -> None:
            con.execute("DELETE FROM charge WHERE hour NOT BETWEEN ? AND ?", (hour - BUDGET_WINDOW_HOURS, hour + 1))
            con.execute("INSERT INTO charge VALUES (?, ?, 1) ON CONFLICT (label, hour) DO UPDATE SET n = n + 1",
                        (label, hour))
        self._db(record)
        return hour

    def refund(self, label: str, hour: int) -> None:
        """Take back one charge of `label` in `hour`, never below zero."""
        self._db(lambda con: con.execute("UPDATE charge SET n = n - 1 WHERE label = ? AND hour = ? AND n > 0",
                                         (label, hour)))

    def used(self, label: str) -> int:
        """Calls charged to `label` in this hour and the 24 before it (the hour after counts too: a clock step)."""
        hour = int(wall_clock() // 3600)
        sql = "SELECT coalesce(sum(n), 0) FROM charge WHERE label = ? AND hour BETWEEN ? AND ?"
        row = self._db(lambda con: con.execute(sql, (label, hour - BUDGET_WINDOW_HOURS, hour + 1)).fetchone(),
                       write=False)
        return row[0]

    # ---- call log (informational; budgets don't depend on it) ----

    def log(self, label: str, bucket: str, status: int, secs: float, summary: str) -> None:
        """Add one call to the log and keep the newest LOG_KEEP. A call that can't be logged is dropped."""
        summary = _printable(re.sub(r"\s+", " ", summary))[:LOG_SUMMARY]

        def record(con: sqlite3.Connection) -> None:
            con.execute("INSERT INTO call (at, label, pid, bucket, status, secs, summary) VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (wall_clock(), label, os.getpid(), bucket, status, secs, summary))
            con.execute("DELETE FROM call WHERE id <= (SELECT max(id) FROM call) - ?", (LOG_KEEP,))
        try:
            self._db(record)
        except (sqlite3.Error, OSError, TollboothError):
            pass

    def entries(self, since: float = 0.0) -> list[Call]:
        """Calls logged at or after `since` (wall-clock time), oldest first, control characters as "?"."""
        rows = self._db(lambda con: con.execute("SELECT at, label, pid, bucket, status, secs, summary FROM call "
                                                "WHERE at >= ? ORDER BY id", (since,)).fetchall(), write=False)
        return [Call(at, _printable(label), pid, _printable(bucket), status, secs, _printable(summary))
                for at, label, pid, bucket, status, secs, summary in rows]
