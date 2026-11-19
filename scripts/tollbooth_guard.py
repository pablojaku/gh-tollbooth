"""tollbooth_guard: what `gh-tollbooth call` may ask GitHub. Fail closed: anything not allowed is refused.

GraphQL documents are parsed with graphql-core, and only `query` operations and fragments pass. A
document that doesn't parse is refused too: gh-tollbooth refuses what it can't verify. Strings and
comments are the parser's business, so a keyword can't hide in either.
"""
from __future__ import annotations

from typing import NoReturn

from graphql import FragmentDefinitionNode, GraphQLError, OperationDefinitionNode, parse

from tollbooth_core import EX_USAGE, TollboothError


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
