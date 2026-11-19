"""tollbooth_guard: what `gh-tollbooth call` may ask GitHub. Fail closed: anything not allowed is refused.

`call` takes the arguments agents already know from `gh api` and turns them into one read-only
request: REST GET or HEAD, or a GraphQL query (POST to `graphql`). gh-tollbooth reads the arguments
itself; no `gh` process runs. Every option it accepts is listed here, with the hint an agent needs
when it reaches for one that is gone (--jq, --template, --cache, --paginate).

GraphQL documents are checked so only `query` operations and fragments pass: strings and comments
are dropped first, so a keyword can't hide in either. Stdlib only, Python 3.9+.
"""
from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass
from typing import Dict, List, NoReturn, Optional, Tuple, Union

from tollbooth_core import EX_USAGE, TollboothError

# All patterns are used with fullmatch (Python's `$` would let a trailing newline through).
# An endpoint starts with a letter, digit or `_`, so it can never be read as an option. No `{owner}`
# placeholders and no `%` in the path: `%67raphql` could decode to `graphql` on GitHub's side.
ENDPOINT_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.~-]*(/[A-Za-z0-9_.~-]+)*(\?[A-Za-z0-9_.~%=&+:,*/-]*)?")
FIELD_KEY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
PLACEHOLDER_RE = re.compile(r":(owner|repo|branch)\b", re.ASCII)  # gh's old-style placeholders
ACCEPT_HEADER_RE = re.compile(r"accept\s*:(.*)", re.I | re.S)
ACCEPT_VALUE_RE = re.compile(r"[ -~]{1,200}")  # printable ASCII: a line break would start another header
INT_RE = re.compile(r"[+-]?[0-9]{1,19}")
INT64 = 2 ** 63
# Control chars (except tab and newline), CR (a GraphQL line terminator), NEL, zero-width and
# bidi-control characters, Unicode line/paragraph separators and the BOM. Built from code points
# so the source file itself contains no invisible characters.
_BAD_CODEPOINTS = [*range(0x00, 0x09), *range(0x0B, 0x20), 0x7F, 0x85, *range(0x200B, 0x2010),
                   0x2028, 0x2029, *range(0x202A, 0x202F), *range(0x2066, 0x206A), 0xFEFF]
BAD_CHARS_RE = re.compile("[" + re.escape("".join(map(chr, _BAD_CODEPOINTS))) + "]")
# Lone surrogates: how Python decodes argv bytes that aren't UTF-8 (U+DC80-U+DCFF); no request can carry them.
NOT_UTF8_RE = re.compile("[\ud800-\udfff]")

VALUE_OPTIONS = {"-X": "method", "--method": "method", "-f": "raw", "--raw-field": "raw",
                 "-F": "typed", "--field": "typed", "-H": "header", "--header": "header"}
FLAG_OPTIONS = {"-i": "include", "--include": "include", "--silent": "silent"}
JQ_HINT = ("{opt} is gone: call prints GitHub's JSON as it is, so pipe the output to jq instead, "
           "e.g. gh-tollbooth call -- api graphql -f query='...' | jq '.data'")
GONE_OPTIONS = {
    "-q": JQ_HINT, "--jq": JQ_HINT, "-t": JQ_HINT, "--template": JQ_HINT,
    "--cache": "{opt} is gone: nothing is cached any more, every call is a live, charged request.",
    "--paginate": "{opt} is gone: one call is one request, so budgets and spacing stay honest. "
                  "Page by hand: -f per_page=100 -f page=N, or a GraphQL cursor (first:, after:).",
}
GONE_OPTIONS["--slurp"] = GONE_OPTIONS["--paginate"]
ALLOWED = ("-X GET|HEAD, -f/-F key=value, -H 'Accept: ...', -i, --silent. Short options must be separate "
           "tokens (-X GET, not -XGET); gh-tollbooth's own --as and --budget go before `--`.")

Value = Union[str, int, bool, None]


@dataclass(frozen=True)
class CallRequest:
    """One read-only request, as `call` was asked to make it."""
    method: str                 # GET or HEAD for REST, POST for GraphQL
    path: str                   # relative API path, query string included
    accept: Optional[str]       # the Accept header, if one was given
    json_body: Optional[dict]   # GraphQL only: {"query": ..., "variables": {...}}
    include: bool               # -i: print the status line and headers before the body
    silent: bool                # --silent: print nothing on stdout


def _refuse(msg: str) -> NoReturn:
    raise TollboothError(msg, EX_USAGE)


# ---------- GraphQL: only `query` operations and fragments ----------

def _strip_strings_and_comments(doc: str) -> str:
    """Single-pass lexer: drop comments, collapse strings, so keywords can't hide in either."""
    out, i, n = [], 0, len(doc)
    while i < n:
        c = doc[i]
        if c == "#":  # a comment ends at \n or \r (GraphQL spec LineTerminator)
            ends = [k for k in (doc.find("\n", i), doc.find("\r", i)) if k >= 0]
            i = min(ends) if ends else n
        elif doc.startswith('"""', i):
            j = i + 3
            while True:
                k = doc.find('"""', j)
                if k < 0:
                    raise ValueError("unterminated block string")
                if doc[k - 1] == "\\":
                    j = k + 3
                    continue
                break
            out.append('""')
            i = k + 3
        elif c == '"':
            j = i + 1
            while j < n and doc[j] not in '"\n':
                j += 2 if doc[j] == "\\" else 1
            if j >= n or doc[j] == "\n":
                raise ValueError("unterminated string")
            out.append('""')
            i = j + 1
        else:
            out.append(c)
            i += 1
    return "".join(out)


def graphql_definitions(doc: str) -> List[str]:
    """Top-level definition kinds in a GraphQL document: 'query', 'mutation', 'fragment', ..."""
    s = _strip_strings_and_comments(doc)
    depth = paren = 0
    kinds, expect_def = [], True
    for m in re.finditer(r"[{}()]|[_A-Za-z][_0-9A-Za-z]*", s):
        t = m.group()
        if t == "(":
            paren += 1
        elif t == ")":
            paren -= 1
        elif t == "{":
            if depth == 0 and paren == 0 and expect_def:
                kinds.append("query")  # anonymous shorthand: { viewer { login } }
            depth += 1
            expect_def = False
        elif t == "}":
            depth -= 1
            if depth == 0:
                expect_def = True
        elif depth == 0 and paren == 0 and expect_def:
            kinds.append(t)
            expect_def = False
        if depth < 0 or paren < 0:
            raise ValueError("unbalanced braces")
    if depth or paren:
        raise ValueError("unbalanced braces")
    return kinds


def check_graphql_document(doc: str) -> None:
    """Refuse (exit 2) a document that isn't only query operations and fragments, or doesn't parse."""
    try:
        kinds = graphql_definitions(doc)
    except ValueError as e:
        _refuse(f"GraphQL document doesn't parse ({e}); gh-tollbooth refuses what it can't verify")
    bad = [k for k in kinds if k not in ("query", "fragment")]
    if not kinds or "query" not in kinds or bad:
        _refuse(f"only GraphQL query operations are allowed in gh-tollbooth (found: {', '.join(kinds) or 'nothing'})")


# ---------- the arguments ----------

def _typed(value: str) -> Value:
    """A -F value as gh reads it: true, false, null and integers become JSON values; the rest stays text."""
    if value in ("true", "false"):
        return value == "true"
    if value == "null":
        return None
    if INT_RE.fullmatch(value) and -INT64 <= int(value) < INT64:
        return int(value)
    return value


def _query_text(value: Value) -> str:
    """A field's value as a REST query parameter, the way gh writes it (null is empty)."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _read_args(args: List[str]) -> Tuple[Optional[str], Optional[str], List[str], List[Tuple[str, str]],
                                         Dict[str, bool]]:
    """Split the args after `api` into endpoint, method, headers, fields [(kind, "k=v")] and flags."""
    endpoint, method = None, None
    headers: List[str] = []
    fields: List[Tuple[str, str]] = []
    flags = {"include": False, "silent": False}
    i = 0
    while i < len(args):
        a = args[i]
        name, val = a, None
        if a.startswith("--") and "=" in a:
            name, val = a.split("=", 1)
        if name in GONE_OPTIONS:
            _refuse(GONE_OPTIONS[name].format(opt=name))
        if name in FLAG_OPTIONS and val is None:
            flags[FLAG_OPTIONS[name]] = True
            i += 1
            continue
        if name in VALUE_OPTIONS:
            if val is None:
                if i + 1 >= len(args):
                    _refuse(f"{name} needs a value")
                val, i = args[i + 1], i + 2
            else:
                i += 1
            kind = VALUE_OPTIONS[name]
            if kind == "method":
                if method is not None:
                    _refuse("give -X/--method at most once")
                method = val.upper()
            elif kind == "header":
                headers.append(val)
            else:
                fields.append((kind, val))
            continue
        if a.startswith("-"):
            _refuse(f"option {a!r} is not allowed in gh-tollbooth call. Allowed: {ALLOWED}")
        if endpoint is not None:
            _refuse(f"call takes one endpoint (got {endpoint!r} and {a!r})")
        endpoint, i = a, i + 1
    return endpoint, method, headers, fields, flags


def _endpoint(endpoint: Optional[str]) -> str:
    if endpoint is None:
        _refuse("call needs an endpoint, e.g. api repos/OWNER/REPO or api graphql -f query='...'")
    ep = endpoint.lstrip("/")
    if "://" in endpoint or endpoint.startswith("//") or not ENDPOINT_RE.fullmatch(ep):
        _refuse(f"endpoint {endpoint!r} not allowed: use a relative API path like repos/OWNER/REPO or graphql")
    if PLACEHOLDER_RE.search(ep):
        _refuse(f"endpoint {endpoint!r} not allowed: gh-tollbooth fills no :owner/:repo/:branch; name them")
    path = ep.split("?", 1)[0].rstrip("/").lower()
    if path in ("graphql", "api/graphql") and ep != "graphql":
        _refuse("send GraphQL only as `api graphql -f query='...'` (no query string), so the document gets checked")
    if any(seg in (".", "..") for seg in path.split("/")):
        _refuse(f"endpoint {endpoint!r} not allowed: no '.' or '..' path segments")
    return ep


def _accept(headers: List[str]) -> Optional[str]:
    if len(headers) > 1:
        _refuse("give at most one -H 'Accept: ...' header")
    for h in headers:
        m = ACCEPT_HEADER_RE.fullmatch(h)
        value = m.group(1).strip() if m else ""
        if not m or not ACCEPT_VALUE_RE.fullmatch(value):
            _refuse(f"only an Accept header with a printable value is allowed in gh-tollbooth (got {h[:60]!r})")
        return value
    return None


def _fields(fields: List[Tuple[str, str]], graphql: bool) -> List[Tuple[str, Value]]:
    out: List[Tuple[str, Value]] = []
    for kind, kv in fields:
        key, sep, value = kv.partition("=")
        if not sep or not FIELD_KEY_RE.fullmatch(key):
            _refuse(f"a field must be key=value with a plain key, letters, digits and _ (got {kv[:60]!r})")
        document = graphql and key == "query"
        if kind == "typed" and value.startswith("@"):
            _refuse("-F key=@file is not allowed: gh-tollbooth never reads local files; pass the value inline")
        if kind == "typed" and not document and ("{" in value or "}" in value or PLACEHOLDER_RE.search(value)):
            _refuse(f"-F value {kv[:60]!r} may not contain {{...}} or :owner/:repo/:branch; use -f for text values")
        if "\n" in value and not document:
            _refuse(f"newlines are only allowed in the GraphQL query document (field {key!r})")
        out.append((key, value if kind == "raw" or document else _typed(value)))
    return out


def parse_call(args: List[str]) -> CallRequest:
    """Validate `gh-tollbooth call` arguments (`api ...`) and return the one request they ask for.

    Raises TollboothError (exit 2) for anything that isn't a read-only GitHub API request.
    """
    if not args:
        _refuse("no request given (use: gh-tollbooth call -- api <endpoint> ...)")
    for a in args:
        if NOT_UTF8_RE.search(a):
            _refuse(f"arguments must be valid UTF-8 text (in {a[:40]!r})")
        if BAD_CHARS_RE.search(a):
            _refuse(f"control or invisible characters are not allowed in gh-tollbooth arguments (in {a[:40]!r})")
    if args[0] != "api":
        _refuse(f"call makes GitHub API requests only, not `gh {args[0][:20]}`. Use the API form, "
                "e.g. gh-tollbooth call -- api repos/OWNER/REPO/pulls or api search/code -f q='...'")
    endpoint, method, headers, raw_fields, flags = _read_args(args[1:])
    ep = _endpoint(endpoint)
    accept = _accept(headers)
    fields = _fields(raw_fields, graphql=ep == "graphql")
    if ep == "graphql":
        if method is not None:
            _refuse("GraphQL requests are POST; don't set -X")
        return CallRequest("POST", ep, accept, _graphql_body(fields), flags["include"], flags["silent"])
    method = method or "GET"
    if method not in ("GET", "HEAD"):
        _refuse(f"REST method {method[:10]} is not read-only: gh-tollbooth sends GET or HEAD")
    if fields:
        query = urllib.parse.urlencode([(k, _query_text(v)) for k, v in fields])
        ep += ("&" if "?" in ep else "?") + query
    return CallRequest(method, ep, accept, None, flags["include"], flags["silent"])


def _graphql_body(fields: List[Tuple[str, Value]]) -> dict:
    """{"query": ..., "operationName": ..., "variables": {...}}: every other field is a variable, as in gh."""
    body: dict = {}
    variables: Dict[str, Value] = {}
    for key, value in fields:
        if key in body or key in variables:
            _refuse(f"give the field {key!r} once")
        if key == "query":
            check_graphql_document(str(value))
            body["query"] = value
        elif key == "operationName":
            body["operationName"] = str(value)
        else:
            variables[key] = value
    if "query" not in body:
        _refuse("a GraphQL call needs -f query='...'")
    if variables:
        body["variables"] = variables
    return body
