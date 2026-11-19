"""tollbooth_sweep: many GitHub repository searches, a few per GraphQL request.

read_queries() reads a query file: one `slice<TAB>query` per line, where the slice names the part of the
topic a query covers and goes into every row the query finds. The query file is untrusted: it is read
only if it is a small regular .tsv file in UTF-8 with no hidden characters, and a refusal names the path
and the line, never the file's text.
sweep() sends the queries in batches (one tollbooth_graphql.search() each: one charged request) and
writes one JSON row per repository it finds (a SweepRow):
- a 502/504 splits the batch in half; a single query is tried once more with first: 10;
- a query whose answer is incomplete counts as missing: after every row is written the sweep ends with
  exit 3 and names those slices;
- an error answer (401, a permission 403, not JSON) ends it with exit 1, a spent budget with exit 4 and a
  rate limit with exit 75. Rows are flushed after each batch, so what was fetched before is kept.
Progress and the cost of each request are notices on stderr; they name slices, never query text.
Not here: the GraphQL request and reading its answer (tollbooth_graphql).
"""
from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, TextIO

import tollbooth_graphql as gql
from tollbooth_core import EX_HEAVY, EX_USAGE, TollboothError, log, read_regular_file
from tollbooth_text import LABEL_RE, has_hidden
from tollbooth_throttle import StoppedOnAnswer

if TYPE_CHECKING:
    from tollbooth_loop import Caller

MAX_QUERIES_BYTES = 64 * 1024
MAX_QUERY_CHARS = 256     # GitHub's limit for one search query
DEFAULT_SORT = " sort:stars-desc"
NODE_BUDGET = 100         # search results per GraphQL request; 150 took 8 s of GitHub's 10 s limit
MAX_BATCH = 6             # queries per request, whatever first is
RETRY_FIRST = 10          # results per query when a single query was too heavy
ALWAYS_SHOWN = frozenset({"language", "license", "total"})  # in every row, null when unknown


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


class SweepRow(gql.Repo):
    """One row a sweep writes: a repository, and the query that found it."""

    slice: str = ""             # the query's slice
    query: str = ""             # the query, its sort included
    rank: int = 0               # the repository's place in the query's results, 1 = first
    total: int | None = None    # the results GitHub counted for the query
    fetched: str = ""           # when the sweep started: UTC, ISO 8601

    def json_line(self) -> str:
        """The row as one JSON line. A repository field GitHub left empty is left out; language, license
        and total are always there, null when unknown."""
        return json.dumps({k: v for k, v in self.model_dump().items() if v is not None or k in ALWAYS_SHOWN})


def sweep(caller: Caller, queries: list[Query], first: int, out: TextIO) -> int:
    """Run every query, `first` results each (1 to 100), and write one JSON row per repository to `out`.

    Returns the number of rows written. Raises TollboothError exit 3 after every row is written when some
    queries came back missing or partial, and the Caller's exit codes (1, 3, 4, 75, 76) on the way.
    """
    if not 1 <= first <= NODE_BUDGET:
        raise ValueError(f"first must be 1 to {NODE_BUDGET}, not {first}")
    caller.check("graphql")  # a spent budget or a long block fails before any work
    per = max(1, min(MAX_BATCH, NODE_BUDGET // first))
    batches = [queries[i:i + per] for i in range(0, len(queries), per)]
    run = _Run(caller, out, _iso(caller.throttle.clock.now()))
    for i, batch in enumerate(batches, 1):
        log.info("[%d/%d] slices: %s", i, len(batches), ", ".join(sorted({q.slice for q in batch})))
        run.batch(batch, first)
    if run.missing:
        slices = ", ".join(sorted(set(run.missing)))
        raise TollboothError(f"{len(run.missing)} of {len(queries)} queries returned no data or only part of it "
                             f"(slices: {slices}); the rows printed are incomplete", EX_HEAVY)
    log.info("PASS: %d queries, %d rows", len(queries), run.written)
    return run.written


def _iso(wall: float) -> str:
    return dt.datetime.fromtimestamp(wall, dt.timezone.utc).isoformat(timespec="seconds")


class _Run:
    """One sweep's state: where rows go, when it started, how many rows it wrote, which slices came back short."""

    def __init__(self, caller: Caller, out: TextIO, fetched: str):
        self.caller, self.out, self.fetched = caller, out, fetched
        self.written = 0
        self.missing: list[str] = []

    def batch(self, batch: list[Query], first: int) -> None:
        """One request for `batch`; a 502/504 splits it, or tries a single query again with fewer results."""
        try:
            answer = gql.search(self.caller, [q.text for q in batch], first)
        except StoppedOnAnswer as e:
            if e.code != EX_HEAVY:
                raise
            if len(batch) > 1:
                log.info("a batch of %d was too heavy; splitting it", len(batch))
                mid = len(batch) // 2
                self.batch(batch[:mid], first)
                self.batch(batch[mid:], first)
                return
            if first > RETRY_FIRST:
                log.info("a single query was too heavy; trying again with first: %d", RETRY_FIRST)
                self.batch(batch, RETRY_FIRST)
                return
            raise
        self._write(batch, answer)

    def _write(self, batch: list[Query], answer: gql.SearchAnswer) -> None:
        for line in answer.warnings:
            log.warning("%s", line)
        n = 0
        for q, found in zip(batch, answer.found):
            if not found.complete:
                self.missing.append(q.slice)
            for place, repo in found.ranked:
                row = SweepRow.model_construct(**repo.model_dump(), slice=q.slice, query=q.text, rank=place,
                                               total=found.total, fetched=self.fetched)  # checked already
                self.out.write(row.json_line() + "\n")
                n += 1
        self.out.flush()  # the rows so far survive a later failure or a kill
        self.written += n
        log.info("%d queries -> %d repos (%s)", len(batch), n, answer.cost)
