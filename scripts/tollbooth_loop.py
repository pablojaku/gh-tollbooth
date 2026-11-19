"""tollbooth_loop: one GitHub request, and the decision to try it again. Stdlib only, Python 3.9+.

A Caller is who is calling GitHub during one command: a label, its budget and the shared throttle.
Caller.fetch() is how gh-tollbooth's commands talk to GitHub. Each pass takes one turn through the
throttle (tollbooth_throttle: budget, blocks, call gap, the lock, charging, the log) and sends the
request in it (tollbooth_http.send). Then it reads GitHub's answer:
- not a rate limit: returned as it is, 404 and 403 included;
- a primary limit blocks that bucket until GitHub's reset time, a secondary limit blocks every
  bucket; either way it waits and tries again, for at most MAX_ANSWERS limited answers;
- a 502 or 504 means GitHub gave up on the request (exit 3), with advice if it was a GraphQL query.

A wait longer than TOLLBOOTH_MAX_WAIT ends the call at once (exit 75). The whole call ends within
TOLLBOOTH_DEADLINE seconds (exit 76 if it can't). Times come from the throttle's clock.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple

import tollbooth_core as core
import tollbooth_http as http
import tollbooth_throttle
from tollbooth_core import EX_HEAVY, Clock, TollboothError, eprint, limited

CALL_TIMEOUT = 90         # seconds one request may take
FIRST_BACKOFF = 60        # seconds to wait after a 429 that names no wait; doubled on each repeat
MAX_ANSWERS = 4           # rate-limited answers one call takes before it gives up


@dataclass(frozen=True)
class _Request:
    method: str
    path: str
    token: str = field(repr=False)  # never in a repr, a log line or a debug print
    accept: Optional[str]
    json_body: Optional[dict]
    bucket: str
    summary: str      # for the call log


class StoppedOnAnswer(TollboothError):
    """fetch() stopped on GitHub's own answer (a 502/504, or a rate limit it won't wait out).

    `answer` is that answer, so a command can still show what GitHub said."""

    def __init__(self, error: TollboothError, answer: http.Response):
        super().__init__(str(error), error.code)
        self.answer = answer


class _Backoff:
    """The wait after a secondary limit that names none: FIRST_BACKOFF, doubled on each such answer."""

    def __init__(self) -> None:
        self.seconds = float(FIRST_BACKOFF)

    def delay(self, verdict: http.Verdict) -> float:
        """GitHub's wait if it named one; else the backoff, which then doubles for the next answer."""
        if verdict.delay is not None:
            return verdict.delay
        seconds, self.seconds = self.seconds, self.seconds * 2
        return seconds


@dataclass(frozen=True)
class _Answer:
    """One request's answer and what it means for the next try."""
    resp: http.Response
    verdict: http.Verdict
    limited: str      # what is rate-limited, for messages: a bucket, or "every bucket"
    delay: float      # seconds to wait before trying again; 0 if not rate-limited


@dataclass
class Caller:
    """Who is calling GitHub during one command: a label, its budget and the shared throttle."""
    label: str               # whose budget each request is charged to
    budget: Optional[int]    # requests allowed for the label in 24 h; None = no limit
    throttle: tollbooth_throttle.Throttle
    _token: Optional[str] = field(default=None, init=False, repr=False, compare=False)

    def token(self) -> str:
        """The GitHub token, asked for on first use (`gh auth token` starts a process), then kept."""
        if self._token is None:
            self._token = http.token()
        return self._token

    def check(self, bucket: str) -> None:
        """Fail before doing any work if this caller can't call `bucket` now: the budget is used up
        (exit 4) or the bucket is blocked for longer than TOLLBOOTH_MAX_WAIT (exit 75)."""
        self.throttle.check(self.label, self.budget, bucket)

    def fetch(self, method: str, path: str, *, accept: Optional[str] = None,
              json_body: Optional[dict] = None) -> http.Response:
        """Send one request through the shared throttle and return GitHub's answer.

        Any answer that isn't a rate limit is returned as is, 404 and 403 included.
        Raises TollboothError (exit 3, 4, 75, 76), NoToken, TransportError or ValueError. A stop on
        GitHub's own answer (a 502/504, a rate limit) is a StoppedOnAnswer that carries that answer.
        A request is charged just before it is sent, and only if it is sent.
        """
        token = self.token()
        http.build_request(method, path, token, accept, json_body)  # a request it would refuse fails here
        req = _Request(method, path, token, accept, json_body, core.infer_bucket(path),
                       _summary(method, path, json_body))
        throttle, clock = self.throttle, self.throttle.clock
        deadline = clock.mono() + core.DEADLINE
        backoff, answers = _Backoff(), 0
        for _ in range(60):  # every pass sleeps or raises, so the deadline ends the loop; this is a safety net
            throttle.check(self.label, self.budget, req.bucket)
            throttle.wait_turn(req.bucket, deadline)
            got = throttle.attempt(self.label, self.budget, req.bucket, deadline, req.summary,
                                   lambda left: _send(req, clock, backoff, left))
            if got is None:  # the bucket was blocked or spaced again while we waited for the lock
                continue
            if got.verdict.kind is None:
                return got.resp
            if got.verdict.kind == "heavy":
                raise StoppedOnAnswer(_gave_up(req.method, req.path), got.resp)
            answers += 1
            delay = max(1.0, got.delay)
            if delay > tollbooth_throttle.MAX_WAIT or clock.mono() + delay > deadline or answers >= MAX_ANSWERS:
                raise StoppedOnAnswer(limited(got.limited, delay, clock), got.resp)
            eprint(f"gh-tollbooth: {got.limited} is rate-limited; waiting {delay:.0f} s (lock released)")
            clock.sleep(delay)
        raise core.busy("still blocked after repeated waits")


def _send(req: _Request, clock: Clock, backoff: _Backoff, left: float) -> Tuple[_Answer, int, Dict[str, float]]:
    """Make the request and read GitHub's answer. Returns it with its status and the blocks to share."""
    resp = http.send(req.method, req.path, req.token, accept=req.accept, json_body=req.json_body,
                     timeout=min(CALL_TIMEOUT, left))
    verdict = http.classify(resp, clock.now(), req.path)
    if verdict.kind not in ("primary", "secondary"):
        return _Answer(resp, verdict, "", 0.0), resp.status, {}
    delay = min(backoff.delay(verdict), core.BLOCK_SANITY)
    if verdict.kind == "primary":  # a bucket it doesn't track counts as the request's own
        what = verdict.bucket if verdict.bucket in core.SPACING else req.bucket
        limited_buckets = [what]
    else:
        what, limited_buckets = "every bucket", list(core.SPACING)
    return _Answer(resp, verdict, what, delay), resp.status, {b: delay for b in limited_buckets}


# ---------- helpers ----------

def _summary(method: str, path: str, json_body: Optional[dict]) -> str:
    """What was asked, for the call log: the method, the path and a GraphQL document's text."""
    query = json_body.get("query") if isinstance(json_body, dict) else None
    return f"{method} {path} {query}" if isinstance(query, str) else f"{method} {path}"


def _gave_up(method: str, path: str) -> TollboothError:
    """A 502 or 504: GitHub stopped working on the request. Only a GraphQL query can be made lighter."""
    if path == "graphql":
        return TollboothError("GitHub gave up on the query (502/504: its 10 s limit). Make it lighter: "
                              "fewer aliases, a smaller first:, fewer nested connections.", EX_HEAVY)
    return TollboothError(f"GitHub timed out on {method} {path[:60]} (502/504). Try again later, or ask for less.",
                          EX_HEAVY)
