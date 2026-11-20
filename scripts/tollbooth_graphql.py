"""tollbooth_graphql: what gh-tollbooth knows about GitHub's GraphQL API.

- search() runs a batch of repository searches as one GraphQL query (one charged request through the
  Caller) and reads GitHub's answer: each query's repositories in order, the total GitHub counted, and
  whether the answer was complete; plus one warning per GraphQL error and the cost note;
- lookup() asks for a batch of repositories by name the same way, with their details (homepage, latest
  release, open issues, last commit) and, if asked, the first README it finds; each name comes back with its
  RepoDetail (None if GitHub sent none) and the error GitHub reported on it, if any;
- Repo is one repository from that answer, read defensively: a value of the wrong type reads as missing,
  never as a crash, and the description loses its hidden characters (tollbooth_text.strip_hidden);
- summary() describes a request for the call log, a GraphQL document's text included;
- HEAVY_QUERY_ADVICE is what to do when GitHub gives up on a query (502/504: its 10 s limit).
GitHub's answer is parsed by pydantic, whose JSON parser has the same limits on every Python build
(nesting 200 deep, numbers up to 4,300 digits). A ValidationError's text quotes the input, so it is
never shown.
Not here: sending a request (the Caller, tollbooth_loop) and checking that a document is read-only
(tollbooth_guard). Never import this module as `graphql`: that is graphql-core's package.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from http.client import responses as REASONS
from typing import TYPE_CHECKING, Annotated, Any

from pydantic import AfterValidator, AliasPath, BaseModel, BeforeValidator, Field, ValidationError

import tollbooth_http as http
from tollbooth_core import EX_FAIL, TollboothError
from tollbooth_text import printable, strip_hidden

if TYPE_CHECKING:  # tollbooth_loop imports this module, so the Caller is named for type checkers only
    from tollbooth_loop import Caller

HEAVY_QUERY_ADVICE = ("GitHub gave up on the query (502/504: its 10 s limit). Make it lighter: "
                      "fewer aliases, a smaller first:, fewer nested connections.")
REPO_FIELDS = ("nameWithOwner url description stargazerCount forkCount pushedAt createdAt "
               "isArchived isFork primaryLanguage { name } licenseInfo { spdxId }")
TOPICS = "repositoryTopics(first: 6) { nodes { topic { name } } }"
RATE_LIMIT = "rateLimit { cost remaining resetAt }"
DETAIL_FIELDS = ("homepageUrl latestRelease { tagName publishedAt } issues(states: OPEN) { totalCount } "
                 "defaultBranchRef { target { ... on Commit { committedDate } } }")
README_PATHS = ("README.md", "readme.md", "Readme.md", "README.rst", "README", "README.markdown", "README.txt")
WARNING_CHARS = 200        # a warning line is cut to this many characters


# ---------- reading GitHub's answer: a value of the wrong type reads as missing ----------

def _text(value: object) -> object:
    return value if isinstance(value, str) else None


def _count(value: object) -> object:
    return value if type(value) is int else None  # not a bool, which Python counts as an int


def _flag(value: object) -> object:
    return value if type(value) is bool else None


def _items(value: object) -> object:
    return value if isinstance(value, list) else []


def _objects(value: object) -> object:
    return [v for v in value if isinstance(v, dict)] if isinstance(value, list) else []


def _topic_names(value: object) -> object:
    """repositoryTopics.nodes -> the names that are text: [{"topic": {"name": "cli"}}, ...] -> ["cli", ...]."""
    names = (v.get("topic", {}).get("name") if isinstance(v, dict) and isinstance(v.get("topic"), dict) else None
             for v in (value if isinstance(value, list) else []))
    return [n for n in names if isinstance(n, str)]


Text = Annotated["str | None", BeforeValidator(_text)]
Count = Annotated["int | None", BeforeValidator(_count)]
Flag = Annotated["bool | None", BeforeValidator(_flag)]


class Repo(BaseModel):
    """One repository from a search answer. Its fields are the rows' fields: GitHub's own names, with
    language, license and topics taken out of their nested objects."""

    nameWithOwner: Text = None
    url: Text = None
    description: Annotated["str | None", BeforeValidator(_text),
                           AfterValidator(lambda v: None if v is None else strip_hidden(v))] = None
    stargazerCount: Count = None
    forkCount: Count = None
    pushedAt: Text = None
    createdAt: Text = None
    isArchived: Flag = None
    isFork: Flag = None
    language: Text = Field(None, validation_alias=AliasPath("primaryLanguage", "name"))
    license: Text = Field(None, validation_alias=AliasPath("licenseInfo", "spdxId"))
    topics: Annotated["list[str]", BeforeValidator(_topic_names)] = Field(
        default_factory=list, validation_alias=AliasPath("repositoryTopics", "nodes"))


class RepoDetail(Repo):
    """A repository looked up by name: a Repo plus its details. The README is the first of README_PATHS
    that holds visible text, without its hidden characters; None if none does or it wasn't asked for."""

    homepageUrl: Text = None
    release: Text = Field(None, validation_alias=AliasPath("latestRelease", "tagName"))
    releasedAt: Text = Field(None, validation_alias=AliasPath("latestRelease", "publishedAt"))
    openIssues: Count = Field(None, validation_alias=AliasPath("issues", "totalCount"))
    lastCommit: Text = Field(None, validation_alias=AliasPath("defaultBranchRef", "target", "committedDate"))
    readme: str | None = None


class _Search(BaseModel):
    """One alias's answer: the total GitHub counted and the result nodes, as they came."""

    repositoryCount: Count = None
    nodes: Annotated["list[Any]", BeforeValidator(_items)] = Field(default_factory=list)


class _Error(BaseModel):
    type: Text = None
    message: Text = None
    path: Annotated["list[Any]", BeforeValidator(_items)] = Field(default_factory=list)


class _RateLimit(BaseModel):
    cost: Count = None
    remaining: Count = None


class _Answer(BaseModel):
    """GitHub's whole answer. It must be a JSON object; anything inside it may be missing or odd."""

    data: Annotated["dict[str, Any]", BeforeValidator(lambda v: v if isinstance(v, dict) else {})] = Field(
        default_factory=dict)
    errors: Annotated["list[_Error]", BeforeValidator(_objects)] = Field(default_factory=list)


# ---------- the search ----------

@dataclass(frozen=True)
class Found:
    """One query's answer: each repository with its place in GitHub's results (1 = first), the total GitHub
    counted, and whether the answer was complete. It isn't when GitHub left the query out of its answer,
    reported an error on it, or put a null among its results (part of it timed out)."""

    ranked: list[tuple[int, Repo]]
    total: int | None
    complete: bool


@dataclass(frozen=True)
class SearchAnswer:
    found: list[Found]     # one per query, in the order given
    warnings: list[str]    # one line per GraphQL error in the answer, hidden characters as "?"
    cost: str              # "cost 1, graphql left 4999"


def search(caller: Caller, queries: Sequence[str], first: int) -> SearchAnswer:
    """Run `queries` as one GraphQL query, `first` results each: one charged request.

    Each query is an alias (q0, q1, ...) with its own variable, so no query text is in the document.
    Raises TollboothError: exit 1 for an answer that isn't 2xx or isn't a JSON object, and the Caller's
    own (a 502/504 is a StoppedOnAnswer with exit 3; a spent budget is exit 4; a rate limit exit 75).
    """
    if not queries:
        raise ValueError("search() needs at least one query")
    variables = {f"q{i}": q for i, q in enumerate(queries)}
    resp = caller.fetch("POST", "graphql", json_body={"query": _search_doc(len(queries), first), "variables": variables})
    answer = _read(resp)
    errored = {e.path[0] for e in answer.errors if e.path and isinstance(e.path[0], str)}
    found = [_found(answer.data.get(alias), alias in errored) for alias in variables]
    return SearchAnswer(found, [_warning(e) for e in answer.errors], _cost(answer.data.get("rateLimit")))


@dataclass(frozen=True)
class Looked:
    """One name's answer: its RepoDetail, or None when GitHub sent none, and the type of the first GraphQL
    error that names it (None if none does; "" for an error that gives no type)."""

    repo: RepoDetail | None
    error: str | None


@dataclass(frozen=True)
class LookupAnswer:
    found: list[Looked]    # one per name, in the order given
    warnings: list[str]    # one line per GraphQL error in the answer, hidden characters as "?"
    cost: str              # "cost 1, graphql left 4999"


def lookup(caller: Caller, names: Sequence[str], readme: bool) -> LookupAnswer:
    """Ask for repositories by owner/name, with their details and, with `readme`, their README: one charged request.

    Each name travels as two variables, owner and name, never in the document. Raises as search() does.
    """
    if not names:
        raise ValueError("lookup() needs at least one name")
    variables: dict[str, str] = {}
    for i, name in enumerate(names):
        variables[f"o{i}"], variables[f"n{i}"] = name.split("/", 1)
    resp = caller.fetch("POST", "graphql", json_body={"query": _lookup_doc(len(names), readme), "variables": variables})
    answer = _read(resp)
    kinds: dict[str, str] = {}
    for e in answer.errors:
        if e.path and isinstance(e.path[0], str):
            kinds.setdefault(e.path[0], e.type or "")
    found = [Looked(_detail(answer.data.get(f"r{i}"), readme), kinds.get(f"r{i}")) for i in range(len(names))]
    return LookupAnswer(found, [_warning(e) for e in answer.errors], _cost(answer.data.get("rateLimit")))


def _lookup_doc(n: int, readme: bool) -> str:
    """One query for `n` repositories (aliases r0..r{n-1}, variables $o0/$n0..), with their README if asked."""
    fields = f"{REPO_FIELDS} {TOPICS} {DETAIL_FIELDS}"
    if readme:
        fields += " " + " ".join(f'rm{j}: object(expression: "HEAD:{path}") {{ ... on Blob {{ text }} }}'
                                 for j, path in enumerate(README_PATHS))
    decl = ", ".join(f"$o{i}: String!, $n{i}: String!" for i in range(n))
    body = " ".join(f"r{i}: repository(owner: $o{i}, name: $n{i}) {{ {fields} }}" for i in range(n))
    return f"query({decl}) {{ {body} {RATE_LIMIT} }}"


def _detail(raw: object, readme: bool) -> RepoDetail | None:
    if not isinstance(raw, dict):
        return None
    detail = RepoDetail.model_validate(raw)
    if readme:
        texts = (_text(f.get("text")) if isinstance(f := raw.get(f"rm{j}"), dict) else None
                 for j in range(len(README_PATHS)))
        cleaned = (strip_hidden(t) for t in texts if isinstance(t, str))
        detail.readme = next((t for t in cleaned if t.strip()), None)
    return detail


def _search_doc(n: int, first: int) -> str:
    """One query with `n` repository searches: aliases q0..q{n-1}, variables $q0.., `first` results each."""
    fields = f"{REPO_FIELDS} {TOPICS}"
    decl = ", ".join(f"$q{i}: String!" for i in range(n))
    body = " ".join(f"q{i}: search(query: $q{i}, type: REPOSITORY, first: {int(first)}) "
                    f"{{ repositoryCount nodes {{ ... on Repository {{ {fields} }} }} }}" for i in range(n))
    return f"query({decl}) {{ {body} {RATE_LIMIT} }}"


def _read(resp: http.Response) -> _Answer:
    if not 200 <= resp.status < 300:
        hint = http.TOKEN_REJECTED_HINT if resp.status == 401 else ""
        raise TollboothError(f"GraphQL answered HTTP {resp.status} {REASONS.get(resp.status, '')}".rstrip() + hint,
                             EX_FAIL)
    try:
        return _Answer.model_validate_json(resp.body)
    except ValidationError:  # never shown: its text quotes the body
        raise TollboothError(f"GraphQL answered HTTP {resp.status} with a body that isn't a JSON object",
                             EX_FAIL) from None


def _found(raw: object, errored: bool) -> Found:
    if not isinstance(raw, dict):
        return Found([], None, False)
    result = _Search.model_validate(raw)
    ranked = [(place, Repo.model_validate(n)) for place, n in enumerate(result.nodes, 1) if isinstance(n, dict)]
    return Found(ranked, result.repositoryCount, not errored and len(ranked) == len(result.nodes))


def _warning(e: _Error) -> str:
    return printable(f"warning: {e.type or ''} {e.message or ''}".strip())[:WARNING_CHARS]


def _cost(raw: object) -> str:
    limit = _RateLimit.model_validate(raw) if isinstance(raw, dict) else _RateLimit()
    return f"cost {_shown(limit.cost)}, graphql left {_shown(limit.remaining)}"


def _shown(count: int | None) -> str:
    return "?" if count is None else str(count)


# ---------- the call log ----------

def summary(method: str, path: str, json_body: Any) -> str:
    """What was asked, for the call log: the method, the path and a GraphQL document's text."""
    query = json_body.get("query") if isinstance(json_body, dict) else None
    return f"{method} {path} {query}" if isinstance(query, str) else f"{method} {path}"
