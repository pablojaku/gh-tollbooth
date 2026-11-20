"""tollbooth_repos: details, and the README if asked, for a list of GitHub repositories. Stdlib only, Python 3.9+.

shortlist() decides which repositories: the names given on the command line first, then the rows of
--from files in rank's order (newest row per repository, most stars first, archives and forks left out),
cut to --top. repos() asks for them in batches (aliases in one GraphQL document, one charged request per
batch) and writes one JSON row per repository:
- a repository GitHub reports as NOT_FOUND gets an `error: "not found"` row and doesn't fail the run;
- any other missing repository gets an error row, and one with part of its data missing an `incomplete`
  field; after every row is written the run ends with exit 3;
- a 502/504 splits the batch in half; a single repository is tried once more without its README, and one
  still too heavy gets an `error: "too heavy"` row while the run goes on;
- an error answer (401, a permission 403, not JSON) ends it with exit 1, a budget with exit 4 and a rate
  limit with exit 75. Rows are flushed after each batch, so what was fetched before is kept.

It doesn't search: names come from the caller or from rows `sweep` wrote. Names travel only as GraphQL
variables, never in the document.
"""
from __future__ import annotations

import json
from typing import Dict, List, Sequence, TextIO

import tollbooth_core as core
import tollbooth_graphql as graphql
from tollbooth_core import EX_HEAVY, EX_USAGE, TollboothError, eprint
from tollbooth_loop import Caller, StoppedOnAnswer
from tollbooth_rank import RankOptions, rank, read_rows

BATCH = 20                # repositories per request
README_BATCH = 5          # repositories per request when their READMEs are asked for
MAX_README_CHARS = 20000  # characters of README text per repository, at most
NAME_CHARS = 60           # characters of a refused name an error message shows
PROGRESS_CHARS = 70       # characters of names a progress line shows


def shortlist(names: Sequence[str], from_paths: Sequence[str], top: int) -> List[str]:
    """The repositories to fetch: `names` first, then the --from rows in rank's order, cut to `top` (0 = all).

    A name is kept once, whatever its case. Refusals exit 2 and cost nothing.
    """
    for name in names:
        if not core.REPO_RE.fullmatch(name):
            raise TollboothError(f"not an owner/name: {name[:NAME_CHARS]!r}", EX_USAGE)
    if top and not from_paths:
        raise TollboothError("--top limits the repos taken from --from: give --from RAW.jsonl, or drop --top",
                             EX_USAGE)
    picked = list(names)
    if from_paths:
        picked += [e.row["nameWithOwner"] for e in rank(read_rows(from_paths), RankOptions(top=top))]
    seen, unique = set(), []
    for name in picked:
        if name.lower() not in seen:
            seen.add(name.lower())
            unique.append(name)
    if not unique:
        raise TollboothError("no repos to fetch: give owner/name arguments, or --from a file of sweep rows "
                             "(rank leaves archived repos and forks out)", EX_USAGE)
    return unique


def repos(caller: Caller, names: List[str], readme: bool, readme_chars: int, out: TextIO) -> int:
    """Fetch `names` and write one JSON row per repository to `out`; with `readme`, its README cut to `readme_chars`.

    Returns the number of rows written. Raises TollboothError exit 3 after all rows are written when some
    repositories came back missing (not NOT_FOUND) or partial, and the Caller's exit codes (1, 4, 75, 76) on the way.
    """
    if not 1 <= readme_chars <= MAX_README_CHARS:
        raise TollboothError(f"--readme-chars must be 1 to {MAX_README_CHARS}", EX_USAGE)
    caller.check("graphql")  # a spent budget or a long block fails before any work
    per = README_BATCH if readme else BATCH
    batches = [names[i:i + per] for i in range(0, len(names), per)]
    run = _Run(caller, out, graphql.fetched_at(caller.throttle.clock.now()), readme_chars)
    for i, batch in enumerate(batches, 1):
        eprint(f"[{i}/{len(batches)}] {', '.join(batch)[:PROGRESS_CHARS]}")
        run.batch(batch, readme)
    if run.failed:
        raise TollboothError(f"{run.failed} of {len(names)} repos came back without data or only part of it "
                             "(not a NOT_FOUND); the rows printed are incomplete", EX_HEAVY)
    eprint(f"PASS: {run.written} repos")
    return run.written


class _Run:
    """One run's state: where rows go, when it started, how many rows it wrote, how many came back short."""

    def __init__(self, caller: Caller, out: TextIO, fetched: str, readme_chars: int):
        self.caller, self.out, self.fetched, self.readme_chars = caller, out, fetched, readme_chars
        self.written = 0
        self.failed = 0

    def batch(self, names: List[str], readme: bool, without_readme: bool = False) -> None:
        """One request for `names`; a 502/504 splits it, retries one repository without its README, or marks it."""
        variables: Dict[str, object] = {}
        for i, name in enumerate(names):
            variables[f"o{i}"], variables[f"n{i}"] = name.split("/", 1)
        try:
            data = graphql.post(self.caller, graphql.repos_doc(len(names), readme), variables)
        except StoppedOnAnswer as e:
            if e.code != EX_HEAVY:
                raise
            if len(names) > 1:
                eprint(f"gh-tollbooth: a batch of {len(names)} was too heavy; splitting it")
                mid = len(names) // 2
                self.batch(names[:mid], readme, without_readme)
                self.batch(names[mid:], readme, without_readme)
            elif readme:
                eprint(f"gh-tollbooth: {names[0]} was too heavy; trying again without its README")
                self.batch(names, False, without_readme=True)
            else:
                eprint(f"gh-tollbooth: {names[0]} is still too heavy; writing an error row and going on")
                self._emit([{"nameWithOwner": names[0], "error": "too heavy"}], failed=1)
            return
        self._write(names, data, without_readme)

    def _write(self, names: List[str], data: dict, without_readme: bool) -> None:
        for line in graphql.warnings(data):
            eprint(f"gh-tollbooth: {line}")
        errored = graphql.error_aliases(data, [f"r{i}" for i in range(len(names))])
        answers = data.get("data")
        rows, failed = [], 0
        for i, name in enumerate(names):
            node = answers.get(f"r{i}") if isinstance(answers, dict) else None
            kind = errored.get(f"r{i}")
            if isinstance(node, dict):
                row = graphql.flatten(node, self.readme_chars)
                if not (isinstance(row.get("nameWithOwner"), str) and core.REPO_RE.fullmatch(row["nameWithOwner"])):
                    row["nameWithOwner"] = name  # rank refuses a file with a bad name: keep the one asked for
                if kind is not None:  # GitHub filled only part of it
                    row["incomplete"] = kind.lower() or "error"
                if without_readme:
                    row["readme"] = None
                    row.setdefault("incomplete", "too heavy")
                if "incomplete" in row:
                    failed += 1
            elif kind == "NOT_FOUND":  # only GitHub saying so makes a null alias "not found"
                row = {"nameWithOwner": name, "error": "not found"}
            else:
                row = {"nameWithOwner": name, "error": (kind or "").lower() or "no data"}
                failed += 1
            rows.append(row)
        self._emit(rows, failed)
        eprint(f"  {len(names)} repos ({graphql.cost_note(data)})")

    def _emit(self, rows: List[dict], failed: int) -> None:
        for row in rows:
            row["fetched"] = self.fetched
            self.out.write(json.dumps(row) + "\n")
        self.out.flush()  # the rows so far survive a later failure or a kill
        self.written += len(rows)
        self.failed += failed
