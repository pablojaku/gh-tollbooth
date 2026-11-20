"""Rank tests: read sweep rows, keep one entry per repository, filter, sort and print a table.

No mocks except at the boundaries: real temporary files, and tollbooth_http.token / send replaced only to prove
that `rank` never calls them.
Run: uv run pytest
"""
import contextlib
import datetime as dt
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

import tollbooth_cli as cli
import tollbooth_core as core
import tollbooth_graphql as gql
import tollbooth_http as http
import tollbooth_rank as ranking
import tollbooth_sweep as sweeping
from click.testing import CliRunner

from _fixtures import Notices, settings

SECRET = "FAKEsecretFAKE"
FETCHED = "2026-11-20T09:00:00+00:00"


def row(name: str, stars: int | None = 10, **fields: Any) -> dict:
    """A sweep row as tollbooth_sweep writes it (the fields rank reads, plus extras)."""
    return {"nameWithOwner": name, "url": f"https://github.com/{name}", "stargazerCount": stars,
            "pushedAt": "2026-11-01T12:00:00Z", "isArchived": False, "isFork": False, "language": "Python",
            "license": "MIT", "description": f"about {name}", "slice": "s", "query": f"q {name}",
            "fetched": FETCHED, **fields}


def ranked(rows, **options):
    return ranking.rank([ranking.RankRow.model_validate(r) for r in rows], ranking.RankOptions(**options))


def names(entries):
    return [e.name for e in entries]


class FilesCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def write(self, content, name="raw.jsonl"):
        """A file of `content`: a list of rows (one JSON object per line), text or bytes."""
        if isinstance(content, list):
            content = "".join(json.dumps(r) + "\n" for r in content)
        f = self.dir / name
        f.write_bytes(content.encode() if isinstance(content, str) else content)
        return f


class Reader(FilesCase):
    """read_rows: regular files, 64 MB in total, one JSON object per line; errors name path, line and field."""

    def refused(self, *paths):
        with self.assertRaises(core.TollboothError) as cm:
            ranking.read_rows([str(p) for p in paths])
        self.assertEqual(cm.exception.code, core.EX_USAGE)
        self.assertNotIn(SECRET, str(cm.exception))
        return str(cm.exception)

    def test_rows_from_several_files_blank_lines_skipped(self):
        a = self.write([row("o/a"), row("o/b")], "a.jsonl")
        b = self.write("\n" + json.dumps(row("o/c")) + "\r\n\n", "b.jsonl")
        self.assertEqual([r.nameWithOwner for r in ranking.read_rows([str(a), str(b)])], ["o/a", "o/b", "o/c"])

    def test_only_regular_files(self):
        real = self.write([row("o/a")])
        os.symlink(real, self.dir / "link.jsonl")
        self.assertIn("symlink", self.refused(self.dir / "link.jsonl"))
        (self.dir / "d.jsonl").mkdir()
        self.assertIn("not a regular file", self.refused(self.dir / "d.jsonl"))
        os.mkfifo(self.dir / "f.jsonl")
        self.assertIn("not a regular file", self.refused(self.dir / "f.jsonl"))  # and the open didn't hang
        self.assertIn("missing.jsonl", self.refused(self.dir / "missing.jsonl"))

    def test_64_mb_in_total_across_all_files(self):
        small = self.write([row("o/a")], "small.jsonl")
        big = self.dir / "big.jsonl"
        with open(big, "wb") as f:
            f.truncate(64 * 1024 * 1024 - 10)  # sparse: nothing is written, and nothing is read
        message = self.refused(small, big)
        self.assertIn("big.jsonl", message)
        self.assertIn("64 MB in total", message)

    def test_text_that_is_not_utf8(self):
        self.assertIn("not valid UTF-8", self.refused(self.write(b'{"description": "' + SECRET.encode() + b'\xff"}\n')))

    def test_one_json_object_per_line(self):
        good = json.dumps(row("o/a"))
        self.assertEqual(self.refused(self.write(f'{good}\n{{"x": "{SECRET}"\n')).split(": ", 1)[1],
                         "not valid JSON")
        self.assertIn(":2: not valid JSON", self.refused(self.write(f"{good}\n{{'x': 1}}\n")))
        self.assertIn(":1: expected a JSON object per line", self.refused(self.write(f'["{SECRET}"]\n')))

    def test_the_same_size_limits_on_every_python(self):
        # pydantic's JSON parser, not the stdlib's: its limits don't depend on the Python build
        for line in ("[" * 100_000 + "]" * 100_000, '{"a": ' + "[" * 201 + "]" * 201 + "}"):
            self.assertIn(":1: nested more than 200 levels", self.refused(self.write(line + "\n")))
        self.assertEqual(len(ranking.read_rows([str(self.write('{"a": ' + "[" * 200 + "]" * 200 + "}\n"))])), 1)
        self.assertIn(":1: a number too large to read", self.refused(self.write('{"x": ' + "7" * 4301 + "}\n")))
        self.assertEqual(len(ranking.read_rows([str(self.write('{"forkCount": ' + "7" * 4300 + "}\n"))])), 1)

    def test_each_field_rank_uses_must_have_its_type(self):
        bad = {
            "nameWithOwner": ["no-slash", "o/a b", "o/a/b", 5, f"o/{SECRET}\n"],
            "stargazerCount": [True, "5", 1.5],
            "query": [5], "description": [["x"]], "language": [{"name": "C"}], "license": [1], "url": [2],
            "slice": ["a b", "", "x" * 41, 3, f"{SECRET}|"],
            "isArchived": ["yes", 1], "isFork": [0],
            "fetched": ["yesterday", "2026-13-01T00:00:00Z", "2026-11-20", "20261120T090000Z", 5,
                        f"2026-11-20T09:00:00Z{SECRET}"],
            "pushedAt": ["2026-6-1", "soon", "2026-02-30T00:00:00Z", 7],
        }
        for field, values in bad.items():
            for value in values:
                with self.subTest(field=field, value=value):
                    f = self.write([row("o/a"), dict(row("o/b"), **{field: value})])
                    self.assertEqual(self.refused(f), f"{f}:2: bad field '{field}'")

    def test_null_and_missing_fields_are_fine(self):
        nulls = {k: None for k in ("stargazerCount", "query", "description", "language", "license", "url",
                                   "slice", "isArchived", "isFork", "fetched", "pushedAt")}
        f = self.write([dict(row("o/a"), **nulls), {"nameWithOwner": "o/b"}, {}])
        self.assertEqual(len(ranking.read_rows([str(f)])), 3)

    def test_fetched_and_pushed_formats_that_are_accepted(self):
        times = ["2026-11-20T09:00:00Z", "2026-11-20T09:00:00+00:00", "2026-11-20 09:00", "2026-11-20T09:00:00.123",
                 "2026-11-20T09:00:00.123456-05:00"]
        f = self.write([dict(row(f"o/r{i}"), fetched=t, pushedAt=t) for i, t in enumerate(times)])
        self.assertEqual(len(ranking.read_rows([str(f)])), len(times))

    def test_every_row_a_sweep_writes_reads_back_unchanged(self):
        # the contract between the two commands: SweepRow writes, RankRow reads
        node = {"nameWithOwner": "o/r", "url": "https://github.com/o/r", "description": "A tool.", "stargazerCount": 42,
                "forkCount": 3, "pushedAt": "2026-11-01T10:00:00Z", "createdAt": "2020-01-02T03:04:05Z",
                "isArchived": False, "isFork": True, "primaryLanguage": {"name": "Go"}, "licenseInfo": None,
                "repositoryTopics": {"nodes": [{"topic": {"name": "cli"}}]}}
        lines = []
        for repo in (gql.Repo.model_validate(node), gql.Repo.model_validate({"nameWithOwner": "o/bare"})):
            lines.append(sweeping.SweepRow.model_construct(**repo.model_dump(), slice="A", query="x sort:stars-desc",
                                                           rank=1, total=None, fetched=FETCHED).json_line())
        rows = ranking.read_rows([str(self.write("\n".join(lines) + "\n"))])
        self.assertEqual([r.model_dump(exclude_unset=True) for r in rows], [json.loads(ln) for ln in lines])


class Merge(unittest.TestCase):
    """One entry per repository: the newest row, with the slices and queries of every row that found it."""

    def test_one_entry_per_repository_whatever_the_case(self):
        entries = ranked([row("Org/Repo"), row("org/repo"), row("org/other")])
        self.assertEqual(names(entries), ["org/other", "Org/Repo"])  # same stars: by name

    def test_the_newest_row_wins_even_with_fewer_stars(self):
        old = row("o/a", 900, fetched="2026-11-01T09:00:00Z", description="old")
        new = row("o/a", 850, fetched="2026-11-20T09:00:00Z", description="new")
        self.assertEqual(ranked([new, old])[0].row.description, "new")
        self.assertEqual(ranked([old, new])[0].row.description, "new")

    def test_times_compare_as_instants(self):
        east = row("o/a", fetched="2026-11-20T10:00:00+02:00", description="08:00 UTC")
        utc = row("o/a", fetched="2026-11-20T09:00:00Z", description="09:00 UTC")
        naive = row("o/a", fetched="2026-11-20T08:30:00", description="08:30, read as UTC")
        self.assertEqual(ranked([utc, east, naive])[0].row.description, "09:00 UTC")

    def test_at_the_same_time_more_stars_win_then_the_first_row(self):
        rows = [row("o/a", 5, description="first"), row("o/a", 7, description="more"),
                row("o/a", 7, description="later")]
        self.assertEqual(ranked(rows)[0].row.description, "more")

    def test_a_row_without_fetched_counts_as_the_oldest(self):
        rows = [row("o/a", 999, fetched=None, description="no date"), row("o/a", 1, description="dated")]
        self.assertEqual(ranked(rows)[0].row.description, "dated")
        self.assertEqual(ranked([{"nameWithOwner": "o/b"}])[0].row.model_dump(exclude_unset=True), {"nameWithOwner": "o/b"})

    def test_slices_queries_and_hits_come_from_every_row(self):
        rows = [row("o/a", slice="x", query="one"), row("o/a", slice="y", query="two"),
                row("o/a", slice="y", query="one", fetched="2026-11-01T00:00:00Z"),
                row("o/b", slice=None, query=None)]
        a, b = ranked(rows)
        self.assertEqual((a.slices, a.queries, a.hits), ({"x", "y"}, {"one", "two"}, 2))
        self.assertEqual((b.slices, b.queries, b.hits), (set(), set(), 1))

    def test_error_rows_and_rows_without_a_name_are_skipped(self):
        rows = [row("o/a", error="not found"), {"stargazerCount": 5}, row("o/b", incomplete="timeout")]
        self.assertEqual(names(ranked(rows)), ["o/b"])


class Filters(unittest.TestCase):
    def test_min_stars(self):
        self.assertEqual(names(ranked([row("o/a", 5), row("o/b", 20), row("o/c", None)], min_stars=10)), ["o/b"])

    def test_pushed_since_drops_older_and_undated_repos(self):
        rows = [row("o/a", pushedAt="2026-06-15T00:00:00Z"), row("o/b", pushedAt="2026-05-31T23:59:59Z"),
                row("o/c", pushedAt=None), row("o/d", pushedAt="2026-06-01T00:00:00Z")]
        self.assertEqual(sorted(names(ranked(rows, pushed_since=dt.date(2026, 6, 1)))), ["o/a", "o/d"])

    def test_archived_and_forks_only_when_asked(self):
        rows = [row("o/a"), row("o/arch", isArchived=True), row("o/fork", isFork=True)]
        self.assertEqual(names(ranked(rows)), ["o/a"])
        self.assertEqual(sorted(names(ranked(rows, include_archived=True))), ["o/a", "o/arch"])
        self.assertEqual(sorted(names(ranked(rows, include_forks=True))), ["o/a", "o/fork"])

    def test_slice_matches_any_row_of_the_repository(self):
        rows = [row("o/a", slice="x"), row("o/a", slice="y", fetched="2026-11-01T00:00:00Z"), row("o/b", slice="x")]
        self.assertEqual(names(ranked(rows, slice="y")), ["o/a"])

    def test_the_filters_read_the_newest_row(self):
        rows = [row("o/a", 50, fetched="2026-11-01T00:00:00Z"), row("o/a", 3)]
        self.assertEqual(ranked(rows, min_stars=10), [])

    def test_options_are_checked(self):
        bad: Any
        for bad in ({"top": -1}, {"min_stars": -1}, {"by": "forks"}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                ranking.RankOptions(**bad)


class Order(unittest.TestCase):
    def test_by_stars_then_by_name(self):
        rows = [row("o/b", 5), row("o/C", 9), row("o/a", 5), row("o/d", None)]
        self.assertEqual(names(ranked(rows)), ["o/C", "o/a", "o/b", "o/d"])

    def test_by_hits_then_stars_then_name(self):
        rows = [row("o/a", 100), row("o/b", 1, query="1"), row("o/b", 1, query="2"),
                row("o/c", 5, query="1"), row("o/c", 5, query="2"), row("o/d", 5, query="x"), row("o/D2", 5)]
        self.assertEqual(names(ranked(rows, by="hits")), ["o/c", "o/b", "o/a", "o/d", "o/D2"])

    def test_top(self):
        rows = [row(f"o/r{i}", i) for i in range(5)]
        self.assertEqual(names(ranked(rows, top=2)), ["o/r4", "o/r3"])
        self.assertEqual(len(ranked(rows, top=0)), 5)


def table(entries, fmt):
    out = io.StringIO()
    ranking.write_table(entries, fmt, out)
    return out.getvalue()


class Tables(unittest.TestCase):
    """tsv, md and jsonl; every text cell in a table is cleaned and cut to length."""

    def entries(self, **fields):
        return ranked([row("o/a", 18400, slice="x", **fields), row("o/a", 1, slice="y", query="q2",
                                                                   fetched="2026-11-02T00:00:00Z")])

    def test_tsv(self):
        lines = table(self.entries(), "tsv").splitlines()
        self.assertEqual(lines[0].split("\t"), ["stars", "hits", "pushed", "fetched", "lang", "license", "repo",
                                                "slices", "description"])
        self.assertEqual(lines[1].split("\t"), ["18400", "2", "2026-11-01", "2026-11-20", "Python", "MIT", "o/a",
                                                "x,y", "about o/a"])
        self.assertEqual(len(lines), 2)

    def test_md_links_to_github_from_the_name_never_from_the_row_url(self):
        lines = table(self.entries(url="https://evil.example/x"), "md").splitlines()
        self.assertEqual(lines[0], "| Stars | Hits | Pushed | Fetched | Repo | Lang | Slices | Description |")
        self.assertEqual(lines[1], "|---:|---:|---|---|---|---|---|---|")
        self.assertEqual(lines[2], "| 18,400 | 2 | 2026-11-01 | 2026-11-20 | [o/a](https://github.com/o/a) | Python | "
                                   "x,y | about o/a |")
        self.assertNotIn("evil", table(self.entries(url="https://evil.example/x"), "md"))

    def test_missing_values_print_blank(self):
        entries = ranked([{"nameWithOwner": "o/a"}])
        self.assertEqual(table(entries, "tsv").splitlines()[1], "0\t1\t\t\t\t\to/a\t\t")
        self.assertEqual(table(entries, "md").splitlines()[2],
                         "| 0 | 1 |  |  | [o/a](https://github.com/o/a) |  |  |  |")

    def test_text_cells_cannot_break_the_table_or_reach_the_terminal(self):
        # hidden characters go (strip_hidden); tab and line breaks become one space; the cell separator a space
        dirty = "a\tb\nc\r|d\x1b[31m e\x9b2J f‮g\udc80h\U000e0041   i ❤️"
        entries = self.entries(description=dirty, language=dirty, license=dirty)
        for fmt in ("tsv", "md"):
            text = table(entries, fmt)
            with self.subTest(fmt=fmt):
                self.assertEqual(len(text.splitlines()), 2 if fmt == "tsv" else 3)
                for bad in ("\x1b", "\x9b", "‮", "\udc80", "\U000e0041", "️", "\r", "  "):
                    self.assertNotIn(bad, text)
                self.assertIn("a b c d[31m e2J fgh i ❤", text.replace("(31m", "[31m"))
        self.assertEqual(table(entries, "tsv").count("\t"), 16)

    def test_md_cells_cannot_form_links_or_html(self):
        text = table(self.entries(description="see [x](https://evil.example) <img src=y> `z`"), "md")
        row_line = text.splitlines()[2]
        self.assertTrue(row_line.endswith("| see (x)(https://evil.example) img src=y z |"), row_line)
        self.assertEqual(row_line.count("]("), 1)  # only the repo link

    def test_cells_are_cut_to_length(self):
        entries = self.entries(description="d" * 300, language="l" * 300, license="c" * 300)
        cells = table(entries, "tsv").splitlines()[1].split("\t")
        self.assertEqual((len(cells[4]), len(cells[5]), len(cells[8])), (40, 40, 100))
        many = ranked([row("o/a", slice=f"slice{i:02}") for i in range(30)])
        self.assertEqual(len(table(many, "tsv").splitlines()[1].split("\t")[7]), 80)

    def test_jsonl_keeps_the_row_and_adds_slices_queries_and_hits(self):
        out = [json.loads(ln) for ln in table(self.entries(extra={"kept": True}), "jsonl").splitlines()]
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0], dict(row("o/a", 18400, slice="x", extra={"kept": True}), slices=["x", "y"],
                                      queries=["q o/a", "q2"], hits=2))


class Command(FilesCase):
    """`gh-tollbooth rank` end to end: offline, no state folder, no token, no GitHub."""

    def setUp(self):
        super().setUp()
        blocker = self.dir / "file"
        blocker.write_text("")
        self.state = blocker / "state"  # can never be made: its parent is a file
        self.token = mock.Mock(side_effect=AssertionError("rank asked for a token"))
        self.send = mock.Mock(side_effect=AssertionError("rank sent a request"))
        for obj, name, value in ((core, "SETTINGS", settings(state_dir=self.state)), (http, "token", self.token),
                                 (http, "send", self.send)):
            p = mock.patch.object(obj, name, value)
            p.start()
            self.addCleanup(p.stop)

    def main(self, *argv):
        """Run `gh-tollbooth rank ...`. Returns (exit code, stdout, stderr, notices)."""
        notices = Notices()
        with mock.patch.object(core.log, "handlers", [notices]):
            result = CliRunner().invoke(cli.cli, ["rank", *argv], env={"TOLLBOOTH_AS": None}, prog_name="gh-tollbooth")
        if result.exception is not None and not isinstance(result.exception, SystemExit):
            raise result.exception
        self.token.assert_not_called()
        self.send.assert_not_called()
        self.assertFalse(self.state.exists())
        return result.exit_code, result.stdout, result.stderr, "\n".join(notices.lines)

    def test_rank_runs_offline_without_a_state_folder(self):
        a = self.write([row("o/a", 5), row("o/b", 50, slice="t")], "a.jsonl")
        b = self.write([row("O/A", 7, fetched="2026-11-21T00:00:00Z")], "b.jsonl")
        code, out, err, notices = self.main(str(a), str(b), "--format", "md", "--as", "lab", "--budget", "3")
        self.assertEqual(code, 0, err)
        self.assertEqual([ln.split(" | ")[4] for ln in out.splitlines()[2:]],
                         ["[o/b](https://github.com/o/b)", "[O/A](https://github.com/O/A)"])
        self.assertEqual(notices, "2 repos from 3 rows (sorted by stars)")

    def test_filters_and_sorting_from_the_command_line(self):
        f = self.write([row("o/a", 5, query="1"), row("o/a", 5, query="2"), row("o/b", 50), row("o/c", 1)])
        code, out, err, notices = self.main(str(f), "--by", "hits", "--top", "2", "--min-stars", "2",
                                            "--pushed-since", "2026-01-01", "--slice", "s", "--format", "jsonl")
        self.assertEqual(code, 0, err)
        self.assertEqual([json.loads(ln)["nameWithOwner"] for ln in out.splitlines()], ["o/a", "o/b"])
        self.assertIn("(sorted by hits)", notices)

    def test_bad_arguments_exit_2(self):
        f = str(self.write([row("o/a")]))
        for argv in (["--top", "-1"], ["--min-stars", "-1"], ["--pushed-since", "2026-6-1"],
                     ["--pushed-since", "2026-02-30"], ["--slice", "a b"], ["--by", "forks"], ["--format", "csv"],
                     ["--min", "5"], []):
            with self.subTest(argv=argv):
                code, out, _, _ = self.main(*([f] if argv else []), *argv)
                self.assertEqual((code, out), (core.EX_USAGE, ""))

    def test_a_refused_file_exits_2_and_prints_nothing(self):
        f = self.write(f'{{"stargazerCount": "{SECRET}"}}\n')
        code, out, err, _ = self.main(str(f))
        self.assertEqual((code, out), (core.EX_USAGE, ""))
        self.assertIn(":1: bad field 'stargazerCount'", err)
        self.assertNotIn(SECRET, err)

    def test_a_closed_stdout_exits_2(self):
        f = self.write([row("o/a")])
        err = io.StringIO()
        with mock.patch.object(sys, "stdout", None), contextlib.redirect_stderr(err):
            code = cli.main(["rank", str(f)])
        self.assertEqual(code, core.EX_USAGE)
        self.assertIn("stdout is closed", err.getvalue())

    def test_commands_that_talk_to_github_still_need_the_state_folder(self):
        result = CliRunner().invoke(cli.cli, ["status"], env={"TOLLBOOTH_AS": None}, prog_name="gh-tollbooth")
        self.assertEqual(result.exit_code, core.EX_FAIL)
        self.assertIn("TOLLBOOTH_STATE_DIR", result.stderr)


if __name__ == "__main__":
    unittest.main()
