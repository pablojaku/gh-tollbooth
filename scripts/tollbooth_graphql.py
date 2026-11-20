"""tollbooth_graphql: what gh-tollbooth knows about GitHub's GraphQL API. Stdlib only, Python 3.9+.

- search_doc() builds one batched repository search: aliases q0..qN, one query variable each;
- repos_doc() builds one batched repository lookup: aliases r0..rN, owner and name as variables,
  plus the README at HEAD when asked;
- post() sends a document through a Caller and returns GitHub's parsed answer;
- flatten() turns a repository node into one flat JSON row; fetched_at() is the time rows carry;
- error_aliases() and warnings() read the `errors` of an answer; cost_note() its rateLimit;
- summary() describes a request for the call log; HEAVY_QUERY_ADVICE is what to do after a 502/504.

GitHub's answers are read defensively: a value of the wrong type reads as missing, never as a crash,
and text that goes to a terminal has its control characters replaced. A repository's description and
README are its owner's text, which agents read: flatten() removes the characters that hide text from a
person but not from a model (control, zero-width, bidi, tag characters). Visible text stays as it is.
"""
from __future__ import annotations

import datetime as dt
import json
import re
from http.client import responses as REASONS
from typing import TYPE_CHECKING, Dict, List, Optional

from tollbooth_core import EX_FAIL, TollboothError
from tollbooth_guard import BAD_CHARS_RE

if TYPE_CHECKING:  # tollbooth_loop imports this module, so the Caller is only named for type checkers
    from tollbooth_loop import Caller

REPO_FIELDS = ("nameWithOwner url description stargazerCount forkCount pushedAt createdAt "
               "isArchived isFork primaryLanguage { name } licenseInfo { spdxId }")
TOPICS = "repositoryTopics(first: 6) { nodes { topic { name } } }"
RATE_LIMIT = "rateLimit { cost remaining resetAt }"
DETAIL_FIELDS = ("homepageUrl latestRelease { tagName publishedAt } issues(states: OPEN) { totalCount } "
                 "defaultBranchRef { target { ... on Commit { committedDate } } }")
README_PATHS = ("README.md", "readme.md", "Readme.md", "README.rst", "README", "README.markdown", "README.txt")
README_CHARS = 3000       # characters of README text in a row, by default
HEAVY_QUERY_ADVICE = ("GitHub gave up on the query (502/504: its 10 s limit). Make it lighter: "
                      "fewer aliases, a smaller first:, fewer nested connections.")
WARNING_CHARS = 200
CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")
# Beyond the guard's list (control characters except tab and newline, CR, zero-width, bidi, separators, BOM): C1
# controls, word joiners and Unicode tag characters ("ASCII smuggling"). Built from code points, so the source file
# itself holds no invisible characters.
_MORE_HIDDEN = [*range(0x80, 0xA0), *range(0x2060, 0x2065), *range(0xE0000, 0xE0080)]
MORE_HIDDEN_RE = re.compile("[" + re.escape("".join(map(chr, _MORE_HIDDEN))) + "]")


def search_doc(n: int, first: int) -> str:
    """One GraphQL query with `n` repository searches (aliases q0..q{n-1}, variables $q0..), `first` results each."""
    fields = f"{REPO_FIELDS} {TOPICS}"
    decl = ", ".join(f"$q{i}: String!" for i in range(n))
    body = " ".join(f"q{i}: search(query: $q{i}, type: REPOSITORY, first: {int(first)}) "
                    f"{{ repositoryCount nodes {{ ... on Repository {{ {fields} }} }} }}" for i in range(n))
    return f"query({decl}) {{ {body} {RATE_LIMIT} }}"


def repos_doc(n: int, readme: bool) -> str:
    """One GraphQL query for `n` repositories (aliases r0..r{n-1}, variables $o0/$n0..), with their README if asked."""
    fields = f"{REPO_FIELDS} {TOPICS} {DETAIL_FIELDS}"
    if readme:
        fields += " " + " ".join(f'rm{j}: object(expression: "HEAD:{path}") {{ ... on Blob {{ text }} }}'
                                 for j, path in enumerate(README_PATHS))
    decl = ", ".join(f"$o{i}: String!, $n{i}: String!" for i in range(n))
    body = " ".join(f"r{i}: repository(owner: $o{i}, name: $n{i}) {{ {fields} }}" for i in range(n))
    return f"query({decl}) {{ {body} {RATE_LIMIT} }}"


def post(caller: Caller, doc: str, variables: Dict[str, object]) -> dict:
    """Send one GraphQL document through the Caller (one charged request) and return the parsed answer.

    Raises TollboothError exit 1 for an answer that isn't 2xx (401, a permission 403, ...) or isn't a
    JSON object; rate limits, budget and 502/504 come from Caller.fetch with their own exit codes.
    """
    body: Dict[str, object] = {"query": doc}
    if variables:
        body["variables"] = variables
    resp = caller.fetch("POST", "graphql", json_body=body)
    if not 200 <= resp.status < 300:
        hint = " (GitHub rejected the token: run `gh auth login`, or check GH_TOKEN)" if resp.status == 401 else ""
        reason = REASONS.get(resp.status, "")
        raise TollboothError(f"GraphQL answered HTTP {resp.status} {reason}".rstrip() + hint, EX_FAIL)
    try:
        data = json.loads(resp.body)
    except (ValueError, RecursionError):
        data = None
    if not isinstance(data, dict):
        raise TollboothError(f"GraphQL answered HTTP {resp.status} with a body that isn't a JSON object", EX_FAIL)
    return data


def _at(value: object, *keys: str) -> object:
    """value[k1][k2]..., or None as soon as a step isn't a dict holding that key."""
    for k in keys:
        if not isinstance(value, dict):
            return None
        value = value.get(k)
    return value


def flatten(node: dict, readme_chars: int = README_CHARS) -> dict:
    """A repository node as one flat row: its scalar fields, plus language, license and topics.

    A node from repos_doc() also gives release, releasedAt, openIssues and lastCommit, and, if the README
    was asked for, readme: the first README found, cleaned, then cut to `readme_chars` (None if none).
    """
    row = {k: v for k, v in node.items() if not isinstance(v, (dict, list)) and v is not None}
    if isinstance(row.get("description"), str):
        row["description"] = _clean(row["description"])
    row["language"] = _text_or_none(_at(node, "primaryLanguage", "name"))
    row["license"] = _text_or_none(_at(node, "licenseInfo", "spdxId"))
    topics = _at(node, "repositoryTopics", "nodes")
    names = (_at(t, "topic", "name") for t in (topics if isinstance(topics, list) else []))
    row["topics"] = [n for n in names if isinstance(n, str)]
    if "latestRelease" in node:
        row["release"] = _text_or_none(_at(node, "latestRelease", "tagName"))
        row["releasedAt"] = _text_or_none(_at(node, "latestRelease", "publishedAt"))
    if "issues" in node:
        row["openIssues"] = _int_or_none(_at(node, "issues", "totalCount"))
    if "defaultBranchRef" in node:
        row["lastCommit"] = _text_or_none(_at(node, "defaultBranchRef", "target", "committedDate"))
    if any(f"rm{j}" in node for j in range(len(README_PATHS))):
        texts = (_text_or_none(_at(node, f"rm{j}", "text")) for j in range(len(README_PATHS)))
        cleaned = (_clean(t) for t in texts if t)
        row["readme"] = next((t[:readme_chars] for t in cleaned if t), None)
    return row


def _clean(text: str) -> str:
    """The owner's text without the characters that hide text from a person but not from a model."""
    return MORE_HIDDEN_RE.sub("", BAD_CHARS_RE.sub("", text))


def fetched_at(wall: float) -> str:
    """The time rows carry as `fetched`: ISO 8601 in UTC, to the second."""
    return dt.datetime.fromtimestamp(wall, dt.timezone.utc).isoformat(timespec="seconds")


def _text_or_none(value: object) -> Optional[str]:
    return value if isinstance(value, str) else None


def _int_or_none(value: object) -> Optional[int]:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def error_aliases(data: dict, aliases: List[str]) -> Dict[str, str]:
    """alias -> GitHub's error type (the first one) for each error whose path starts at one of `aliases`."""
    found: Dict[str, str] = {}
    errors = data.get("errors")
    for e in errors if isinstance(errors, list) else []:
        path = e.get("path") if isinstance(e, dict) else None
        if isinstance(path, list) and path and isinstance(path[0], str) and path[0] in aliases:
            kind = e.get("type")
            found.setdefault(path[0], kind if isinstance(kind, str) else "")
    return found


def warnings(data: dict) -> List[str]:
    """One line per GraphQL error in an answer: its type and message, control characters replaced, cut short."""
    lines = []
    errors = data.get("errors")
    for e in errors if isinstance(errors, list) else []:
        kind, message = _at(e, "type"), _at(e, "message")
        text = f"warning: {kind if isinstance(kind, str) else ''} {message if isinstance(message, str) else ''}"
        lines.append(CONTROL_RE.sub("?", text.strip())[:WARNING_CHARS])
    return lines


def cost_note(data: dict) -> str:
    """'cost 1, graphql left 4999' from an answer's rateLimit; anything that isn't a number shows as '?'."""
    cost, left = _at(data, "data", "rateLimit", "cost"), _at(data, "data", "rateLimit", "remaining")
    return f"cost {_number(cost)}, graphql left {_number(left)}"


def _number(value: object) -> str:
    return str(value) if isinstance(value, int) and not isinstance(value, bool) else "?"


def summary(method: str, path: str, json_body: Optional[dict]) -> str:
    """What was asked, for the call log: the method, the path and a GraphQL document's text."""
    query = json_body.get("query") if isinstance(json_body, dict) else None
    return f"{method} {path} {query}" if isinstance(query, str) else f"{method} {path}"
