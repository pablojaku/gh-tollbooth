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
Not here: the turn's rules (tollbooth_throttle) and the HTTP request itself (tollbooth_http).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import tollbooth_http as http
import tollbooth_throttle as throttling
from tollbooth_core import EX_HEAVY, TollboothError
from tollbooth_text import RESOURCE_RE
from tollbooth_throttle import StoppedOnAnswer

CALL_TIMEOUT = 90          # seconds one request may take, at most


@dataclass
class Caller:
    """Who is calling GitHub during one command: a label, its budget and the shared throttle."""
    label: str                      # whose budget each request is charged to
    budget: int | None              # requests allowed for the label in 24 h; None: no limit
    throttle: throttling.Throttle
    _token: str | None = field(default=None, init=False, repr=False, compare=False)  # never in a repr

    def token(self) -> str:
        """The GitHub token, asked for on first use (`gh auth token` starts a process), then kept."""
        if self._token is None:
            self._token = http.token()
        return self._token

    def check(self, bucket: str) -> None:
        """Fail before doing any work if this caller can't call `bucket` now: the budget is used up
        (exit 4) or the bucket is blocked for longer than TOLLBOOTH_MAX_WAIT (exit 75)."""
        self.throttle.check(self.label, self.budget, bucket)

    def fetch(self, method: str, path: str, *, accept: str | None = None, json_body: Any = None) -> http.Response:
        """Send one request through the shared throttle and return GitHub's answer.

        Any answer that isn't a rate limit is returned as it is, 404 and 403 included.
        Raises TollboothError (exit 3, 4, 75, 76; a StoppedOnAnswer when it stops on GitHub's answer),
        NoToken, TransportError, or ValueError for a request tollbooth_http refuses. A request is
        charged just before it is sent, and only if it is sent.
        """
        clock = self.throttle.clock
        deadline = throttling.Deadline(clock)  # first: the time `gh auth token` takes counts too
        token = self.token()
        http.build_request(method, path, token, accept, json_body)  # a request it would refuse costs nothing
        bucket = throttling.infer_bucket(path)

        def send(left: float) -> tuple[http.Response, int, throttling.Limit | None]:
            resp = http.send(method, path, token, accept=accept, json_body=json_body,
                             timeout=min(CALL_TIMEOUT, left))
            return resp, resp.status, _limit(http.classify(resp, clock.now(), path), bucket)

        resp = self.throttle.turn(self.label, self.budget, bucket, deadline, _summary(method, path, json_body),
                                  send)
        if http.classify(resp, clock.now(), path).kind == "heavy":
            raise StoppedOnAnswer(_gave_up(method, path), resp)
        return resp


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


def _summary(method: str, path: str, json_body: Any) -> str:
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
