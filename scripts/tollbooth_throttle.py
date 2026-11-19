"""tollbooth_throttle: the machine-wide lock and the shared state, in the shared state folder.

The machine-wide lock is a filelock FileLock (flock on Unix) on `lock` in the shared state folder.
The shared state (call times, blocks, budget charges and the call log) is one SQLite file there, state.db.
A Deadline is one call's time budget: every wait asks it first, and it picks exit 75 or 76.
BUCKETS are the rate-limit buckets the throttle tracks; infer_bucket() says which one a request goes to.
A command takes one turn per request with Throttle.turn(): check the budget and blocks, wait without
the lock, then under the lock check again, charge, send and record. Charging, marking and logging are
private, so no caller can get that order wrong.
Not here: settings, exit codes and errors (tollbooth_core).
"""
from __future__ import annotations

import datetime as dt
import errno
import os
import re
import sqlite3
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar

from filelock import FileLock, Timeout
from tenacity import retry, retry_if_exception, stop_after_attempt

import tollbooth_core as core
from tollbooth_core import EX_BUDGET, EX_BUSY, EX_FAIL, EX_LIMITED, TollboothError, log
from tollbooth_text import printable

T = TypeVar("T")
R = TypeVar("R")

FS_RETRIES = 5             # tries for a file step whose folder someone deletes (rm -rf ~/.cache/gh-tollbooth)
# How a file step fails when the folder is deleted under it: not found, mkdir losing a race (exists),
# and on macOS, creating a file in a folder that is being deleted (invalid argument)
VANISHED = (errno.ENOENT, errno.EEXIST, errno.EINVAL)
LOCK_POLL = 0.2            # seconds between tries for a held lock
WAIT_NOTICE = 5            # seconds of waiting for the lock before one "waiting" line on stderr
MIN_CALL_TIME = 15         # seconds; no request starts with less time left (or half the deadline, if shorter)
MIN_SEND_TIME = 1.0        # seconds a charged request gets even past the deadline: once charged, it is sent
SPACING_UNDER_LOCK = 2.5   # seconds; a longer call gap (code search) is slept without the lock
MAX_PASSES = 60            # turns one call takes before it gives up; a safety net, waits end it sooner
FIRST_BACKOFF = 60         # seconds to wait after a secondary limit that names none; doubled on each such answer
MAX_ANSWERS = 4            # rate-limited answers one call takes; the fourth is exit 75
SPACING = {"graphql": 1.0, "core": 0.5, "search": 2.2, "code_search": 6.5}  # seconds between calls, per bucket
BUCKETS = tuple(SPACING)   # the buckets the throttle tracks, in the order status shows them; GitHub has more
BLOCK_CAP = 3600           # seconds; GitHub limits reset hourly, so a longer wait is recorded as one hour...
BLOCK_STALE = 3700         # ...and a block further ahead than this can't be real (a clock step): it is ignored
BUDGET_WINDOW_HOURS = 24   # a budget counts the charges of this hour and the 24 before it
STATE_WAIT = 10            # seconds to wait for another agent's change to state.db (each is one short transaction)
LOG_KEEP = 5000            # calls kept in the call log; older ones are deleted
LOG_SUMMARY = 160          # characters kept of what each call asked

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
CREATE TABLE IF NOT EXISTS backoff (id INTEGER PRIMARY KEY CHECK (id = 1), seconds REAL NOT NULL) STRICT;
"""


@dataclass(frozen=True)
class Call:
    """One row of the call log."""
    at: float       # wall-clock Unix time
    label: str
    pid: int
    bucket: str
    status: int     # HTTP status, 0 if GitHub gave no answer
    secs: float
    summary: str    # what was asked, cut to LOG_SUMMARY characters, hidden characters as "?"


@dataclass(frozen=True)
class Limit:
    """A rate limit GitHub reported for one request, as send() hands it to Throttle.turn()."""
    what: str                 # for messages: a bucket, "every bucket", or an untracked resource
    buckets: tuple[str, ...]  # the buckets every agent must stop for; empty: only this call stops
    seconds: float | None     # how long GitHub said to wait; None: it didn't say, so the shared backoff decides


class StoppedOnAnswer(TollboothError):
    """A call stopped on GitHub's own answer (a rate limit it won't wait out, or a 502/504).

    `answer` is that answer, as send returned it, so a command can still show what GitHub said."""

    def __init__(self, error: TollboothError, answer: Any):
        super().__init__(str(error), error.code)
        self.answer = answer


def infer_bucket(path: str) -> str:
    """GitHub's rate-limit bucket for a relative API path. GitHub routes `SEARCH/x` like `search/x`."""
    ep = path.lstrip("/").lower()
    if ep == "graphql":
        return "graphql"
    if ep.startswith("search/code"):
        return "code_search"
    return "search" if ep.startswith("search/") else "core"


def _busy(why: str) -> TollboothError:
    return TollboothError(f"{why}; gave up so this command doesn't hang (TOLLBOOTH_DEADLINE {core.SETTINGS.deadline} s). "
                          "gh-tollbooth is busy, not rate-limited: wait a minute and retry once.", EX_BUSY)


def _limited(what: str, seconds: float, clock: core.Clock) -> TollboothError:
    """Exit 75: `what` is rate-limited for `seconds` more. Stop and report; don't retry in a loop."""
    until = dt.datetime.fromtimestamp(clock.now() + seconds).strftime("%H:%M:%S")
    return TollboothError(f"{what} is rate-limited until {until}. Stop and report; do not retry in a loop.",
                          EX_LIMITED)


def _budget_error(label: str, budget: int) -> TollboothError:
    return TollboothError(f"the budget of {budget} GitHub calls for '{label}' is used up. "
                          "Stop querying and report what you have.", EX_BUDGET)


def _unusable(path: Path, e: BaseException) -> TollboothError:
    """Exit 1: the state folder or a file in it can't be used (permissions, a file in the way, ...)."""
    return TollboothError(f"can't use {path} ({getattr(e, 'strerror', None) or e}); fix its permissions or set "
                          "TOLLBOOTH_STATE_DIR to a folder you can write to", EX_FAIL)


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


# ---------- one call's time ----------

class Deadline:
    """One GitHub call's time budget: TOLLBOOTH_DEADLINE seconds of the monotonic clock, from its start.

    Every wait asks it first, and it picks the exit code when a wait doesn't fit: 75 (rate-limited, stop)
    when the wait is for a rate limit, 76 (busy, retry once) when it isn't.
    """

    def __init__(self, clock: core.Clock, seconds: float | None = None):
        self.clock = clock
        self.seconds = core.SETTINGS.deadline if seconds is None else seconds
        self.at = clock.mono() + self.seconds

    def left(self) -> float:
        """Seconds until the deadline; negative once it has passed."""
        return self.at - self.clock.mono()

    def fits(self, wait: float) -> bool:
        """True if a request can still start after waiting `wait` seconds.

        MIN_CALL_TIME must be left for it then, or half the deadline if that is shorter.
        """
        return wait + min(MIN_CALL_TIME, self.seconds / 2) <= self.left()

    def wait(self, seconds: float, limit: str | None = None) -> None:
        """Sleep `seconds`, or stop if a request couldn't start afterwards.

        `limit` names the rate limit the wait is for (a bucket, or "every bucket"): longer than
        TOLLBOOTH_MAX_WAIT, or past the deadline, is exit 75. Any other wait that doesn't fit is exit 76.
        A wait for a limit says so on stderr; every wait here is slept without the lock, but a call gap.
        """
        if limit is not None and (seconds > core.SETTINGS.max_wait or not self.fits(seconds)):
            raise _limited(limit, seconds, self.clock)
        if not self.fits(seconds):
            raise _busy("not enough time left for another GitHub request")
        if seconds > 0:
            if limit is not None:
                log.info("%s is rate-limited; waiting %.0f s (lock released)", limit, seconds)
            self.clock.sleep(seconds)


# ---------- shared throttle ----------

class Throttle:
    """The machine-wide lock, the shared state and the turn every request takes through them.

    Times come from `clock`: its wall clock for what is shared between processes, its monotonic clock
    for a call's own deadline. A state folder, lock or state.db that can't be used is exit 1.
    """

    def __init__(self, state_dir: Path | None = None, clock: core.Clock = core.SYSTEM_CLOCK):
        self.dir = core.SETTINGS.state_dir if state_dir is None else Path(state_dir)
        self.clock = clock
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise _unusable(self.dir, e) from None
        self.lock_path = self.dir / "lock"
        self.db_path = self.dir / "state.db"

    @contextmanager
    def hold(self, deadline: float) -> Generator[Throttle, None, None]:
        """Hold the machine-wide lock; give up (exit 76) at `deadline`, a value of the monotonic clock.

        filelock tries every LOCK_POLL seconds and opens the file again on each try, so a waiter follows a
        lock file that was deleted or replaced. One line on stderr says so after WAIT_NOTICE seconds.
        There is no first-come queue. Callers sleep their call gap outside the lock, so a latecomer gets in
        between a busy agent's calls. Add a queue if agents starve.
        Known limit: if the lock file is deleted while held, a later agent can't see the holder and
        the two overlap once.
        """
        lock = FileLock(self.lock_path, thread_local=False)

        def wait_until(limit: float) -> None:
            _in_folder(self.dir, lambda: lock.acquire(timeout=max(0.0, limit - self.clock.mono()),
                                                      poll_interval=LOCK_POLL))

        notice_at = self.clock.mono() + WAIT_NOTICE
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
        except OSError as e:  # after Timeout, which is an OSError too
            raise _unusable(self.lock_path, e) from None
        try:
            yield self
        finally:
            lock.release()

    # ---- one turn ----

    def turn(self, label: str, budget: int | None, bucket: str, deadline: Deadline, summary: str,
             send: Callable[[float], tuple[R, int, Limit | None]]) -> R:
        """One request through the shared throttle; returns what `send` returned with no rate limit.

        1. Check: the label's budget (exit 4) and a block on the bucket longer than TOLLBOOTH_MAX_WAIT (75).
        2. Without the lock: wait out a short block, then the bucket's call gap; the Deadline decides.
        3. Under the lock: check again, charge the label, call send(seconds_left), record the call.
        If another agent blocked or used the bucket while we waited for the lock, go round again.
        `send` returns (result, HTTP status, Limit or None). A Limit is recorded as a block for every
        agent; the turn then waits it out and sends again, up to MAX_ANSWERS limited answers. A limit
        on a resource the throttle doesn't track is not shared, and ends this call (exit 75). A limit
        that ends the call is a StoppedOnAnswer that carries send's result.
        A request is charged before it is sent, so a killed process pays; if `send` raises, the call
        is still spaced and logged, with status 0.
        """
        answers = 0
        for _ in range(MAX_PASSES):
            self.check(label, budget, bucket)
            self._wait_turn(bucket, deadline)
            got = self._attempt(label, budget, bucket, deadline, summary, send)
            if got is None:
                continue
            result, limit, seconds = got
            if limit is None:
                return result
            answers += 1
            try:
                if not limit.buckets or answers >= MAX_ANSWERS:
                    raise _limited(limit.what, seconds, self.clock)
                deadline.wait(seconds, limit=limit.what)
            except TollboothError as e:  # the call stops on this answer: hand it to the caller
                raise StoppedOnAnswer(e, result) from None
        raise _busy("still not our turn after repeated waits")

    def check(self, label: str, budget: int | None, bucket: str) -> None:
        """Fail before any work if `label` can't call `bucket` now: its budget is used up (exit 4), or the
        bucket is blocked for longer than TOLLBOOTH_MAX_WAIT (exit 75)."""
        if budget is not None and self.used(label) >= budget:
            raise _budget_error(label, budget)
        left = self.blocked_for(bucket)
        if left > core.SETTINGS.max_wait:
            raise _limited(bucket, left, self.clock)

    def _wait_turn(self, bucket: str, deadline: Deadline) -> None:
        left = self.blocked_for(bucket)
        if left > 0:
            deadline.wait(left, limit=bucket)
        deadline.wait(self.spacing_gap(bucket))

    def _attempt(self, label: str, budget: int | None, bucket: str, deadline: Deadline, summary: str,
                 send: Callable[[float], tuple[R, int, Limit | None]]) -> tuple[R, Limit | None, float] | None:
        """Under the lock: check again, charge, send, record. Returns send's result, its Limit and the
        seconds to wait for it; None means another agent got in first.

        The time check comes before the charge, and charging can be slow (another agent's change to
        state.db), so send gets at least MIN_SEND_TIME: a charged request is always sent."""
        with self.hold(deadline.at):
            if self.blocked_for(bucket) > 0:  # another agent recorded a block while we waited for the lock
                return None
            if budget is not None and self.used(label) >= budget:
                raise _budget_error(label, budget)  # checked under the lock, so calls at once can't overrun it
            gap = self.spacing_gap(bucket)
            if gap > SPACING_UNDER_LOCK:  # another agent's code search landed meanwhile
                return None
            deadline.wait(gap)
            self._charge(label)
            started = self.clock.mono()
            try:
                result, status, limit = send(max(deadline.left(), MIN_SEND_TIME))
            except Exception:
                self._mark(bucket)
                self._log(label, bucket, 0, self.clock.mono() - started, summary)
                raise
            seconds = 0.0
            if limit is None:
                self._mark(bucket, reset_backoff=True)
            else:
                seconds = min(self._next_backoff() if limit.seconds is None else limit.seconds, BLOCK_CAP)
                now = self.clock.now()
                self._mark(bucket, {b: now + seconds for b in limit.buckets})
            self._log(label, bucket, status, self.clock.mono() - started, summary)
            return result, limit, seconds

    # ---- state.db: one short transaction per step, safe with or without hold() ----

    def _db(self, work: Callable[[sqlite3.Connection], T], write: bool = True) -> T:
        """Run `work` in one transaction on state.db and close the connection.

        A write starts with BEGIN IMMEDIATE, SQLite's write lock, so what it reads and what it writes are
        one step for every agent; another agent waits up to STATE_WAIT seconds, then gets exit 76. A deleted
        folder is made again (_in_folder). A file that isn't a database is moved to state.db.broken. Any
        other error is exit 1.
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
            if (getattr(e, "sqlite_errorcode", 0) & 0xFF) not in (sqlite3.SQLITE_NOTADB, sqlite3.SQLITE_CORRUPT):
                raise self._db_error(e) from None
        except OSError as e:
            raise _unusable(self.dir, e) from None
        log.warning("%s is not a SQLite database; moved it to state.db.broken and started a new one", self.db_path)
        try:
            self.db_path.replace(self.db_path.with_name("state.db.broken"))
            return _in_folder(self.dir, once)
        except (sqlite3.DatabaseError, OSError) as e:
            raise self._db_error(e) from None

    def _db_error(self, e: sqlite3.DatabaseError | OSError) -> TollboothError:
        if isinstance(e, sqlite3.DatabaseError) and (getattr(e, "sqlite_errorcode", 0) & 0xFF) == sqlite3.SQLITE_BUSY:
            return _busy("another agent held the state file too long")
        return _unusable(self.db_path, e)

    def blocked_for(self, bucket: str) -> float:
        """Seconds until a recorded block on `bucket` ends (0 or less if none). Blocks over an hour ahead are stale."""
        row = self._db(lambda con: con.execute("SELECT until FROM block WHERE bucket = ?", (bucket,)).fetchone(),
                       write=False)
        left = row[0] - self.clock.now() if row else 0.0
        return 0.0 if left > BLOCK_STALE else left

    def spacing_gap(self, bucket: str) -> float:
        """Seconds to wait before the next call in `bucket` (at most SPACING; a last call in the future waits 0)."""
        row = self._db(lambda con: con.execute("SELECT at FROM last_call WHERE bucket = ?", (bucket,)).fetchone(),
                       write=False)
        elapsed = self.clock.now() - row[0] if row else SPACING[bucket]
        return SPACING[bucket] - elapsed if 0 <= elapsed < SPACING[bucket] else 0.0

    def _mark(self, bucket: str, blocks: dict[str, float] | None = None, spaced: bool = True,
              reset_backoff: bool = False) -> None:
        """Record a call's time (for spacing) and blocks {bucket: until} that every agent must honor.

        A block is cut to BLOCK_CAP from now. A shorter block never shortens a longer one, unless the stored
        one is stale (more than BLOCK_STALE ahead). `reset_backoff`: GitHub answered without a limit, so
        the next wait for a secondary limit that names none starts at FIRST_BACKOFF again.
        """
        def record(con: sqlite3.Connection) -> None:
            now = self.clock.now()
            if reset_backoff:
                con.execute("DELETE FROM backoff")
            if spaced:
                con.execute("INSERT INTO last_call VALUES (?, ?) ON CONFLICT (bucket) DO UPDATE SET at = excluded.at",
                            (bucket, now))
            for b, until in (blocks or {}).items():
                con.execute("INSERT INTO block VALUES (:b, :until) ON CONFLICT (bucket) DO UPDATE SET until = "
                            "CASE WHEN until - :now > :stale THEN excluded.until ELSE max(until, excluded.until) END",
                            {"b": b, "until": min(until, now + BLOCK_CAP), "now": now, "stale": BLOCK_STALE})
        self._db(record)

    def _next_backoff(self) -> float:
        """The wait for a secondary limit that names none: FIRST_BACKOFF, doubled on each such answer to any
        agent until one that isn't a limit. It lives in state.db, so it keeps growing across calls."""
        def bump(con: sqlite3.Connection) -> float:
            row = con.execute("SELECT seconds FROM backoff").fetchone()
            seconds = row[0] if row else float(FIRST_BACKOFF)
            con.execute("INSERT INTO backoff VALUES (1, :s) ON CONFLICT (id) DO UPDATE SET seconds = :s",
                        {"s": min(seconds * 2, BLOCK_CAP)})
            return seconds
        return self._db(bump)

    # ---- budgets: per label and wall-clock hour, charged before the call starts ----

    def _charge(self, label: str) -> None:
        """Charge one call to `label` in this hour. Charges older than the window are deleted."""
        hour = int(self.clock.now() // 3600)

        def record(con: sqlite3.Connection) -> None:
            con.execute("DELETE FROM charge WHERE hour NOT BETWEEN ? AND ?", (hour - BUDGET_WINDOW_HOURS, hour + 1))
            con.execute("INSERT INTO charge VALUES (?, ?, 1) ON CONFLICT (label, hour) DO UPDATE SET n = n + 1",
                        (label, hour))
        self._db(record)

    def used(self, label: str) -> int:
        """Calls charged to `label` in this hour and the 24 before it (the hour after counts too: a clock step)."""
        hour = int(self.clock.now() // 3600)
        sql = "SELECT coalesce(sum(n), 0) FROM charge WHERE label = ? AND hour BETWEEN ? AND ?"
        row = self._db(lambda con: con.execute(sql, (label, hour - BUDGET_WINDOW_HOURS, hour + 1)).fetchone(),
                       write=False)
        return row[0]

    # ---- call log (informational; budgets don't depend on it) ----

    def _log(self, label: str, bucket: str, status: int, secs: float, summary: str) -> None:
        """Add one call to the log and keep the newest LOG_KEEP. A call that can't be logged is dropped."""
        summary = printable(re.sub(r"\s+", " ", summary))[:LOG_SUMMARY]

        def record(con: sqlite3.Connection) -> None:
            con.execute("INSERT INTO call (at, label, pid, bucket, status, secs, summary) VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (self.clock.now(), label, os.getpid(), bucket, status, secs, summary))
            con.execute("DELETE FROM call WHERE id <= (SELECT max(id) FROM call) - ?", (LOG_KEEP,))
        try:
            self._db(record)
        except (sqlite3.Error, OSError, TollboothError):
            pass

    def entries(self, since: float = 0.0) -> list[Call]:
        """Calls logged at or after `since` (wall-clock time), oldest first, hidden characters as "?"."""
        rows = self._db(lambda con: con.execute("SELECT at, label, pid, bucket, status, secs, summary FROM call "
                                                "WHERE at >= ? ORDER BY id", (since,)).fetchall(), write=False)
        return [Call(at, printable(label), pid, printable(bucket), status, secs, printable(summary))
                for at, label, pid, bucket, status, secs, summary in rows]
