"""tollbooth_loop: who is calling GitHub during one command, and their requests through the shared throttle.

A Caller is a label, its budget and the shared throttle. Caller.fetch() is how gh-tollbooth's commands
talk to GitHub. It starts the call's Deadline, asks for the token once per command, refuses a request
tollbooth_http would refuse before anything is charged, and takes one turn through the throttle
(Throttle.turn: budget, blocks, call gap, the lock, charging, the log). In the turn it sends the request
and reads GitHub's answer with tollbooth_http.classify():
- not a rate limit: returned as it is, 404 and 403 included;
- a primary limit stops the bucket GitHub names, a secondary limit every bucket: the turn records the
  block for every agent, waits and sends again;
- a 502 or 504 means GitHub gave up on the request: exit 3, with advice if it was a GraphQL query.
A stop on GitHub's own answer (a 502/504, a rate limit the turn won't wait out) is a StoppedOnAnswer
that carries that answer, so a command can still show it.
The Caller is the one seam between a command and GitHub: every failure comes out as a TollboothError
with its exit code (no token or no answer: 1; a request tollbooth_http would refuse: 2), so a command
catches one kind. Caller.rate_limit() asks GitHub's own counts outside the throttle (docs/design.md, Rate limits).
Not here: the turn's rules (tollbooth_throttle), the HTTP request itself (tollbooth_http) and what a
GraphQL request looks like in the call log (tollbooth_graphql).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import tollbooth_graphql as gql
import tollbooth_http as http
import tollbooth_throttle as throttling
from tollbooth_core import EX_FAIL, EX_HEAVY, EX_USAGE, TollboothError
from tollbooth_text import RESOURCE_RE
from tollbooth_throttle import StoppedOnAnswer

CALL_TIMEOUT = 90          # seconds one request may take, at most
RATE_LIMIT_TIMEOUT = 30    # seconds for GET rate_limit, which skips the throttle and its deadline


@dataclass
class Caller:
    """Who is calling GitHub during one command: a label, its budget and the shared throttle."""
    label: str                      # whose budget each request is charged to
    budget: int | None              # requests allowed for the label in 24 h; None: no limit
    throttle: throttling.Throttle
    _token: str | None = field(default=None, init=False, repr=False, compare=False)  # never in a repr

    def token(self) -> str:
        """The GitHub token, asked for on first use (`gh auth token` starts a process), then kept.
        No token is exit 1."""
        if self._token is None:
            try:
                self._token = http.token()
            except http.NoToken as e:
                raise TollboothError(str(e), EX_FAIL) from None
        return self._token

    def check(self, bucket: str) -> None:
        """Fail before doing any work if this caller can't call `bucket` now: the budget is used up
        (exit 4) or the bucket is blocked for longer than TOLLBOOTH_MAX_WAIT (exit 75)."""
        self.throttle.check(self.label, self.budget, bucket)

    def fetch(self, method: str, path: str, *, accept: str | None = None, json_body: Any = None) -> http.Response:
        """Send one request through the shared throttle and return GitHub's answer.

        Any answer that isn't a rate limit is returned as it is, 404 and 403 included.
        Raises only TollboothError: exit 1 (no token, no answer from GitHub), 2 (a request tollbooth_http
        refuses, before anything is charged), 3, 4, 75 or 76; a StoppedOnAnswer when it stops on GitHub's
        answer. A request is charged just before it is sent, and only if it is sent.
        """
        clock = self.throttle.clock
        deadline = throttling.Deadline(clock)  # first: the time `gh auth token` takes counts too
        token = self.token()
        try:
            http.build_request(method, path, token, accept, json_body)  # a request it would refuse costs nothing
        except ValueError as e:
            raise TollboothError(f"refused: {e}", EX_USAGE) from None
        bucket = throttling.infer_bucket(path)

        def send(left: float) -> tuple[http.Response, int, throttling.Limit | None]:
            resp = http.send(method, path, token, accept=accept, json_body=json_body,
                             timeout=min(CALL_TIMEOUT, left))
            return resp, resp.status, _limit(http.classify(resp, clock.now(), path), bucket)

        try:
            resp = self.throttle.turn(self.label, self.budget, bucket, deadline, gql.summary(method, path, json_body),
                                      send)
        except http.TransportError as e:  # charged and logged by the turn
            raise _no_answer(e) from None
        if http.classify(resp, clock.now(), path).kind == "heavy":
            raise StoppedOnAnswer(_gave_up(method, path), resp)
        return resp

    def rate_limit(self) -> bytes:
        """GitHub's rate_limit document, asked for outside the throttle: no Budget charge, no call gap, no
        Block check, no log line (docs/design.md, Rate limits). GitHub doesn't count it, and `status` is what you run while
        buckets are blocked. Another answer than 200, or none, is exit 1."""
        try:
            resp = http.send("GET", "rate_limit", self.token(), timeout=RATE_LIMIT_TIMEOUT)
        except http.TransportError as e:
            raise _no_answer(e) from None
        if resp.status != 200:
            hint = http.TOKEN_REJECTED_HINT if resp.status == 401 else ""
            raise TollboothError(f"GET rate_limit answered HTTP {resp.status}{hint}", EX_FAIL)
        return resp.body


def _no_answer(e: http.TransportError) -> TollboothError:
    return TollboothError(f"no answer from GitHub ({e})", EX_FAIL)


def _limit(verdict: http.Verdict, bucket: str) -> throttling.Limit | None:
    """GitHub's verdict as the turn's Limit, or None if it isn't a rate limit.

    A primary limit stops the bucket GitHub names (the request's own, if it names none), but only if
    the throttle tracks that bucket; a secondary limit stops every bucket. A message repeats GitHub's
    name only if it is a plain name: header text could drive the terminal.
    """
    if verdict.kind == "primary":
        named = verdict.bucket or bucket
        what = named if RESOURCE_RE.fullmatch(named) else "an untracked resource"
        return throttling.Limit(what, (named,) if named in throttling.BUCKETS else (), verdict.delay)
    if verdict.kind == "secondary":
        return throttling.Limit("every bucket", throttling.BUCKETS, verdict.delay)
    return None


def _gave_up(method: str, path: str) -> TollboothError:
    """A 502 or 504: GitHub stopped working on the request. Only a GraphQL query can be made lighter."""
    if path == "graphql":
        return TollboothError(gql.HEAVY_QUERY_ADVICE, EX_HEAVY)
    return TollboothError(f"GitHub timed out on {method} {path[:60]} (502/504). Try again later, or ask for less.",
                          EX_HEAVY)
