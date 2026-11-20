"""tollbooth_rank: merge, filter and sort the rows a sweep wrote.

read_rows() reads JSONL files (one JSON object per line, as `sweep` writes them) through RankRow, which
checks the type of every field rank uses. rank() keeps one entry per repository: its newest row (by
`fetched`), the slices and queries of every row that found it, and its hits (the distinct queries). Then it
filters and sorts the entries. write_table() prints them as tsv, md or jsonl.

It sends nothing to GitHub: re-ranking is free and needs no token or state folder.

The files are untrusted. They are read only if they are regular files of at most 64 MB in total; each line
is parsed by pydantic's JSON parser, which has the same limits on every Python build (nesting 200 deep,
numbers up to 4,300 digits); an error names the path, the line and the field, never a value from the file.
Every text cell in a table loses its hidden characters, becomes one line without `|` and is cut to length,
so a row can't break the table or, in Markdown, form a link; a Markdown link is built from the repository's
name, never its `url`.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import stat
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Annotated, Any, TextIO

from pydantic import AfterValidator, BaseModel, ConfigDict, ValidationError

from tollbooth_core import EX_USAGE, TollboothError, read_regular_file
from tollbooth_text import LABEL_RE, REPO_RE, strip_hidden

MAX_ROWS_BYTES = 64 * 1024 * 1024   # bytes, across all the files one command reads
DESCRIPTION_CHARS = 100             # characters in a table's description cell
SLICES_CHARS = 80                   # characters in a table's slices cell
CELL_CHARS = 40                     # characters in any other text cell
TSV_HEADER = "stars\thits\tpushed\tfetched\tlang\tlicense\trepo\tslices\tdescription"
MD_HEADER = ("| Stars | Hits | Pushed | Fetched | Repo | Lang | Slices | Description |\n"
             "|---:|---:|---|---|---|---|---|---|")
DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
# ISO 8601 as every supported Python reads it the same way: a date, a time to the minute, optional seconds with
# 3 or 6 fraction digits, and an optional Z or +HH:MM. Checked first, so a file means the same on every Python.
INSTANT_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}[T ][0-9]{2}:[0-9]{2}(:[0-9]{2}(\.[0-9]{3}|\.[0-9]{6})?)?"
                        r"(Z|[+-][0-9]{2}:[0-9]{2})?")
MD_RE = re.compile(r"[<>`]")  # Markdown: no HTML and no code spans
OLDEST = dt.datetime.min.replace(tzinfo=dt.timezone.utc)


def _instant(text: str) -> dt.datetime | None:
    """An ISO 8601 date-time as an aware datetime (a time without an offset counts as UTC), or None."""
    if not INSTANT_RE.fullmatch(text):
        return None
    try:
        t = dt.datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


def _day(text: str) -> dt.date | None:
    """The real date a text starts with (YYYY-MM-DD), or None."""
    if not DATE_RE.fullmatch(text[:10]):
        return None
    try:
        return dt.date.fromisoformat(text[:10])
    except ValueError:
        return None


def _must(test: Any) -> AfterValidator:
    """A check for one field: the value passes `test`, or the row is refused."""
    def check(value: str) -> str:
        if not test(value):
            raise ValueError("wrong form")  # never shown: the reader names only the field
        return value
    return AfterValidator(check)


Name = Annotated[str, _must(lambda v: REPO_RE.fullmatch(v))]
Slice = Annotated[str, _must(lambda v: LABEL_RE.fullmatch(v))]
Instant = Annotated[str, _must(lambda v: _instant(v) is not None)]
Day = Annotated[str, _must(lambda v: _day(v) is not None)]


class RankRow(BaseModel):
    """One row, as `sweep` writes it. Each field rank uses must have its type when present (null counts as
    missing): a count is a whole number, a flag true or false, a text a string, and the name, slice and
    times have their forms. A wrong type refuses the row. Any other field is kept as it is, for jsonl."""

    model_config = ConfigDict(extra="allow", strict=True)

    nameWithOwner: Name | None = None
    stargazerCount: int | None = None
    query: str | None = None
    description: str | None = None
    language: str | None = None
    license: str | None = None
    url: str | None = None
    slice: Slice | None = None
    isArchived: bool | None = None
    isFork: bool | None = None
    fetched: Instant | None = None
    pushedAt: Day | None = None
    error: Any = None   # a row for a repository that couldn't be read; rank skips it


@dataclass(frozen=True)
class RankOptions:
    """What `rank` keeps and how it sorts; the defaults keep every repository except archives and forks."""
    min_stars: int = 0
    pushed_since: dt.date | None = None   # keep repositories pushed on or after this day
    slice: str | None = None              # keep repositories that a query of this slice found
    by: str = "stars"                     # "stars", or "hits" (then stars)
    top: int = 0                          # entries to keep after sorting; 0 = all
    include_archived: bool = False
    include_forks: bool = False

    def __post_init__(self) -> None:
        if self.min_stars < 0 or self.top < 0 or self.by not in ("stars", "hits"):
            raise ValueError("min_stars and top must be 0 or more, and by 'stars' or 'hits'")


@dataclass
class Entry:
    """One repository: its newest row, and the slices and queries of every row that found it."""
    row: RankRow
    slices: set[str] = field(default_factory=set)
    queries: set[str] = field(default_factory=set)

    @property
    def name(self) -> str:
        return self.row.nameWithOwner or ""

    @property
    def hits(self) -> int:
        """The distinct queries that found the repository (1 for a row that names none)."""
        return max(1, len(self.queries))


def read_rows(paths: Sequence[str]) -> list[RankRow]:
    """Every row in the JSONL files at `paths`, in order. Blank lines are skipped.

    Refusals exit 2 and name the path, the line and the field, never a value from the file.
    """
    rows: list[RankRow] = []
    used = 0
    for path in paths:
        left = MAX_ROWS_BYTES - used
        with suppress(OSError):  # a path that can't be read gets read_regular_file's own message
            st = os.lstat(path)
            if stat.S_ISREG(st.st_mode) and st.st_size > left:
                raise TollboothError(f"input file {path}: the input files are larger than 64 MB in total", EX_USAGE)
        data = read_regular_file(path, "input file", left)
        used += len(data)
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:  # its message quotes the file's bytes
            raise TollboothError(f"input file {path} is not valid UTF-8", EX_USAGE) from None
        for n, line in enumerate(text.split("\n"), 1):
            if line.strip():
                rows.append(_parse(line, f"{path}:{n}"))
    return rows


def _parse(line: str, where: str) -> RankRow:
    try:
        return RankRow.model_validate_json(line)
    except ValidationError as e:  # never shown: its text quotes the line
        raise TollboothError(f"{where}: {_why(e)}", EX_USAGE) from None


def _why(e: ValidationError) -> str:
    """What is wrong with a line, in words that quote nothing from it."""
    first = e.errors(include_input=False)[0]
    if first["type"] == "json_invalid":
        parser = str(first.get("ctx", {}).get("error", ""))  # the parser's words and a position
        if parser.startswith("recursion limit"):
            return "nested more than 200 levels"
        if parser.startswith("number out of range"):
            return "a number too large to read"
        return "not valid JSON"
    if not first["loc"]:
        return "expected a JSON object per line"
    return f"bad field '{first['loc'][0]}'"


def rank(rows: Sequence[RankRow], options: RankOptions) -> list[Entry]:
    """One entry per repository in `rows`, filtered and sorted by `options`.

    Rows with an `error` or no nameWithOwner are skipped. Names match whatever their case. The newest row
    (by `fetched`; a row without one is the oldest) wins, then the one with more stars, then the first.
    """
    entries: dict[str, Entry] = {}
    for r in rows:
        if not r.nameWithOwner or r.error:
            continue
        key = r.nameWithOwner.lower()
        entry = entries.get(key)
        if entry is None:
            entry = entries[key] = Entry(r)
        elif _newness(r) > _newness(entry.row):
            entry.row = r
        if r.slice:
            entry.slices.add(r.slice)
        if r.query:
            entry.queries.add(r.query)
    kept = [e for e in entries.values() if _keep(e, options)]
    if options.by == "hits":
        kept.sort(key=lambda e: (-e.hits, -_stars(e.row), e.name.lower()))
    else:
        kept.sort(key=lambda e: (-_stars(e.row), e.name.lower()))
    return kept[:options.top] if options.top else kept


def _newness(row: RankRow) -> tuple[dt.datetime, int]:
    return (_instant(row.fetched) if row.fetched else None) or OLDEST, _stars(row)


def _stars(row: RankRow) -> int:
    return row.stargazerCount or 0


def _keep(entry: Entry, o: RankOptions) -> bool:
    r = entry.row
    if _stars(r) < o.min_stars:
        return False
    if o.pushed_since is not None:
        pushed = _day(r.pushedAt) if r.pushedAt else None
        if pushed is None or pushed < o.pushed_since:
            return False
    if (r.isArchived and not o.include_archived) or (r.isFork and not o.include_forks):
        return False
    return o.slice is None or o.slice in entry.slices


def write_table(entries: Sequence[Entry], fmt: str, out: TextIO) -> None:
    """Print `entries` to `out` as "tsv" (with a header line), "md" (a Markdown table) or "jsonl"."""
    if fmt == "jsonl":
        for e in entries:  # the row as it was read, null fields and unknown fields included
            out.write(json.dumps({**e.row.model_dump(exclude_unset=True), "slices": sorted(e.slices),
                                  "queries": sorted(e.queries), "hits": e.hits}) + "\n")
    elif fmt == "tsv":
        out.write(TSV_HEADER + "\n")
        for e in entries:
            out.write(_tsv_line(e) + "\n")
    elif fmt == "md":
        out.write(MD_HEADER + "\n")
        for e in entries:
            out.write(_md_line(e) + "\n")
    else:
        raise ValueError(f"unknown table format {fmt!r}")


def _tsv_line(e: Entry) -> str:
    r = e.row
    return "\t".join([str(_stars(r)), str(e.hits), _date(r.pushedAt), _date(r.fetched), _cell(r.language, CELL_CHARS),
                      _cell(r.license, CELL_CHARS), e.name, _cell(",".join(sorted(e.slices)), SLICES_CHARS),
                      _cell(r.description, DESCRIPTION_CHARS)])


def _md_line(e: Entry) -> str:
    """One Markdown table row. It leaves out the license, as the gh-era table did."""
    r, name = e.row, e.name  # a checked owner/name: safe in a link as it is
    cells = [f"{_stars(r):,}", str(e.hits), _date(r.pushedAt), _date(r.fetched),
             f"[{name}](https://github.com/{name})", _md_cell(r.language, CELL_CHARS),
             _md_cell(",".join(sorted(e.slices)), SLICES_CHARS), _md_cell(r.description, DESCRIPTION_CHARS)]
    return "| " + " | ".join(cells) + " |"


def _date(value: str | None) -> str:
    """The YYYY-MM-DD a checked pushedAt or fetched starts with; blank when missing."""
    return value[:10] if value else ""


def _cell(value: str | None, limit: int) -> str:
    """Text for one table cell: no hidden characters, one line, no `|`, at most `limit` characters."""
    if not value:
        return ""
    return " ".join(strip_hidden(value).replace("|", " ").split())[:limit].strip()


def _md_cell(value: str | None, limit: int) -> str:
    """A cell for Markdown: brackets become parentheses and `<`, `>` and backticks spaces, so no link or HTML forms."""
    if not value:
        return ""
    return _cell(MD_RE.sub(" ", value.replace("[", "(").replace("]", ")")), limit)
