"""tollbooth_core: the state every agent on this machine shares. Stdlib only, Python 3.9+.

It makes every agent's GitHub calls add up to one polite caller:
- a lock lets one call through at a time;
- spacing keeps a minimum gap between calls in the same bucket;
- a block stops every agent after one of them hits a rate limit;
- a budget caps the calls each label can make.

A bucket is one of GitHub's separate rate-limit counters (GitHub says "resource"). This tool uses
four: graphql, core, search and code_search.

state.json and calls.log are shared files, so what is read from them is checked before use.
Shared times use the wall clock, since a monotonic clock is not the same in every process. A
caller's own deadline uses the monotonic clock.
"""
from __future__ import annotations

import datetime as dt
import errno
import fcntl
import json
import math
import os
import re
import sys
import tempfile
import time
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Callable, Optional, TypeVar

T = TypeVar("T")


def eprint(*a):
    print(*a, file=sys.stderr, flush=True)


def _env_seconds(name: str, default: int) -> int:
    """A whole number of seconds (1 or more) from the environment; anything else is ignored with one line."""
    value = os.environ.get(name, "")
    if re.fullmatch(r"[1-9][0-9]{0,5}", value):
        return int(value)
    if value:
        eprint(f"gh-tollbooth: ignoring {name}={value[:20]!r} (not a whole number of seconds); using {default}")
    return default


STATE_DIR = Path(os.environ.get("TOLLBOOTH_STATE_DIR", str(Path.home() / ".cache" / "gh-tollbooth")))
SPACING = {"graphql": 1.0, "core": 0.5, "search": 2.2, "code_search": 6.5}  # seconds between calls, per bucket
DEADLINE = _env_seconds("TOLLBOOTH_DEADLINE", 100)   # per GitHub call; under the Bash tool's 120 s default
BUDGET_WINDOW_HOURS = 24
LABEL_RE = re.compile(r"[A-Za-z0-9_.-]{1,40}")  # always use .fullmatch: "$" would accept a trailing newline
BLOCK_SANITY = 3700      # seconds; GitHub limits reset hourly, so a longer block is capped or stale
STATE_LOCK_WAIT = 10     # seconds to wait for the state-file lock (held only for a read and a write)
HOUR_RE = re.compile(r"[0-9]{1,12}")
CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")
LOG_MAX_BYTES = 2_000_000  # calls.log is trimmed when it grows past this...
LOG_KEEP_LINES = 5000      # ...to its newest lines (see _trim_log)
FS_RETRIES = 5             # tries for a file step whose folder someone deletes (rm -rf ~/.cache/gh-tollbooth)
TEMP_MAX_AGE = 60          # seconds; an older temp file was left by a writer that was killed
# How a file step fails when the folder is deleted under it: not found, mkdir losing a race (exists),
# and on macOS, creating a file in a folder that is being deleted (invalid argument)
VANISHED = (errno.ENOENT, errno.EEXIST, errno.EINVAL)
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


# ---------- clocks ----------

def _now() -> float:
    """Wall clock: every time shared between processes (state.json), budgets, logs."""
    return time.time()


def _mono() -> float:
    """Process-local monotonic clock: only this process's own deadline, so clock steps can't bend it."""
    return time.monotonic()


def _sleep(seconds: float) -> None:
    time.sleep(max(0.0, seconds))


def clock_in(seconds: float) -> str:
    """Wall-clock time `seconds` from now, for messages."""
    return dt.datetime.fromtimestamp(_now() + seconds).strftime("%H:%M:%S")


def _busy(why: str) -> TollboothError:
    return TollboothError(f"{why}; gave up so this command doesn't hang (TOLLBOOTH_DEADLINE {DEADLINE} s). "
                          "gh-tollbooth is busy, not rate-limited: wait a minute and retry once.", EX_BUSY)


def _num(value) -> float:
    """A finite number read from state.json, or 0 for anything else (an integer too big for a float, too)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    try:
        value = float(value)
    except OverflowError:
        return 0
    return value if math.isfinite(value) else 0


def _printable(text: str) -> str:
    """`text` with control characters as "?", so nothing in the log can drive a terminal."""
    return CONTROL_RE.sub("?", text)


def _same_file(fh, path: Path) -> bool:
    """True if the open file `fh` is still the file at `path` (it was not deleted or replaced)."""
    try:
        a, b = os.fstat(fh.fileno()), os.stat(path)
    except OSError:
        return False
    return (a.st_dev, a.st_ino) == (b.st_dev, b.st_ino)


# ---------- shared throttle ----------

class Throttle:
    """Machine-wide lock, per-bucket spacing, shared blocks, budgets, call log."""

    def __init__(self, state_dir: Path = STATE_DIR):
        self.dir = Path(state_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.lock_path, self.state_path = self.dir / "lock", self.dir / "state.json"
        self.log_path = self.dir / "calls.log"

    def _again(self, step: Callable[[], T]) -> T:
        """Run a file step in the state folder; if the folder vanished meanwhile, re-create it and retry."""
        for _ in range(FS_RETRIES - 1):
            try:
                self.dir.mkdir(parents=True, exist_ok=True)
                return step()
            except OSError as e:
                if e.errno not in VANISHED:
                    raise
        self.dir.mkdir(parents=True, exist_ok=True)  # the last try: whatever it raises goes to the caller
        return step()

    # ---- locks ----

    def _lock_file(self, path: Path, deadline: float, poll: float, held_by: str, warn: bool = False):
        """Open `path` and take an exclusive lock on it; give up (exit 76) at the monotonic `deadline`.

        If the file was deleted or replaced while we waited, the lock we got is on a file nobody else
        will open again, so it is dropped and the file now at `path` is locked instead.
        """
        start, warned = _mono(), False
        while True:
            fh = self._again(lambda: open(path, "a+"))  # the folder may have been deleted since __init__
            try:
                while True:
                    try:
                        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if _mono() >= deadline:
                            raise _busy(f"another agent held the {held_by} too long")
                        if warn and not warned and _mono() - start > 5:
                            eprint("gh-tollbooth: waiting for another agent's GitHub call to finish...")
                            warned = True
                        _sleep(poll)
                if _same_file(fh, path):
                    return fh
            except BaseException:
                fh.close()
                raise
            fh.close()

    @contextmanager
    def hold(self, deadline: float):
        """Hold the machine-wide lock; give up (exit 76) at the monotonic `deadline`.

        There is no first-come queue. Callers sleep their call gap outside the lock and waiters poll
        every 0.2 s, so a latecomer gets in between a busy agent's calls. Add a queue if agents starve.
        Known limit: if the lock file is deleted while held, a later agent can't see the holder and
        the two overlap once.
        """
        fh = self._lock_file(self.lock_path, deadline, 0.2, "GitHub lock", warn=True)
        try:
            yield self
        finally:
            fh.close()  # closing the file releases the lock

    @contextmanager
    def _state_lock(self):
        """A second, short lock around every change to state.json and every write to calls.log.

        The state writers are safe whether or not their caller is inside hold(). Always taken after
        hold(), never around it, and never held across a GitHub call.
        """
        fh = self._lock_file(self.dir / "state.lock", _mono() + STATE_LOCK_WAIT, 0.002, "state file")
        try:
            yield
        finally:
            fh.close()

    def _replace(self, path: Path, data: bytes) -> None:
        """Write `path` in one step: readers see the old file or the new one, never half of one.

        Called under _state_lock(), so any temp file that is not new belongs to a writer that died.
        """
        self._remove_dead_temp_files()
        self._again(lambda: self._write_new(path, data))

    def _remove_dead_temp_files(self) -> None:
        with suppress(OSError):
            for f in self.dir.iterdir():
                if f.name.startswith(("state.json.", "calls.log.")) and f.name.endswith(".tmp"):
                    with suppress(OSError):
                        if time.time() - f.stat().st_mtime > TEMP_MAX_AGE:  # file times are real time
                            f.unlink()

    def _write_new(self, path: Path, data: bytes) -> None:
        fd, tmp = tempfile.mkstemp(dir=self.dir, prefix=path.name + ".", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            os.replace(tmp, path)
        except BaseException:
            with suppress(OSError):
                os.unlink(tmp)
            raise

    # ---- shared state: wall-clock times, ignored when they can't be real ----

    def state(self) -> dict:
        """state.json as a dict; a missing, unreadable or wrongly shaped file reads as empty."""
        try:
            st = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, RecursionError):
            return {}
        return st if isinstance(st, dict) else {}

    def _save(self, st: dict) -> None:
        self._replace(self.state_path, json.dumps(st).encode("utf-8"))

    @contextmanager
    def _update(self):
        """Read state.json, let the caller change it, and write it back, one writer at a time."""
        with self._state_lock():
            st = self.state()
            yield st
            self._save(st)

    def blocked_for(self, bucket: str) -> float:
        """Seconds until a recorded block on `bucket` ends (<= 0 if none). Blocks over an hour ahead are stale."""
        left = _num(self.state().get("blocked:" + bucket)) - _now()
        return 0.0 if left > BLOCK_SANITY else left

    def spacing_gap(self, bucket: str) -> float:
        """Seconds to wait before the next call in `bucket` (never more than SPACING; a future time waits 0)."""
        elapsed = _now() - _num(self.state().get(bucket))
        return SPACING[bucket] - elapsed if 0 <= elapsed < SPACING[bucket] else 0.0

    def mark(self, bucket: str, blocks: Optional[dict] = None, spaced: bool = True) -> None:
        """Record a call's time (for spacing) and blocks {bucket: until} that every agent must honor."""
        with self._update() as st:
            now = _now()
            if spaced:
                st[bucket] = now
            for b, until in (blocks or {}).items():
                until = min(until, now + BLOCK_SANITY)  # a longer wait is honored for an hour, then asked again
                old = _num(st.get("blocked:" + b))
                st["blocked:" + b] = until if old - now > BLOCK_SANITY else max(old, until)

    # ---- budgets: per label and wall-clock hour, charged before the call starts ----

    @staticmethod
    def _charges(st: dict, hour: int) -> dict:
        """The well-formed charges {label: {hour: count}} of the last day; anything else in the file is dropped."""
        raw, out = st.get("charges"), {}
        for label, hours in (raw.items() if isinstance(raw, dict) else ()):
            if not isinstance(hours, dict):
                continue
            kept = {h: n for h, n in hours.items()
                    if HOUR_RE.fullmatch(h) and isinstance(n, int) and not isinstance(n, bool) and n > 0
                    and hour - BUDGET_WINDOW_HOURS <= int(h) <= hour + 1}  # later: the clock ran ahead
            if kept:
                out[label] = kept
        return out

    def charge(self, label: str) -> str:
        hour = int(_now() // 3600)
        with self._update() as st:
            charges = st["charges"] = self._charges(st, hour)
            mine = charges.setdefault(label, {})
            mine[str(hour)] = mine.get(str(hour), 0) + 1
        return str(hour)

    def refund(self, label: str, hour: str) -> None:
        with self._update() as st:
            charges = st["charges"] = self._charges(st, int(_now() // 3600))
            mine = charges.get(label, {})
            if mine.get(hour, 0) > 0:
                mine[hour] -= 1

    def used(self, label: str) -> int:
        """Charged GitHub calls by `label` in the last 24-25 h."""
        return sum(self._charges(self.state(), int(_now() // 3600)).get(label, {}).values())

    # ---- call log (informational; budgets don't depend on it) ----

    def log(self, label: str, bucket: str, status: int, secs: float, summary: str) -> None:
        """One line per call: time, label, pid, bucket, HTTP status, seconds, what was asked.

        The append takes the state lock too, so no line lands in the old file while another agent
        trims it. The log is informational: a line that can't be written in time is dropped.
        """
        summary = _printable(re.sub(r"\s+", " ", summary))[:160]
        stamp = dt.datetime.fromtimestamp(_now()).isoformat(timespec="seconds")
        line = f"{stamp}\t{label}\t{os.getpid()}\t{bucket}\t{status}\t{secs:.1f}\t{summary}\n"
        data = line.encode("utf-8", "replace")

        def append():
            with open(self.log_path, "ab") as f:  # one write per line
                f.write(data)
            return self.log_path.stat().st_size

        try:
            with self._state_lock():
                if self._again(append) > LOG_MAX_BYTES:
                    self._trim_log()
        except (OSError, TollboothError):
            pass

    def _trim_log(self) -> None:
        """Keep the newest LOG_KEEP_LINES lines. If those are still over the cap (a few huge lines),
        keep only the whole lines that fit in the last half of the cap, which may be none."""
        kept = b"".join(self.log_path.read_bytes().splitlines(keepends=True)[-LOG_KEEP_LINES:])
        if len(kept) > LOG_MAX_BYTES:
            tail = kept[-(LOG_MAX_BYTES // 2):]
            start = tail.find(b"\n", 0, len(tail) - 1)
            kept = tail[start + 1:] if start >= 0 else b""
        self._replace(self.log_path, kept)

    def entries(self) -> list[list[str]]:
        try:
            text = self.log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return []
        return [[_printable(col) for col in ln.split("\t")] for ln in text.split("\n") if ln]
