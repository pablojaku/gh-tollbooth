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

classify() decides about rate limits from the status code and GitHub's rate-limit headers, never
from error text, which can echo a query. The one body field it reads is a 403's
`documentation_url`, which GitHub sets: a secondary limit can arrive without those headers.

The token (GH_TOKEN, GITHUB_TOKEN or `gh auth token`) goes in the Authorization header only.
"""
from __future__ import annotations

import os
import re
import subprocess
import threading
from dataclasses import dataclass, field
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, ValidationError

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
# The documentation_url GitHub sends with "You have exceeded a secondary rate limit", for example
# https://docs.github.com/free-pro-team@latest/rest/overview/rate-limits-for-the-rest-api#about-secondary-rate-limits
SECONDARY_DOC_RE = re.compile(r"https://docs\.github\.com/[!-\"$-~]*#[a-z-]*secondary-rate-limits")
MAX_ERROR_BODY_BYTES = 64 * 1024  # GitHub's error objects are a few hundred bytes
MAX_TIMEOUT = 3600                # seconds; the helper thread's wait can't take much more than this
MAX_HEADER_NUMBER = 10 ** 12      # seconds; no real Retry-After or reset time comes near it

# `gh auth token` gets only these variables: no agent secrets, no GH_HOST. The proxy and
# certificate ones are kept because gh may refresh an expiring token.
GH_ENV_KEEP = {"PATH", "HOME", "USER", "LOGNAME", "TMPDIR", "LANG", "LC_ALL", "LC_CTYPE",
               "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_RUNTIME_DIR",
               "DBUS_SESSION_BUS_ADDRESS", "GH_CONFIG_DIR", "SSL_CERT_FILE", "SSL_CERT_DIR",
               "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "https_proxy", "http_proxy", "no_proxy"}
GH_ENV_PIN = {"GH_PROMPT_DISABLED": "1", "GH_NO_UPDATE_NOTIFIER": "1"}
GH_TOKEN_TIMEOUT = 15


class NoToken(Exception):
    """There is no usable GitHub token. The message says how to get one and never holds a token."""


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


@dataclass
class Verdict:
    kind: str | None = None      # None | "primary" | "secondary" | "heavy"
    delay: float | None = None   # seconds GitHub told us to wait (reset or Retry-After), if it did
    bucket: str | None = None    # X-RateLimit-Resource, if present


# ---------- token ----------

def gh_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k in GH_ENV_KEEP}
    env.update(GH_ENV_PIN)
    return env


def _checked(value: str, source: str) -> str:
    if not TOKEN_RE.fullmatch(value):  # never echo the value
        raise NoToken(f"the token from {source} has characters a GitHub token can't contain")
    return value


def token() -> str:
    """The token for github.com, in gh's own order: GH_TOKEN, GITHUB_TOKEN, then gh's stored login."""
    for var in ("GH_TOKEN", "GITHUB_TOKEN"):
        value = os.environ.get(var, "").strip()
        if value:
            return _checked(value, var)
    try:
        p = subprocess.run(["gh", "auth", "token", "--hostname", "github.com"], capture_output=True, text=True,
                           errors="replace", timeout=GH_TOKEN_TIMEOUT, env=gh_env())
    except FileNotFoundError:
        raise NoToken("no GitHub token: set GH_TOKEN, or install the GitHub CLI and run `gh auth login`")
    except subprocess.TimeoutExpired:
        raise NoToken(f"`gh auth token` did not answer in {GH_TOKEN_TIMEOUT} s; set GH_TOKEN instead")
    except OSError as e:  # gh is there but can't run: no execute bit, a directory, the wrong CPU type
        raise NoToken(f"can't run `gh` ({type(e).__name__}); set GH_TOKEN, or reinstall the GitHub CLI")
    value = p.stdout.strip()
    if p.returncode != 0 or not value:
        raise NoToken("no GitHub token: run `gh auth login`, or set GH_TOKEN")
    return _checked(value, "`gh auth token`")


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


# ---------- rate limits, from the status and headers ----------

class _ErrorBody(BaseModel):
    """The one field of a GitHub error object that classify() reads. GitHub fills it; no request can."""

    model_config = ConfigDict(strict=True)  # a list or a number there is not a URL
    documentation_url: str | None = None


def _int(value) -> int | None:
    """A whole number from a header, or None for anything else (a number too big to be a time, too)."""
    try:
        n = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return n if abs(n) <= MAX_HEADER_NUMBER else None


def _links_secondary_limit_docs(body: bytes) -> bool:
    """True if a 403's body is an error object whose documentation_url is GitHub's secondary-limit page.

    pydantic parses and checks the body. Anything but a JSON object with a string there is not one,
    deep nesting included: pydantic's parser stops at a fixed depth on every Python build.
    """
    if len(body) > MAX_ERROR_BODY_BYTES:
        return False
    try:
        url = _ErrorBody.model_validate_json(body).documentation_url
    except ValidationError:
        return False
    return url is not None and SECONDARY_DOC_RE.fullmatch(url) is not None


def classify(resp: Response, wall_now: float, failed: bool = False) -> Verdict:
    """Decide from the HTTP status and GitHub's headers, plus one GitHub-set field of a 403's body.

    `failed` says a 200 answer carried GraphQL errors (GitHub reports a spent GraphQL limit that way).

    - 502/504                                         -> heavy
    - 403/429 with Retry-After                        -> secondary, wait Retry-After
    - 403/429, or a failed 200, with X-RateLimit-Remaining 0 -> primary until the reset time
    - 429 without those headers                       -> secondary (the caller picks the backoff)
    - 403 without them whose documentation_url is the secondary-limit page -> secondary (same)
    - anything else (permission 403, 404, GraphQL NOT_FOUND, ...) -> None: not a rate limit
    """
    h = resp.headers
    bucket = h.get("x-ratelimit-resource")
    if resp.status in (502, 504):
        return Verdict("heavy")
    if not (resp.status in (403, 429) or (resp.status == 200 and failed)):
        return Verdict()
    retry = _int(h.get("retry-after"))
    if retry is not None and resp.status in (403, 429):
        return Verdict("secondary", float(max(1, retry)), bucket)
    reset = _int(h.get("x-ratelimit-reset"))
    if h.get("x-ratelimit-remaining") == "0" and reset is not None:
        return Verdict("primary", max(1.0, reset + 2 - wall_now), bucket)
    if resp.status == 429 or (resp.status == 403 and _links_secondary_limit_docs(resp.body)):
        return Verdict("secondary", None, bucket)
    return Verdict()
