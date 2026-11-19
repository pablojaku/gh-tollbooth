"""tollbooth_graphql: what gh-tollbooth knows about GitHub's GraphQL API.

- summary() describes a request for the call log, a GraphQL document's text included;
- HEAVY_QUERY_ADVICE is what to do when GitHub gives up on a query (502/504: its 10 s limit).
Not here: sending a request (the Caller, tollbooth_loop) and checking that a document is read-only
(tollbooth_guard). Never import this module as `graphql`: that is graphql-core's package.
"""
from __future__ import annotations

from typing import Any

HEAVY_QUERY_ADVICE = ("GitHub gave up on the query (502/504: its 10 s limit). Make it lighter: "
                      "fewer aliases, a smaller first:, fewer nested connections.")


def summary(method: str, path: str, json_body: Any) -> str:
    """What was asked, for the call log: the method, the path and a GraphQL document's text."""
    query = json_body.get("query") if isinstance(json_body, dict) else None
    return f"{method} {path} {query}" if isinstance(query, str) else f"{method} {path}"
