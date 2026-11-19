"""tollbooth_sweep: many GitHub repository searches, a few per GraphQL request. Stdlib only, Python 3.9+.

read_queries() reads a query file: one `slice<TAB>query` per line, where the slice names the part of
the topic a query covers. sweep() sends the queries in batches (aliases in one GraphQL document, one
charged request per batch) and writes one JSON row per repository it finds:
- a 502/504 splits the batch in half; a single query is tried once more with first: 10;
- a query whose answer is missing, or holds an error or a null node, counts as missing: after every
  row is written the sweep ends with exit 3 and names those slices;
- an error answer (401, a permission 403, not JSON) ends it with exit 1, a budget with exit 4 and a
  rate limit with exit 75. Rows are flushed after each batch, so what was fetched before is kept.

The query file is untrusted: it is read only if it is a small regular .tsv file, and an error names
the path and the line, never the file's text.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import stat
from dataclasses import dataclass
from typing import List, TextIO

import tollbooth_core as core
import tollbooth_graphql as graphql
from tollbooth_core import EX_HEAVY, EX_USAGE, TollboothError, eprint
from tollbooth_guard import BAD_CHARS_RE, NOT_UTF8_RE
from tollbooth_loop import Caller, StoppedOnAnswer

MAX_QUERIES_BYTES = 64 * 1024
MAX_QUERY_CHARS = 256     # GitHub's limit for one search query
NODE_BUDGET = 100         # search results per GraphQL request; 150 took 8 s of GitHub's 10 s limit
MAX_BATCH = 6             # queries per request, whatever --first is
RETRY_FIRST = 10          # results per query when a single query was too heavy
DEFAULT_SORT = " sort:stars-desc"


@dataclass(frozen=True)
class Query:
    slice: str    # the part of the topic it covers; every row it finds carries it
    text: str     # the GitHub search query


def read_regular_file(path: str, what: str, max_bytes: int) -> bytes:
    """A regular, non-symlink file of at most `max_bytes`. The open file is checked, so the path can't swap
    under the check; a FIFO can't hang the open. Errors name `what` and the path only (exit 2)."""
    if os.path.islink(path):
        raise TollboothError(f"{what} {path}: symlinks are not accepted", EX_USAGE)
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    except OSError as e:
        raise TollboothError(f"cannot open {what} {path}: {e.strerror}", EX_USAGE)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise TollboothError(f"{what} {path} is not a regular file", EX_USAGE)
        if st.st_size > max_bytes:
            raise TollboothError(f"{what} {path} is larger than {max_bytes // 1024} KB", EX_USAGE)
        with os.fdopen(fd, "rb") as f:  # takes the fd over
            fd = -1
            data = f.read(max_bytes + 1)
    except OSError as e:
        raise TollboothError(f"cannot read {what} {path}: {e.strerror}", EX_USAGE)
    finally:
        if fd >= 0:
            os.close(fd)
    if len(data) > max_bytes:  # it grew after fstat
        raise TollboothError(f"{what} {path} is larger than {max_bytes // 1024} KB", EX_USAGE)
    return data


def read_queries(path: str) -> List[Query]:
    """The queries in a .tsv file of `slice<TAB>query` lines; `#` comments and blank lines are skipped.

    ` sort:stars-desc` is added to a query that names no sort. Refusals (exit 2) name the line, never its text.
    """
    if not path.endswith(".tsv"):
        raise TollboothError(f"the query file must end in .tsv: {path}", EX_USAGE)
    data = read_regular_file(path, "query file", MAX_QUERIES_BYTES)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:  # no exception text: it quotes the file's bytes
        raise TollboothError(f"query file {path} is not valid UTF-8", EX_USAGE)
    queries = []
    for n, line in enumerate(text.split("\n"), 1):
        line = line.rstrip("\r")
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        slice_, tab, q = line.partition("\t")
        q = q.strip()
        if (not tab or not core.LABEL_RE.fullmatch(slice_) or not q or "\t" in q
                or BAD_CHARS_RE.search(q) or NOT_UTF8_RE.search(q)):
            raise TollboothError(f"{path}:{n}: expected slice<TAB>query (the slice: letters, digits, '.', '_', "
                                 "'-', 1 to 40 characters; no control or invisible characters)", EX_USAGE)
        if "sort:" not in q:
            q += DEFAULT_SORT
        if len(q) > MAX_QUERY_CHARS:
            raise TollboothError(f"{path}:{n}: the query is longer than GitHub's {MAX_QUERY_CHARS} characters "
                                 "(sort included)", EX_USAGE)
        queries.append(Query(slice_, q))
    if not queries:
        raise TollboothError(f"no queries in {path} (one slice<TAB>query per line)", EX_USAGE)
    return queries


def sweep(caller: Caller, queries: List[Query], first: int, out: TextIO) -> int:
    """Run every query, `first` results each, and write one JSON row per repository to `out`.

    Returns the number of rows written. Raises TollboothError exit 3 after all rows are written when some
    queries came back missing or partial, and the Caller's exit codes (1, 4, 75, 76) on the way.
    """
    if not 1 <= first <= NODE_BUDGET:
        raise TollboothError(f"--first must be 1 to {NODE_BUDGET}", EX_USAGE)
    caller.check("graphql")  # a spent budget or a long block fails before any work
    per = max(1, min(MAX_BATCH, NODE_BUDGET // first))
    batches = [queries[i:i + per] for i in range(0, len(queries), per)]
    run = _Run(caller, out, _iso(caller.throttle.clock.now()))
    for i, batch in enumerate(batches, 1):
        eprint(f"[{i}/{len(batches)}] slices: {', '.join(sorted({q.slice for q in batch}))}")
        run.batch(batch, first)
    if run.missing:
        slices = ", ".join(sorted(set(run.missing)))
        raise TollboothError(f"{len(run.missing)} of {len(queries)} queries returned no data or only part of it "
                             f"(slices: {slices}); the rows printed are incomplete", EX_HEAVY)
    eprint(f"PASS: {len(queries)} queries, {run.written} rows")
    return run.written


def _iso(wall: float) -> str:
    return dt.datetime.fromtimestamp(wall, dt.timezone.utc).isoformat(timespec="seconds")


class _Run:
    """One sweep's state: where rows go, when it started, how many rows it wrote, which slices came back short."""

    def __init__(self, caller: Caller, out: TextIO, fetched: str):
        self.caller, self.out, self.fetched = caller, out, fetched
        self.written = 0
        self.missing: List[str] = []

    def batch(self, batch: List[Query], first: int) -> None:
        """One request for `batch`; a 502/504 splits it, or retries a single query with fewer results."""
        try:
            data = graphql.post(self.caller, graphql.search_doc(len(batch), first),
                                {f"q{i}": q.text for i, q in enumerate(batch)})
        except StoppedOnAnswer as e:
            if e.code != EX_HEAVY:
                raise
            if len(batch) > 1:
                eprint(f"gh-tollbooth: a batch of {len(batch)} was too heavy; splitting it")
                mid = len(batch) // 2
                self.batch(batch[:mid], first)
                self.batch(batch[mid:], first)
                return
            if first > RETRY_FIRST:
                eprint(f"gh-tollbooth: a single query was too heavy; trying again with first: {RETRY_FIRST}")
                self.batch(batch, RETRY_FIRST)
                return
            raise
        self._write(batch, data)

    def _write(self, batch: List[Query], data: dict) -> None:
        for line in graphql.warnings(data):
            eprint(f"gh-tollbooth: {line}")
        errored = graphql.error_aliases(data, [f"q{i}" for i in range(len(batch))])
        answers = data.get("data")
        n = 0
        for i, q in enumerate(batch):
            res = answers.get(f"q{i}") if isinstance(answers, dict) else None
            nodes = res.get("nodes") if isinstance(res, dict) else None
            nodes = nodes if isinstance(nodes, list) else []
            if not isinstance(res, dict) or f"q{i}" in errored or any(not isinstance(nd, dict) for nd in nodes):
                self.missing.append(q.slice)  # GitHub answered part of it: an alias timed out, or a node is null
            total = res.get("repositoryCount") if isinstance(res, dict) else None
            for rank, node in enumerate(nodes, 1):
                if not isinstance(node, dict):
                    continue
                row = graphql.flatten(node)
                row.update(slice=q.slice, query=q.text, rank=rank,
                           total=total if isinstance(total, int) and not isinstance(total, bool) else None,
                           fetched=self.fetched)
                self.out.write(json.dumps(row) + "\n")
                n += 1
        self.out.flush()  # the rows so far survive a later failure or a kill
        self.written += n
        eprint(f"  {len(batch)} queries -> {n} repos ({graphql.cost_note(data)})")
