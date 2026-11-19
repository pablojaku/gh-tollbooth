"""tollbooth_throttle: the state every agent on this machine shares, and the rules for one turn. Stdlib only, 3.9+.

It makes every agent's GitHub calls add up to one polite caller:
- a lock lets one call through at a time;
- spacing keeps a minimum gap between calls in the same bucket;
- a block stops every agent after one of them hits a rate limit;
- a budget caps the calls each label can make.

A command takes one turn per request, in this order:
1. check(): fail at once if the label's budget is used up or the bucket is blocked too long;
2. wait_turn(): without the lock, sleep through a short block and the bucket's call gap;
3. attempt(): under the lock, check again, charge the label, send, and record what GitHub answered.
The order is the module's business: callers can't charge, mark or log on their own.

state.json and calls.log are shared files, so what is read from them is checked before use.
Shared times use the wall clock, since a monotonic clock is not the same in every process. A
caller's own deadline uses the monotonic clock. Both come from the Clock the Throttle is given.
"""
from __future__ import annotations

import datetime as dt
import errno
import fcntl
import json
import math
import os
import re
import tempfile
import time
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Callable, Dict, Optional, Tuple, TypeVar

import tollbooth_core as core
from tollbooth_core import EX_BUDGET, STATE_DIR, SYSTEM_CLOCK, Clock, TollboothError, busy, eprint, limited

MAX_WAIT = core.env_seconds("TOLLBOOTH_MAX_WAIT", 60)  # seconds; the longest block or backoff slept through
MIN_CALL_TIME = 15        # seconds; no request starts with less time left (or half the deadline, if shorter)
SPACING_UNDER_LOCK = 2.5  # seconds; a longer call gap (code search) is slept without the lock
BUDGET_WINDOW_HOURS = 24
STATE_LOCK_WAIT = 10      # seconds to wait for the state-file lock (held only for a read and a write)
HOUR_RE = re.compile(r"[0-9]{1,12}")
CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")
LOG_MAX_BYTES = 2_000_000  # calls.log is trimmed when it grows past this...
LOG_KEEP_LINES = 5000      # ...to its newest lines (see _trim_log)
FS_RETRIES = 5             # tries for a file step whose folder someone deletes (rm -rf ~/.cache/gh-tollbooth)
TEMP_MAX_AGE = 60          # seconds; an older temp file was left by a writer that was killed
# How a file step fails when the folder is deleted under it: not found, mkdir losing a race (exists),
# and on macOS, creating a file in a folder that is being deleted (invalid argument)
VANISHED = (errno.ENOENT, errno.EEXIST, errno.EINVAL)

T = TypeVar("T")
R = TypeVar("R")


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


def _budget_error(label: str, budget: int) -> TollboothError:
    return TollboothError(f"the budget of {budget} GitHub calls for '{label}' is used up. "
                          "Stop querying and report what you have.", EX_BUDGET)


class Throttle:
    """Machine-wide lock, per-bucket spacing, shared blocks, budgets, call log, and one turn through them."""

    def __init__(self, state_dir: Path = STATE_DIR, clock: Clock = SYSTEM_CLOCK):
        self.clock = clock
        self.dir = Path(state_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.lock_path, self.state_path = self.dir / "lock", self.dir / "state.json"
        self.log_path = self.dir / "calls.log"

    # ---- one turn ----

    def check(self, label: str, budget: Optional[int], bucket: str) -> None:
        """Fail before doing any work if the call can't happen: the label's budget is used up (exit 4),
        or the bucket is blocked for longer than MAX_WAIT (exit 75)."""
        st = self.state()
        if budget is not None and self.used(label, st) >= budget:
            raise _budget_error(label, budget)
        left = self.blocked_for(bucket, st)
        if left > MAX_WAIT:
            raise limited(bucket, left, self.clock)

    def wait_turn(self, bucket: str, deadline: float) -> None:
        """Without holding the lock: sleep through a short block on `bucket`, then its call gap.

        A block longer than MAX_WAIT, or one that outlasts the monotonic `deadline`, ends the call
        (exit 75). A gap is slept only if a request can still start afterwards (else exit 76).
        """
        left = self.blocked_for(bucket)
        if left > 0:
            if left > MAX_WAIT or self.clock.mono() + left > deadline:
                raise limited(bucket, left, self.clock)
            eprint(f"gh-tollbooth: {bucket} is rate-limited; waiting {left:.0f} s (lock released)")
            self.clock.sleep(left)
        self._sleep_gap(self._spacing_gap(bucket), deadline)

    def attempt(self, label: str, budget: Optional[int], bucket: str, deadline: float, summary: str,
                send: Callable[[float], Tuple[R, int, Dict[str, float]]]) -> Optional[R]:
        """Under the machine-wide lock: check again, charge `label`, send, and record what GitHub answered.

        `send(seconds_left)` makes the request and returns (result, HTTP status, blocks), where blocks
        is {bucket: seconds} that every agent must wait. The call is charged before `send` starts, so
        a killed process pays. If `send` raises, the call is still spaced and logged with status 0.
        Returns `send`'s result, or None if the bucket was blocked or needs a long call gap again
        (another agent called meanwhile): the caller then takes its turn again.
        Raises TollboothError: budget used up (exit 4), lock or time ran out (exit 76).
        """
        with self._hold(deadline):
            st = self.state()  # one read for the three checks below, all made under the lock
            if self.blocked_for(bucket, st) > 0:  # another agent blocked it while we waited for the lock
                return None
            if budget is not None and self.used(label, st) >= budget:
                raise _budget_error(label, budget)  # checked under the lock, so calls at once can't overrun it
            gap = self._spacing_gap(bucket, st)
            if gap > SPACING_UNDER_LOCK:  # another agent's code search landed meanwhile
                return None
            self._sleep_gap(gap, deadline)
            self._charge(label)
            started = self.clock.mono()
            try:
                result, status, blocks = send(deadline - started)
            except Exception:
                self._mark(bucket)
                self._log(label, bucket, 0, self.clock.mono() - started, summary)
                raise
            now = self.clock.now()
            self._mark(bucket, {b: now + seconds for b, seconds in blocks.items()})
            self._log(label, bucket, status, self.clock.mono() - started, summary)
            return result

    def _sleep_gap(self, gap: float, deadline: float) -> None:
        """Sleep a call gap, but only if a request can still start afterwards."""
        if self.clock.mono() + gap + min(MIN_CALL_TIME, core.DEADLINE / 2) > deadline:
            raise busy("not enough time left for another GitHub request")
        if gap > 0:
            self.clock.sleep(gap)

    # ---- reads: what `status` shows ----

    def state(self) -> dict:
        """state.json as a dict; a missing, unreadable or wrongly shaped file reads as empty."""
        try:
            st = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, RecursionError):
            return {}
        return st if isinstance(st, dict) else {}

    def blocked_for(self, bucket: str, st: Optional[dict] = None) -> float:
        """Seconds until a recorded block on `bucket` ends (<= 0 if none). Blocks over an hour ahead are stale.

        `st` is a state() already read; without it, state.json is read now."""
        left = _num((self.state() if st is None else st).get("blocked:" + bucket)) - self.clock.now()
        return 0.0 if left > core.BLOCK_SANITY else left

    def used(self, label: str, st: Optional[dict] = None) -> int:
        """Charged GitHub calls by `label` in the last 24-25 h (`st`: as for blocked_for)."""
        st = self.state() if st is None else st
        return sum(self._charges(st, int(self.clock.now() // 3600)).get(label, {}).values())

    def entries(self) -> list[list[str]]:
        try:
            text = self.log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return []
        return [[_printable(col) for col in ln.split("\t")] for ln in text.split("\n") if ln]

    # ---- files ----

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

    def _lock_file(self, path: Path, deadline: float, poll: float, held_by: str, warn: bool = False):
        """Open `path` and take an exclusive lock on it; give up (exit 76) at the monotonic `deadline`.

        If the file was deleted or replaced while we waited, the lock we got is on a file nobody else
        will open again, so it is dropped and the file now at `path` is locked instead.
        """
        start, warned = self.clock.mono(), False
        while True:
            fh = self._again(lambda: open(path, "a+"))  # the folder may have been deleted since __init__
            try:
                while True:
                    try:
                        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if self.clock.mono() >= deadline:
                            raise busy(f"another agent held the {held_by} too long")
                        if warn and not warned and self.clock.mono() - start > 5:
                            eprint("gh-tollbooth: waiting for another agent's GitHub call to finish...")
                            warned = True
                        self.clock.sleep(poll)
                if _same_file(fh, path):
                    return fh
            except BaseException:
                fh.close()
                raise
            fh.close()

    @contextmanager
    def _hold(self, deadline: float):
        """Hold the machine-wide lock; give up (exit 76) at the monotonic `deadline`.

        There is no first-come queue. Callers sleep their call gap outside the lock and waiters poll
        every 0.2 s, so a latecomer gets in between a busy agent's calls. Add a queue if agents starve.
        Known limit: if the lock file is deleted while held, a later agent can't see the holder and
        the two overlap once.
        """
        fh = self._lock_file(self.lock_path, deadline, 0.2, "GitHub lock", warn=True)
        try:
            yield
        finally:
            fh.close()  # closing the file releases the lock

    @contextmanager
    def _state_lock(self):
        """A second, short lock around every change to state.json and every write to calls.log.

        The state writers are safe whether or not their caller is inside _hold(). Always taken after
        _hold(), never around it, and never held across a GitHub call.
        """
        fh = self._lock_file(self.dir / "state.lock", self.clock.mono() + STATE_LOCK_WAIT, 0.002, "state file")
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

    def _save(self, st: dict) -> None:
        self._replace(self.state_path, json.dumps(st).encode("utf-8"))

    @contextmanager
    def _update(self):
        """Read state.json, let the caller change it, and write it back, one writer at a time."""
        with self._state_lock():
            st = self.state()
            yield st
            self._save(st)

    def _spacing_gap(self, bucket: str, st: Optional[dict] = None) -> float:
        """Seconds to wait before the next call in `bucket` (never more than SPACING; a future time waits 0)."""
        spacing = core.SPACING[bucket]
        elapsed = self.clock.now() - _num((self.state() if st is None else st).get(bucket))
        return spacing - elapsed if 0 <= elapsed < spacing else 0.0

    def _mark(self, bucket: str, blocks: Optional[dict] = None) -> None:
        """Record a call's time (for spacing) and blocks {bucket: until} that every agent must honor."""
        with self._update() as st:
            now = self.clock.now()
            st[bucket] = now
            for b, until in (blocks or {}).items():
                until = min(until, now + core.BLOCK_SANITY)  # a longer wait is honored for an hour, then asked again
                old = _num(st.get("blocked:" + b))
                st["blocked:" + b] = until if old - now > core.BLOCK_SANITY else max(old, until)

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

    def _charge(self, label: str) -> None:
        hour = int(self.clock.now() // 3600)
        with self._update() as st:
            charges = st["charges"] = self._charges(st, hour)
            mine = charges.setdefault(label, {})
            mine[str(hour)] = mine.get(str(hour), 0) + 1

    # ---- call log (informational; budgets don't depend on it) ----

    def _log(self, label: str, bucket: str, status: int, secs: float, summary: str) -> None:
        """One line per call: time, label, pid, bucket, HTTP status, seconds, what was asked.

        The append takes the state lock too, so no line lands in the old file while another agent
        trims it. The log is informational: a line that can't be written in time is dropped.
        """
        summary = _printable(re.sub(r"\s+", " ", summary))[:160]
        stamp = dt.datetime.fromtimestamp(self.clock.now()).isoformat(timespec="seconds")
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
