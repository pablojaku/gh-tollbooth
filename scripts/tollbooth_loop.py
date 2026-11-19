"""tollbooth_loop: one GitHub request through the shared throttle, for the command that asks for it.

fetch() is how gh-tollbooth's commands talk to GitHub. It starts the call's Deadline, asks for the
token once, refuses a request tollbooth_http would refuse before anything is charged, and takes one
turn through the throttle (Throttle.turn: budget, blocks, call gap, the lock, charging, the log). In
the turn it sends the request and reads GitHub's answer with tollbooth_http.classify():
- not a rate limit: returned as it is, 404 and 403 included;
- a primary limit stops the bucket GitHub names, a secondary limit every bucket: the turn records the
  block for every agent, waits and sends again;
- a 502 or 504 means GitHub gave up on the request: exit 3, with advice if it was a GraphQL query.
Not here: the turn's rules (tollbooth_throttle) and the HTTP request itself (tollbooth_http).
"""
from __future__ import annotations

from typing import Any, Protocol

import tollbooth_core as core
import tollbooth_http as http
import tollbooth_throttle as throttling
from tollbooth_core import EX_HEAVY, TollboothError

CALL_TIMEOUT = 90          # seconds one request may take, at most


class FetchContext(Protocol):
    """What fetch() needs from the command that calls it."""
    label: str                      # whose budget each request is charged to
    budget: int | None              # requests allowed for the label in 24 h; None: no limit
    throttle: throttling.Throttle
    token: str | None               # None until the first request asks for it; then kept here


def fetch(ctx: FetchContext, method: str, path: str, *, accept: str | None = None,
          json_body: Any = None) -> http.Response:
    """Send one request through the shared throttle and return GitHub's answer.

    Any answer that isn't a rate limit is returned as it is, 404 and 403 included.
    Raises TollboothError (exit 3, 4, 75, 76), NoToken, TransportError, or ValueError for a request
    tollbooth_http refuses. A request is charged just before it is sent, and only if it is sent.
    """
    clock = ctx.throttle.clock
    deadline = throttling.Deadline(clock)  # first: the time `gh auth token` takes counts too
    if ctx.token is None:
        ctx.token = http.token()
    token = ctx.token
    http.build_request(method, path, token, accept, json_body)  # a request it would refuse costs nothing
    bucket = core.infer_bucket(path)

    def send(left: float) -> tuple[tuple[http.Response, bool], int, throttling.Limit | None]:
        resp = http.send(method, path, token, accept=accept, json_body=json_body, timeout=min(CALL_TIMEOUT, left))
        verdict = http.classify(resp, clock.now(), path)
        return (resp, verdict.kind == "heavy"), resp.status, _limit(verdict, bucket)

    resp, heavy = ctx.throttle.turn(ctx.label, ctx.budget, bucket, deadline, _summary(method, path, json_body),
                                    send)
    if heavy:
        raise _gave_up(method, path)
    return resp


def _limit(verdict: http.Verdict, bucket: str) -> throttling.Limit | None:
    """GitHub's verdict as the turn's Limit, or None if it isn't a rate limit.

    A primary limit stops the bucket GitHub names (the request's own, if it names none), but only if
    the throttle tracks that bucket; a secondary limit stops every bucket.
    """
    if verdict.kind == "primary":
        named = verdict.bucket or bucket
        return throttling.Limit(named, (named,) if named in throttling.SPACING else (), verdict.delay)
    if verdict.kind == "secondary":
        return throttling.Limit("every bucket", tuple(throttling.SPACING), verdict.delay)
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
