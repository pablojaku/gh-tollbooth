"""tollbooth_repos: details, and the README if asked, for a list of GitHub repositories.

shortlist() decides which repositories: the names given on the command line first, then the rows of --from
files in rank's order (newest row per repository, most stars first, archives and forks left out), cut to
--top. repos() asks for them in batches (one tollbooth_graphql.lookup() each: one charged request) and writes
one JSON row per repository:
- a repository GitHub reports as NOT_FOUND gets an `error: "not found"` row and doesn't fail the run;
- any other missing repository gets an error row, and one with part of its data missing an `incomplete`
  field; after every row is written the run ends with exit 3;
- a 502/504 splits the batch in half; a single repository is tried once more without its README, and one
  still too heavy gets an `error: "too heavy"` row while the run goes on;
- an error answer (401, a permission 403, not JSON) ends it with exit 1, a spent budget with exit 4 and a rate
  limit with exit 75. Rows are flushed after each batch, so what was fetched before is kept.
It doesn't search: names come from the caller or from rows `sweep` wrote.
Not here: the GraphQL request and reading its answer (tollbooth_graphql).
"""
from __future__ import annotations

import datetime as dt
import json
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, TextIO

import tollbooth_graphql as gql
import tollbooth_rank
from tollbooth_core import EX_HEAVY, EX_USAGE, TollboothError, log
from tollbooth_text import REPO_RE
from tollbooth_throttle import StoppedOnAnswer

if TYPE_CHECKING:
    from tollbooth_loop import Caller

BATCH = 20                # repositories per request
README_BATCH = 5          # repositories per request when their READMEs are asked for
README_CHARS = 3000       # characters of README text per repository, by default
MAX_README_CHARS = 20000  # ...and at most
NAME_CHARS = 60           # characters of a refused name an error message shows
PROGRESS_CHARS = 70       # characters of names a progress notice shows
# In every detail row, null when GitHub sent nothing: the fields GitHub's answer always has a place for
ALWAYS_SHOWN = frozenset({"language", "license", "topics", "release", "releasedAt", "openIssues", "lastCommit"})


def shortlist(names: Sequence[str], from_paths: Sequence[str], top: int) -> list[str]:
    """The repositories to fetch: `names` first, then the --from rows in rank's order, cut to `top` (0 = all).

    A name is kept once, whatever its case. Refusals exit 2 and cost nothing.
    """
    for name in names:
        if not REPO_RE.fullmatch(name):
            raise TollboothError(f"not an owner/name: {name[:NAME_CHARS]!r}", EX_USAGE)
    if top and not from_paths:
        raise TollboothError("--top limits the repos taken from --from: give --from RAW.jsonl, or drop --top", EX_USAGE)
    picked = list(names)
    if from_paths:
        rows = tollbooth_rank.read_rows(from_paths)
        picked += [e.name for e in tollbooth_rank.rank(rows, tollbooth_rank.RankOptions(top=top))]
    seen: set[str] = set()
    unique = []
    for name in picked:
        if name.lower() not in seen:
            seen.add(name.lower())
            unique.append(name)
    if not unique:
        raise TollboothError("no repos to fetch: give owner/name arguments, or --from a file of sweep rows "
                             "(rank leaves archived repos and forks out)", EX_USAGE)
    return unique


def repos(caller: Caller, names: list[str], readme: bool, readme_chars: int, out: TextIO) -> int:
    """Fetch `names` and write one JSON row per repository to `out`; with `readme`, its README cut to `readme_chars`.

    Returns the number of rows written. Raises TollboothError exit 3 after every row is written when some
    repositories came back missing (not NOT_FOUND) or partial, and the Caller's exit codes on the way.
    """
    if not 1 <= readme_chars <= MAX_README_CHARS:
        raise ValueError(f"readme_chars must be 1 to {MAX_README_CHARS}, not {readme_chars}")
    caller.check("graphql")  # a spent budget or a long block fails before any work
    per = README_BATCH if readme else BATCH
    batches = [names[i:i + per] for i in range(0, len(names), per)]
    fetched = dt.datetime.fromtimestamp(caller.throttle.clock.now(), dt.timezone.utc).isoformat(timespec="seconds")
    run = _Run(caller, out, fetched, readme_chars)
    for i, batch in enumerate(batches, 1):
        log.info("[%d/%d] %s", i, len(batches), ", ".join(batch)[:PROGRESS_CHARS])
        run.batch(batch, readme)
    if run.failed:
        raise TollboothError(f"{run.failed} of {len(names)} repos came back without data or only part of it "
                             "(not a NOT_FOUND); the rows printed are incomplete", EX_HEAVY)
    log.info("PASS: %d repos", run.written)
    return run.written


class _Run:
    """One run's state: where rows go, when it started, how many rows it wrote, how many came back short."""

    def __init__(self, caller: Caller, out: TextIO, fetched: str, readme_chars: int):
        self.caller, self.out, self.fetched, self.readme_chars = caller, out, fetched, readme_chars
        self.written = 0
        self.failed = 0

    def batch(self, names: list[str], readme: bool, without_readme: bool = False) -> None:
        """One request for `names`; a 502/504 splits it, retries one repository without its README, or marks it."""
        try:
            answer = gql.lookup(self.caller, names, readme)
        except StoppedOnAnswer as e:
            if e.code != EX_HEAVY:
                raise
            if len(names) > 1:
                log.info("a batch of %d was too heavy; splitting it", len(names))
                mid = len(names) // 2
                self.batch(names[:mid], readme, without_readme)
                self.batch(names[mid:], readme, without_readme)
            elif readme:
                log.info("%s was too heavy; trying again without its README", names[0])
                self.batch(names, False, without_readme=True)
            else:
                log.info("%s is still too heavy; writing an error row and going on", names[0])
                self._emit([{"nameWithOwner": names[0], "error": "too heavy"}], failed=1)
            return
        self._write(names, answer, readme or without_readme, without_readme)

    def _write(self, names: list[str], answer: gql.LookupAnswer, with_readme: bool, without_readme: bool) -> None:
        for line in answer.warnings:
            log.warning("%s", line)
        rows, failed = [], 0
        for name, looked in zip(names, answer.found):
            if looked.repo is not None:
                row = self._detail_row(name, looked.repo, with_readme)
                if looked.error is not None:  # GitHub filled only part of it
                    row["incomplete"] = looked.error.lower() or "error"
                if without_readme:
                    row["readme"] = None
                    row.setdefault("incomplete", "too heavy")
                failed += "incomplete" in row
            elif looked.error == "NOT_FOUND":  # only GitHub saying so makes a missing repository "not found"
                row = {"nameWithOwner": name, "error": "not found"}
            else:
                row = {"nameWithOwner": name, "error": (looked.error or "").lower() or "no data"}
                failed += 1
            rows.append(row)
        self._emit(rows, failed)
        log.info("%d repos (%s)", len(names), answer.cost)

    def _detail_row(self, name: str, repo: gql.RepoDetail, with_readme: bool) -> dict[str, Any]:
        """A repository's row: a field GitHub left empty is left out, but ALWAYS_SHOWN fields stay, null; the
        README only when it was asked for. A name GitHub spelled oddly is replaced by the one asked for, since
        rank refuses a file with a bad name."""
        row = {k: v for k, v in repo.model_dump(exclude={"readme"}).items() if v is not None or k in ALWAYS_SHOWN}
        if not (isinstance(repo.nameWithOwner, str) and REPO_RE.fullmatch(repo.nameWithOwner)):
            row["nameWithOwner"] = name
        if with_readme:
            row["readme"] = repo.readme[:self.readme_chars] if repo.readme else None
        return row

    def _emit(self, rows: list[dict[str, Any]], failed: int) -> None:
        for row in rows:
            row["fetched"] = self.fetched
            self.out.write(json.dumps(row) + "\n")
        self.out.flush()  # the rows so far survive a later failure or a kill
        self.written += len(rows)
        self.failed += failed
