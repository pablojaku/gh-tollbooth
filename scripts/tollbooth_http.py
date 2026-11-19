"""tollbooth_http: the one place gh-tollbooth talks to GitHub.

send() makes one HTTPS request through httpx, whatever the caller asks: only to https://api.github.com,
only GET, HEAD or POST to `graphql`, and it returns a redirect instead of following it. It does not
read the GraphQL document: telling a query from a mutation is the guard's job, not this module's.
The environment is trusted, not checked: whoever sets HTTPS_PROXY or SSL_CERT_FILE can route the
request, token included. See "Trust boundary" in gh-tollbooth.

httpx's HTTP/1.1 parser (h11) refuses a body cut off before its Content-Length or its last chunk, a
Content-Length that isn't one number, and any Transfer-Encoding but chunked. This module refuses the
rest of what can't be trusted to be whole: a body that only the closing connection ends, and one
over MAX_BODY_BYTES.
"""
from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field
from typing import Any

import httpx

API = "https://api.github.com"
USER_AGENT = "gh-tollbooth"
DEFAULT_ACCEPT = "application/vnd.github+json"
MAX_BODY_BYTES = 64 * 1024 * 1024
# Every pattern is used with fullmatch: `$` would let a trailing newline through.
# A relative API path with an optional query: printable ASCII, no spaces, and a first character
# that can't start an absolute path or another URL. build_request also refuses `//` in the path.
PATH_RE = re.compile(r"[A-Za-z0-9_][!-\"$-~]*")  # printable ASCII but `#`: a URL drops everything after a #
TOKEN_RE = re.compile(r"[!-~]{1,1024}")   # printable ASCII: no space or line break can reach the header
ACCEPT_RE = re.compile(r"[ -~]{1,200}")
MAX_TIMEOUT = 3600                # seconds; the helper thread's wait can't take much more than this


class TransportError(Exception):
    """GitHub gave no usable HTTP answer. kind is "timeout" or "error"."""

    def __init__(self, kind: str, detail: str):
        super().__init__(detail)
        self.kind = kind


@dataclass
class Response:
    status: int
    headers: dict[str, str] = field(default_factory=dict)  # lower-case names; repeated ones joined with ", "
    body: bytes = b""


# ---------- one request ----------

def build_request(method: str, path: str, token: str, accept: str | None = None,
                  json_body: Any = None) -> httpx.Request:
    """The only requests this module makes. ValueError means the caller asked for something else."""
    if method not in ("GET", "HEAD") and not (method == "POST" and path == "graphql"):
        raise ValueError(f"gh-tollbooth sends only GET, HEAD and POST graphql (not {method!r} {path[:60]!r})")
    if (json_body is not None) != (method == "POST"):
        raise ValueError("a request body goes with POST graphql and with nothing else")
    if not PATH_RE.fullmatch(path) or "//" in path.split("?", 1)[0]:
        raise ValueError(f"not a relative GitHub API path: {path[:60]!r}")
    accept = DEFAULT_ACCEPT if accept is None else accept
    if not TOKEN_RE.fullmatch(token) or not ACCEPT_RE.fullmatch(accept):
        raise ValueError("header values must be printable ASCII")  # a line break would start another header
    try:
        url, api = httpx.URL(f"{API}/{path}"), httpx.URL(API)
    except httpx.InvalidURL:
        raise ValueError(f"not a GitHub API URL: {path[:60]!r}") from None
    if (url.scheme, url.netloc) != (api.scheme, api.netloc):
        raise ValueError(f"not a GitHub API URL: {path[:60]!r}")
    headers = {"Authorization": f"Bearer {token}", "User-Agent": USER_AGENT, "Accept": accept}
    return httpx.Request(method, url, headers=headers, json=json_body)  # json= adds Content-Type


def _read_capped(r: httpx.Response) -> bytes:
    """The body, refused once it passes MAX_BODY_BYTES."""
    body = bytearray()
    for piece in r.iter_bytes():
        body += piece
        if len(body) > MAX_BODY_BYTES:
            raise TransportError("error", f"the answer is larger than {MAX_BODY_BYTES // (1024 * 1024)} MB")
    return bytes(body)


def _exchange(request: httpx.Request, timeout: float) -> Response:
    with httpx.Client(timeout=timeout) as client:  # httpx follows no redirect unless told to
        r = client.send(request, stream=True)
        try:
            framed = (request.method == "HEAD" or r.status_code in (204, 304)
                      or "content-length" in r.headers or "transfer-encoding" in r.headers)
            if not framed:  # read up to the close of the connection, a cut-off body would look whole
                raise TransportError("error", "GitHub's answer has no Content-Length and isn't chunked, "
                                              "so it can't be checked for being complete")
            body = _read_capped(r)
        finally:
            r.close()
    return Response(r.status_code, dict(r.headers.items()), body)


def _transport_error(e: Exception) -> Exception:
    """httpx failures as TransportError; the text comes from httpx, the OS or TLS, never from our headers."""
    if isinstance(e, TransportError):
        return e
    if isinstance(e, httpx.TimeoutException):
        return TransportError("timeout", "GitHub did not answer in time")
    if isinstance(e, httpx.RequestError):  # DNS, connection, TLS, a cut-off or malformed answer
        return TransportError("error", re.sub(r"\s+", " ", f"{type(e).__name__}: {e}")[:200])
    return e  # anything else is a bug in this module: let it surface


def send(method: str, path: str, token: str, *, accept: str | None = None, json_body: Any = None,
         timeout: float = 30.0) -> Response:
    """Send one request and return GitHub's answer, whatever its status.

    Raises TransportError when there is no answer. Always ends within `timeout` seconds, even on
    a hung DNS lookup or a server that drips bytes, which httpx's per-step timeouts don't cover:
    the exchange runs on a helper thread that is left behind at the deadline.
    """
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= MAX_TIMEOUT:
        raise ValueError(f"timeout must be more than 0 and at most {MAX_TIMEOUT} seconds (not {timeout!r})")
    request = build_request(method, path, token, accept, json_body)
    box: dict[str, Any] = {}

    def work():
        try:
            box["response"] = _exchange(request, timeout)
        except Exception as e:  # handed to the calling thread below
            box["error"] = e

    t = threading.Thread(target=work, name="gh-tollbooth-http", daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        raise TransportError("timeout", f"no answer in {timeout:.0f} s")
    if "error" in box:
        raise _transport_error(box["error"])
    return box["response"]
