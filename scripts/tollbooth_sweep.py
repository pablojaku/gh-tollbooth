"""tollbooth_sweep: many GitHub repository searches, a few per GraphQL request.

read_queries() reads a query file: one `slice<TAB>query` per line, where the slice names the part of the
topic a query covers and goes into every row the query finds. The query file is untrusted: it is read
only if it is a small regular .tsv file in UTF-8 with no hidden characters, and a refusal names the path
and the line, never the file's text.
Not here: the GraphQL request and reading its answer (tollbooth_graphql).
"""
from __future__ import annotations

from dataclasses import dataclass

from tollbooth_core import EX_USAGE, TollboothError, read_regular_file
from tollbooth_text import LABEL_RE, has_hidden

MAX_QUERIES_BYTES = 64 * 1024
MAX_QUERY_CHARS = 256     # GitHub's limit for one search query
DEFAULT_SORT = " sort:stars-desc"


@dataclass(frozen=True)
class Query:
    slice: str    # the part of the topic it covers; every row it finds carries it
    text: str     # the GitHub search query


def read_queries(path: str) -> list[Query]:
    """The queries in a .tsv file of `slice<TAB>query` lines; `#` comments and blank lines are skipped.

    ` sort:stars-desc` is added to a query that names no sort. Refusals (exit 2) name the line, never its text.
    """
    if not path.endswith(".tsv"):
        raise TollboothError(f"the query file must end in .tsv: {path}", EX_USAGE)
    data = read_regular_file(path, "query file", MAX_QUERIES_BYTES)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:  # its message quotes the file's bytes
        raise TollboothError(f"query file {path} is not valid UTF-8", EX_USAGE) from None
    queries = []
    for n, line in enumerate(text.split("\n"), 1):
        line = line.rstrip("\r")
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        slice_, tab, q = line.partition("\t")
        q = q.strip()
        if not tab or not LABEL_RE.fullmatch(slice_) or not q or "\t" in q or has_hidden(q):
            raise TollboothError(f"{path}:{n}: expected slice<TAB>query (the slice: letters, digits, '.', '_', "
                                 "'-', 1 to 40 characters; no hidden characters)", EX_USAGE)
        if "sort:" not in q:
            q += DEFAULT_SORT
        if len(q) > MAX_QUERY_CHARS:
            raise TollboothError(f"{path}:{n}: the query is longer than GitHub's {MAX_QUERY_CHARS} characters "
                                 "(sort included)", EX_USAGE)
        queries.append(Query(slice_, q))
    if not queries:
        raise TollboothError(f"no queries in {path} (one slice<TAB>query per line)", EX_USAGE)
    return queries
