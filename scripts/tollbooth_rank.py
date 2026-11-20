"""tollbooth_rank: merge, filter and sort the rows a sweep wrote. Stdlib only, Python 3.9+.

read_rows() reads JSONL files (one JSON object per line, as `sweep` writes them) and checks the type
of every field rank uses. rank() keeps one entry per repository: its newest row (by `fetched`), the
slices and queries of every row that found it, and its hits (the distinct queries). Then it filters
and sorts the entries. write_table() prints them as tsv, md or jsonl.

It sends nothing to GitHub: re-ranking is free and needs no token or state folder.

The files are untrusted. They are read only if they are regular files of at most 64 MB in total,
and an error names the path, the line and the field, never a value from the file. Every text cell
in a table is cleaned and cut to length, so a row can't break the table, send terminal codes or,
in Markdown, form a link; a Markdown link is built from the repository's name, never its `url`.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import stat
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Set, TextIO, Tuple

import tollbooth_core as core
from tollbooth_core import EX_USAGE, TollboothError
from tollbooth_guard import BAD_CHARS_RE, NOT_UTF8_RE
from tollbooth_sweep import read_regular_file

MAX_ROWS_BYTES = 64 * 1024 * 1024   # bytes, across all the files one command reads
MAX_DIGITS = 20                     # digits in one JSON integer; Python 3.9 parses longer ones in quadratic time
DESCRIPTION_CHARS = 100             # characters in a table's description cell
SLICES_CHARS = 80                   # characters in a table's slices cell
CELL_CHARS = 40                     # characters in any other text cell
TEXT_FIELDS = ("query", "description", "language", "license", "url")
FLAG_FIELDS = ("isArchived", "isFork")
TSV_HEADER = "stars\thits\tpushed\tfetched\tlang\tlicense\trepo\tslices\tdescription"
MD_HEADER = ("| Stars | Hits | Pushed | Fetched | Repo | Lang | Slices | Description |\n"
             "|---:|---:|---|---|---|---|---|---|")
DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
# ISO 8601 as both Python 3.9 and later read it: a date, a time to the minute, optional seconds with 3 or 6
# fraction digits, and an optional Z or +HH:MM. Checked first, so a file means the same on every Python.
INSTANT_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}[T ][0-9]{2}:[0-9]{2}(:[0-9]{2}(\.[0-9]{3}|\.[0-9]{6})?)?"
                        r"(Z|[+-][0-9]{2}:[0-9]{2})?")
DIRTY_RE = re.compile(r"[\x00-\x1f\x7f-\x9f|]")  # control characters (tab, CR and LF too) and the cell separator
MD_RE = re.compile(r"[<>`]")                     # Markdown: no HTML and no code spans
OLDEST = dt.datetime.min.replace(tzinfo=dt.timezone.utc)


@dataclass(frozen=True)
class RankOptions:
    """What `rank` keeps and how it sorts; the defaults keep every repository except archives and forks."""
    min_stars: int = 0
    pushed_since: Optional[dt.date] = None   # keep repositories pushed on or after this day
    slice: Optional[str] = None              # keep repositories that a query of this slice found
    by: str = "stars"                        # "stars", or "hits" (then stars)
    top: int = 0                             # entries to keep after sorting; 0 = all
    include_archived: bool = False
    include_forks: bool = False

    def __post_init__(self) -> None:
        if self.min_stars < 0 or self.top < 0 or self.by not in ("stars", "hits"):
            raise ValueError("min_stars and top must be 0 or more, and by 'stars' or 'hits'")


@dataclass
class Entry:
    """One repository: its newest row, and the slices and queries of every row that found it."""
    row: dict
    slices: Set[str] = field(default_factory=set)
    queries: Set[str] = field(default_factory=set)

    @property
    def hits(self) -> int:
        """The distinct queries that found the repository (1 for a row that names none)."""
        return max(1, len(self.queries))


class _LongNumber(Exception):
    pass


def read_rows(paths: Sequence[str]) -> List[dict]:
    """Every row in the JSONL files at `paths`, in order. Blank lines are skipped.

    Refusals exit 2 and name the path, the line and the field, never a value from the file.
    """
    rows: List[dict] = []
    used = 0
    for path in paths:
        left = MAX_ROWS_BYTES - used
        with suppress(OSError):  # an unreadable path gets read_regular_file's own message
            st = os.lstat(path)
            if stat.S_ISREG(st.st_mode) and st.st_size > left:
                raise TollboothError(f"input file {path}: the input files are larger than 64 MB in total", EX_USAGE)
        data = read_regular_file(path, "input file", left)
        used += len(data)
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:  # no exception text: it quotes the file's bytes
            raise TollboothError(f"input file {path} is not valid UTF-8", EX_USAGE)
        for n, line in enumerate(text.split("\n"), 1):
            if line.strip():
                rows.append(_parse(line, f"{path}:{n}"))
    return rows


def _parse(line: str, where: str) -> dict:
    try:
        value = json.loads(line, parse_int=_bounded_int)
    except _LongNumber:
        raise TollboothError(f"{where}: a number longer than {MAX_DIGITS} digits", EX_USAGE)
    except (ValueError, RecursionError):  # never echo the parser's message: it quotes the line
        raise TollboothError(f"{where}: not valid JSON", EX_USAGE)
    if not isinstance(value, dict):
        raise TollboothError(f"{where}: expected a JSON object per line", EX_USAGE)
    bad = _bad_field(value)
    if bad:
        raise TollboothError(f"{where}: bad field '{bad}'", EX_USAGE)
    return value


def _bounded_int(text: str) -> int:
    if len(text.lstrip("-")) > MAX_DIGITS:
        raise _LongNumber()
    return int(text)


def _bad_field(row: dict) -> Optional[str]:
    """The name of the first field rank uses whose value has the wrong type, or None. Null counts as missing."""
    for name, ok in FIELD_CHECKS:
        value = row.get(name)
        if value is not None and not ok(value):
            return name
    return None


def _instant(text: str) -> Optional[dt.datetime]:
    """An ISO 8601 date-time as an aware datetime (a time without an offset counts as UTC), or None."""
    if not INSTANT_RE.fullmatch(text):
        return None
    try:
        t = dt.datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


def _day(text: str) -> Optional[dt.date]:
    """The real date a text starts with (YYYY-MM-DD), or None."""
    if not DATE_RE.fullmatch(text[:10]):
        return None
    try:
        return dt.date.fromisoformat(text[:10])
    except ValueError:
        return None


# The fields rank reads, each with the test its value must pass when present and not null.
FIELD_CHECKS: Tuple[Tuple[str, Callable[[object], bool]], ...] = (
    ("nameWithOwner", lambda v: isinstance(v, str) and core.REPO_RE.fullmatch(v) is not None),
    ("stargazerCount", lambda v: isinstance(v, int) and not isinstance(v, bool)),
    *((name, lambda v: isinstance(v, str)) for name in TEXT_FIELDS),
    ("slice", lambda v: isinstance(v, str) and core.LABEL_RE.fullmatch(v) is not None),
    *((name, lambda v: isinstance(v, bool)) for name in FLAG_FIELDS),
    ("fetched", lambda v: isinstance(v, str) and _instant(v) is not None),
    ("pushedAt", lambda v: isinstance(v, str) and _day(v) is not None),
)


def rank(rows: Sequence[dict], options: RankOptions) -> List[Entry]:
    """One entry per repository in `rows` (as read_rows returns them), filtered and sorted by `options`.

    Rows with an `error` or no nameWithOwner are skipped. Names match whatever their case. The newest
    row (by `fetched`; a row without one is the oldest) wins, then the one with more stars, then the first.
    """
    entries: Dict[str, Entry] = {}
    for r in rows:
        name = r.get("nameWithOwner")
        if not name or r.get("error"):
            continue
        entry = entries.get(name.lower())
        if entry is None:
            entry = entries[name.lower()] = Entry(r)
        elif _newness(r) > _newness(entry.row):
            entry.row = r
        if r.get("slice"):
            entry.slices.add(r["slice"])
        if r.get("query"):
            entry.queries.add(r["query"])
    kept = [e for e in entries.values() if _keep(e, options)]
    if options.by == "hits":
        kept.sort(key=lambda e: (-e.hits, -_stars(e.row), e.row["nameWithOwner"].lower()))
    else:
        kept.sort(key=lambda e: (-_stars(e.row), e.row["nameWithOwner"].lower()))
    return kept[:options.top] if options.top else kept


def _newness(row: dict) -> Tuple[dt.datetime, int]:
    fetched = row.get("fetched")
    return (_instant(fetched) if fetched else None) or OLDEST, _stars(row)


def _stars(row: dict) -> int:
    return row.get("stargazerCount") or 0


def _keep(entry: Entry, o: RankOptions) -> bool:
    r = entry.row
    if _stars(r) < o.min_stars:
        return False
    if o.pushed_since is not None:
        pushed = _day(r["pushedAt"]) if r.get("pushedAt") else None
        if pushed is None or pushed < o.pushed_since:
            return False
    if (r.get("isArchived") and not o.include_archived) or (r.get("isFork") and not o.include_forks):
        return False
    return o.slice is None or o.slice in entry.slices


def write_table(entries: Sequence[Entry], fmt: str, out: TextIO) -> None:
    """Print `entries` to `out` as "tsv" (with a header line), "md" (a Markdown table) or "jsonl"."""
    if fmt == "jsonl":
        for e in entries:
            out.write(json.dumps(dict(e.row, slices=sorted(e.slices), queries=sorted(e.queries), hits=e.hits)) + "\n")
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
    return "\t".join([str(_stars(r)), str(e.hits), _date(r.get("pushedAt")), _date(r.get("fetched")),
                      _cell(r.get("language"), CELL_CHARS), _cell(r.get("license"), CELL_CHARS), r["nameWithOwner"],
                      _cell(",".join(sorted(e.slices)), SLICES_CHARS), _cell(r.get("description"), DESCRIPTION_CHARS)])


def _md_line(e: Entry) -> str:
    """One Markdown table row. It leaves out the license, as the gh-era table did."""
    r, name = e.row, e.row["nameWithOwner"]  # a checked owner/name: safe in a link as it is
    cells = [f"{_stars(r):,}", str(e.hits), _date(r.get("pushedAt")), _date(r.get("fetched")),
             f"[{name}](https://github.com/{name})", _md_cell(r.get("language"), CELL_CHARS),
             _md_cell(",".join(sorted(e.slices)), SLICES_CHARS), _md_cell(r.get("description"), DESCRIPTION_CHARS)]
    return "| " + " | ".join(cells) + " |"


def _date(value: object) -> str:
    """The YYYY-MM-DD a checked pushedAt or fetched starts with; blank when missing."""
    return value[:10] if isinstance(value, str) else ""


def _cell(value: object, limit: int) -> str:
    """Text for one table cell: one line, no control or invisible characters, no `|`, at most `limit` characters."""
    if not isinstance(value, str):
        return ""
    text = NOT_UTF8_RE.sub(" ", BAD_CHARS_RE.sub(" ", DIRTY_RE.sub(" ", value)))
    return " ".join(text.split())[:limit].strip()


def _md_cell(value: object, limit: int) -> str:
    """A cell for Markdown: brackets become parentheses and `<`, `>` and backticks spaces, so no link or HTML forms."""
    if not isinstance(value, str):
        return ""
    return _cell(MD_RE.sub(" ", value.replace("[", "(").replace("]", ")")), limit)
