"""tollbooth_guard: what `gh-tollbooth call` may ask GitHub. Fail closed: anything not allowed is refused.

`call` takes the arguments agents already know from `gh api` and turns them into one read-only
request: REST GET or HEAD, or a GraphQL query (POST to `graphql`). gh-tollbooth reads the arguments
itself, with a Click command; no `gh` process runs. Every option it accepts is listed here, with the
hint an agent needs when it reaches for one that is gone (--jq, --template, --cache, --paginate).

GraphQL documents are parsed with graphql-core, and only `query` operations and fragments pass. A
document that doesn't parse is refused too: gh-tollbooth refuses what it can't verify. Strings and
comments are the parser's business, so a keyword can't hide in either.
"""
from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass
from typing import NoReturn

import click
from graphql import FragmentDefinitionNode, GraphQLError, OperationDefinitionNode, parse

from tollbooth_core import EX_USAGE, TollboothError
from tollbooth_text import has_hidden, is_utf8

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

JQ_HINT = ("{opt} is gone: call prints GitHub's JSON as it is, so pipe the output to jq instead, "
           "e.g. gh-tollbooth call -- api graphql -f query='...' | jq '.data'")
PAGINATE_HINT = ("{opt} is gone: one call is one request, so budgets and spacing stay honest. "
                 "Page by hand: -f per_page=100 -f page=N, or a GraphQL cursor (first:, after:).")
GONE_HINTS = {"jq": JQ_HINT, "template": JQ_HINT, "paginate": PAGINATE_HINT, "slurp": PAGINATE_HINT,
              "cache": "{opt} is gone: nothing is cached any more, every call is a live, charged request."}
ALLOWED = ("-X GET|HEAD, -f/-F key=value, -H 'Accept: ...', -i, --silent. gh-tollbooth's own --as and "
           "--budget go before `--`.")

Value = str | int | bool | None


@dataclass(frozen=True)
class CallRequest:
    """One read-only request, as `call` was asked to make it."""
    method: str                 # GET or HEAD for REST, POST for GraphQL
    path: str                   # relative API path, query string included
    accept: str | None          # the Accept header, if one was given
    json_body: dict | None      # GraphQL only: {"query": ..., "variables": {...}}
    include: bool               # -i: print the status line and headers before the body
    silent: bool                # --silent: print nothing on stdout


def _refuse(msg: str) -> NoReturn:
    raise TollboothError(msg, EX_USAGE)


# ---------- GraphQL: only `query` operations and fragments ----------

def graphql_definitions(doc: str) -> list[str]:
    """The kinds of a GraphQL document's top-level definitions: 'query', 'mutation', 'fragment', ...

    Raises GraphQLError for a document that doesn't parse, or is nested too deeply to parse.
    """
    kinds = []
    for node in parse(doc, no_location=True).definitions:
        if isinstance(node, OperationDefinitionNode):
            kinds.append(node.operation.value)  # an anonymous `{ ... }` is a query too
        elif isinstance(node, FragmentDefinitionNode):
            kinds.append("fragment")
        else:
            kinds.append(node.kind)  # a schema definition (type, directive, ...): never a query
    return kinds


def check_graphql_document(doc: str) -> None:
    """Refuse (exit 2) a document that isn't only query operations and fragments, or doesn't parse."""
    try:
        kinds = graphql_definitions(doc)
    except GraphQLError as e:
        _refuse(f"GraphQL document doesn't parse ({e.message}); gh-tollbooth refuses what it can't verify")
    if "query" not in kinds or any(k not in ("query", "fragment") for k in kinds):
        _refuse(f"only GraphQL query operations are allowed in gh-tollbooth (found: {', '.join(kinds)})")


# ---------- the arguments: `gh api`'s options, read by Click ----------

def _gone(ctx: click.Context, param: click.Parameter, value: object) -> None:
    """A gh option gh-tollbooth doesn't have: refuse it with the hint an agent needs."""
    if value:
        _refuse(GONE_HINTS[str(param.name)].format(opt="/".join(param.opts)))


@click.command(add_help_option=False)
@click.argument("endpoint", required=False)
@click.option("-X", "--method", "methods", multiple=True)
@click.option("-f", "--raw-field", "raw_fields", multiple=True)
@click.option("-F", "--field", "typed_fields", multiple=True)
@click.option("-H", "--header", "headers", multiple=True)
@click.option("-i", "--include", is_flag=True)
@click.option("--silent", is_flag=True)
@click.option("-q", "--jq", hidden=True, expose_value=False, callback=_gone)
@click.option("-t", "--template", hidden=True, expose_value=False, callback=_gone)
@click.option("--cache", hidden=True, expose_value=False, callback=_gone)
@click.option("--paginate", is_flag=True, hidden=True, expose_value=False, callback=_gone)
@click.option("--slurp", is_flag=True, hidden=True, expose_value=False, callback=_gone)
def _api_args(**_: object) -> None:
    """The arguments after `call -- api`. Only parsed (make_context), never run."""


def _read_args(args: list[str]) -> dict:
    """The arguments after `api`, read by Click; anything it doesn't know is refused with what is allowed."""
    if "--" in args:  # Click would read what follows as the endpoint
        _refuse(f"'--' is not allowed after `api`. Allowed: {ALLOWED}")
    try:
        params = _api_args.make_context("api", list(args)).params
    except click.ClickException as e:
        _refuse(f"{e.format_message()} Allowed: {ALLOWED}")
    if len(params["methods"]) > 1:
        _refuse("give -X/--method at most once")
    return params


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


def _endpoint(endpoint: str | None) -> str:
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


def _accept(headers: tuple[str, ...]) -> str | None:
    if len(headers) > 1:
        _refuse("give at most one -H 'Accept: ...' header")
    for h in headers:
        m = ACCEPT_HEADER_RE.fullmatch(h)
        value = m.group(1).strip() if m else ""
        if not m or not ACCEPT_VALUE_RE.fullmatch(value):
            _refuse(f"only an Accept header with a printable value is allowed in gh-tollbooth (got {h[:60]!r})")
        return value
    return None


def _fields(fields: list[tuple[str, str]], graphql: bool) -> list[tuple[str, Value]]:
    out: list[tuple[str, Value]] = []
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


def parse_call(args: list[str]) -> CallRequest:
    """Validate `gh-tollbooth call` arguments (`api ...`) and return the one request they ask for.

    Raises TollboothError (exit 2) for anything that isn't a read-only GitHub API request.
    """
    if not args:
        _refuse("no request given (use: gh-tollbooth call -- api <endpoint> ...)")
    for a in args:
        if not is_utf8(a):
            _refuse(f"arguments must be valid UTF-8 text (in {a[:40]!r})")
        if has_hidden(a):
            _refuse("hidden characters (control, format or bidi characters, variation selectors) are not allowed "
                    f"in gh-tollbooth arguments (in {a[:40]!r})")
    if args[0] != "api":
        _refuse(f"call makes GitHub API requests only, not `gh {args[0][:20]}`. Use the API form, "
                "e.g. gh-tollbooth call -- api repos/OWNER/REPO/pulls or api search/code -f q='...'")
    p = _read_args(args[1:])
    ep = _endpoint(p["endpoint"])
    accept = _accept(p["headers"])
    # -f fields first, then -F, each in the order given: the order gh reads them in
    raw = [("raw", kv) for kv in p["raw_fields"]] + [("typed", kv) for kv in p["typed_fields"]]
    fields = _fields(raw, graphql=ep == "graphql")
    method = p["methods"][0].upper() if p["methods"] else None
    if ep == "graphql":
        if method is not None:
            _refuse("GraphQL requests are POST; don't set -X")
        return CallRequest("POST", ep, accept, _graphql_body(fields), p["include"], p["silent"])
    method = method or "GET"
    if method not in ("GET", "HEAD"):
        _refuse(f"REST method {method[:10]} is not read-only: gh-tollbooth sends GET or HEAD")
    if fields:
        query = urllib.parse.urlencode([(k, _query_text(v)) for k, v in fields])
        ep += ("&" if "?" in ep else "?") + query
    return CallRequest(method, ep, accept, None, p["include"], p["silent"])


def _graphql_body(fields: list[tuple[str, Value]]) -> dict:
    """{"query": ..., "operationName": ..., "variables": {...}}: every other field is a variable, as in gh."""
    body: dict = {}
    variables: dict[str, Value] = {}
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
