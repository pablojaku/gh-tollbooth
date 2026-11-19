"""Sweep tests: the query file, and many repository searches sent in batches.

tollbooth_http.send and token are replaced, so nothing leaves the machine; the throttle runs on a FakeClock,
so call gaps and waits finish at once.
Run: uv run pytest
"""
import io
import json
import logging
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import tollbooth_core as core
import tollbooth_guard as guard
import tollbooth_http as http
import tollbooth_loop as loop
import tollbooth_sweep as sweeping
import tollbooth_throttle as throttling
from _fixtures import FakeClock, answer, primary, settings

SECRET = "aws_secret_access_key = FAKEsecretFAKEsecret"
FETCHED = "2027-01-15T08:00:00+00:00"  # the FakeClock's wall time


class QueryFile(unittest.TestCase):
    """A narrow .tsv format; refusals name the path and the line, never the file's text."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def write(self, text, name="q.tsv"):
        f = self.dir / name
        f.write_bytes(text.encode() if isinstance(text, str) else text)
        return f

    def refused(self, path):
        with self.assertRaises(core.TollboothError) as cm:
            sweeping.read_queries(str(path))
        self.assertEqual(cm.exception.code, core.EX_USAGE)
        self.assertNotIn("FAKE", str(cm.exception))
        return str(cm.exception)

    def test_slices_and_queries(self):
        f = self.write("# comment\n\nA\tcode wiki\r\nB\tx sort:updated-desc\n  \n")
        self.assertEqual(sweeping.read_queries(str(f)), [sweeping.Query("A", "code wiki sort:stars-desc"),
                                                         sweeping.Query("B", "x sort:updated-desc")])

    def test_only_a_small_regular_tsv_file(self):
        self.refused(self.write(SECRET + "\n", "creds"))
        self.refused(self.dir / "missing.tsv")
        real = self.write("A\tx\n", "real.tsv")
        os.symlink(real, self.dir / "link.tsv")
        self.refused(self.dir / "link.tsv")
        (self.dir / "d.tsv").mkdir()
        self.refused(self.dir / "d.tsv")
        os.mkfifo(self.dir / "f.tsv")
        self.assertIn("not a regular file", self.refused(self.dir / "f.tsv"))  # and the open didn't hang
        self.assertIn("64 KB", self.refused(self.write("A\tx\n#" + "y" * 65536 + "\n")))

    def test_the_size_is_checked_before_reading(self):
        f = self.write("A\tx\n")
        regular = mock.Mock(st_mode=0o100644, st_size=70_000)
        with mock.patch.object(core.os, "fstat", return_value=regular):
            self.assertIn("64 KB", self.refused(f))

    def test_a_bad_line_names_its_number_not_its_text(self):
        for bad in ("no tab " + SECRET, "bad slice\t" + SECRET, "\t" + SECRET, "A\t   ", "L" * 41 + "\t" + SECRET,
                    "A\t" + SECRET + "\tmore", "A\t" + "x" * 300):
            message = self.refused(self.write("# c\n" + bad + "\n"))
            self.assertIn("q.tsv:2", message, bad[:20])
            self.assertNotIn("xxxx", message)

    def test_hidden_characters(self):
        for bad in ("\x00", "\x1b", "\x7f", "​", "﻿", "\x0b", "\x85", "؜", "\U000e0041", "️"):
            message = self.refused(self.write("A\tfine\nB\tbad%sbad\n" % (bad + SECRET)))
            self.assertIn("q.tsv:2", message, repr(bad))
            self.assertNotIn("bad", message.split("q.tsv:2")[1])

    def test_text_that_is_not_utf8(self):
        for raw in (b"A\t\xff\xfe\n", b"A\tx\xed\xa0\x80y\n"):  # the second is a lone surrogate, CESU-style
            self.assertNotIn("codec", self.refused(self.write(raw)))

    def test_an_empty_file(self):
        self.refused(self.write("# only a comment\n"))

    def test_githubs_256_character_limit_counts_the_added_sort(self):
        fits = "x" * (256 - len(" sort:stars-desc"))
        self.assertEqual(len(sweeping.read_queries(str(self.write(f"A\t{fits}\n")))[0].text), 256)
        self.assertIn("256", self.refused(self.write(f"A\t{fits}y\n")))


def repo(name, stars=1):
    return {"nameWithOwner": name, "stargazerCount": stars, "primaryLanguage": {"name": "Go"}, "licenseInfo": None,
            "repositoryTopics": {"nodes": [{"topic": {"name": "wiki"}}]}}


def found(body, **extra):
    """GitHub's answer to a batched search: one repository per query, named after the query's first word."""
    variables = body.get("variables", {})
    data = {f"q{i}": {"repositoryCount": 1, "nodes": [repo("o/" + variables[f"q{i}"].split()[0])]}
            for i in range(len(variables))}
    data["rateLimit"] = {"cost": 1, "remaining": 4999}
    return answer(200, body=json.dumps({"data": data, **extra}).encode())


class GitHub:
    """A fake GitHub for GraphQL searches: records each request; `rule(n, body)` may answer the n-th one."""

    def __init__(self, rule=None):
        self.rule, self.bodies, self.calls = rule, [], []

    def __call__(self, method, path, token, accept=None, json_body=None, timeout=30.0):
        self.calls.append((method, path))
        self.bodies.append(json_body)
        reply = self.rule(len(self.bodies), json_body) if self.rule else None
        return reply if reply is not None else found(json_body)

    def sizes(self):
        return [len(b["variables"]) for b in self.bodies]


class Notices(logging.Handler):
    """Takes the place of the stderr handler: keeps each notice's text."""

    def __init__(self):
        super().__init__()
        self.lines: list[str] = []

    def emit(self, record):
        self.lines.append(record.getMessage())


class SweepCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.clock = FakeClock()
        self.throttle = throttling.Throttle(Path(tmp.name) / "state", self.clock)
        for obj, name, value in ((core, "SETTINGS", settings(deadline=100, max_wait=60)),
                                 (http, "token", mock.Mock(return_value="ghx_FAKEtokenFAKEtoken"))):
            p = mock.patch.object(obj, name, value)
            p.start()
            self.addCleanup(p.stop)

    def run_sweep(self, queries, github, first=20, budget=None, code=0):
        """sweep() with a fake GitHub. Returns (rows, notices on stderr, error); checks the exit code."""
        out, notices, error = io.StringIO(), Notices(), None
        with mock.patch.object(http, "send", github), mock.patch.object(core.log, "handlers", [notices]):
            try:
                sweeping.sweep(loop.Caller("lab", budget, self.throttle), queries, first, out)
            except core.TollboothError as e:
                error = e
        self.assertEqual(error.code if error else 0, code, error)
        return [json.loads(ln) for ln in out.getvalue().splitlines()], "\n".join(notices.lines), error


def queries(*slices):
    return [sweeping.Query(s, f"{s.lower()}term sort:stars-desc") for s in slices]


class Batches(SweepCase):
    def test_a_few_queries_per_request(self):
        for first, sizes in ((20, [5, 2]), (100, [1] * 7), (10, [6, 1]), (17, [5, 2])):
            github = GitHub()
            rows, _, _ = self.run_sweep(queries(*"ABCDEFG"), github, first=first)
            self.assertEqual((github.sizes(), len(rows)), (sizes, 7), first)

    def test_one_row_per_repository(self):
        rows, err, _ = self.run_sweep(queries("A", "B"), GitHub())
        # a repository field GitHub left empty is left out; language, license and total are always there
        self.assertEqual(rows[1], {"nameWithOwner": "o/bterm", "stargazerCount": 1, "language": "Go",
                                   "license": None, "topics": ["wiki"], "slice": "B", "query": "bterm sort:stars-desc",
                                   "rank": 1, "total": 1, "fetched": FETCHED})
        self.assertIn("PASS: 2 queries, 2 rows", err)
        self.assertIn("cost 1, graphql left 4999", err)

    def test_each_request_is_a_query_only_document_with_its_variables(self):
        github = GitHub()
        self.run_sweep(queries("A", "B"), github, first=30)
        self.assertEqual(github.calls, [("POST", "graphql")])
        body = github.bodies[0]
        guard.check_graphql_document(body["query"])
        self.assertIn("first: 30", body["query"])
        self.assertEqual(body["variables"], {"q0": "aterm sort:stars-desc", "q1": "bterm sort:stars-desc"})
        self.assertEqual(self.throttle.used("lab"), 1)

    def test_progress_names_slices_never_query_text(self):
        _, err, _ = self.run_sweep([sweeping.Query("alpha", "secretterm one"),
                                    sweeping.Query("beta", "secretterm two")], GitHub())
        self.assertIn("slices: alpha, beta", err)
        self.assertNotIn("secretterm", err)

    def test_first_outside_1_to_100_is_a_callers_mistake(self):
        for first in (0, 101):  # the command line refuses these before sweep() runs
            github = GitHub()
            with self.assertRaises(ValueError):
                sweeping.sweep(loop.Caller("lab", None, self.throttle), queries("A"), first, io.StringIO())
            self.assertEqual(github.bodies, [])


class TooHeavy(SweepCase):
    def test_a_heavy_batch_is_split_in_half(self):
        github = GitHub(lambda n, body: answer(502) if len(body["variables"]) > 2 else None)
        rows, err, _ = self.run_sweep(queries(*"ABCDE"), github)
        self.assertEqual(github.sizes(), [5, 2, 3, 1, 2])
        self.assertEqual(sorted(r["slice"] for r in rows), list("ABCDE"))
        self.assertIn("too heavy; splitting", err)

    def test_a_single_heavy_query_is_tried_again_with_first_10(self):
        github = GitHub(lambda n, body: answer(504) if "first: 20" in body["query"] else None)
        rows, err, _ = self.run_sweep(queries("A"), github)
        self.assertEqual((len(github.bodies), len(rows)), (2, 1))
        self.assertIn("first: 10", github.bodies[1]["query"])
        self.assertIn("trying again with first: 10", err)

    def test_still_too_heavy_exits_3_and_keeps_the_rows_before(self):
        github = GitHub(lambda n, body: answer(502) if "bterm" in json.dumps(body["variables"]) else None)
        rows, _, error = self.run_sweep(queries("A", "B"), github, first=100, code=core.EX_HEAVY)
        self.assertEqual([r["slice"] for r in rows], ["A"])
        self.assertEqual(len(github.bodies), 3)  # A, then B with first: 100 and first: 10
        self.assertIn("lighter", str(error))


class Partial(SweepCase):
    def answers(self, data):
        return GitHub(lambda n, body: answer(200, body=json.dumps(data).encode()))

    def test_errors_and_null_nodes_count_as_missing_after_every_row(self):
        data = {"data": {"q0": {"repositoryCount": 3, "nodes": [repo("o/a"), None, repo("o/b")]},
                         "q1": {"repositoryCount": 1, "nodes": [repo("o/c")]}},
                "errors": [{"type": "FORBIDDEN", "path": ["q0", "nodes", 1], "message": "SAML enforcement"}]}
        rows, err, error = self.run_sweep(queries("A", "B"), self.answers(data), code=core.EX_HEAVY)
        self.assertEqual([(r["slice"], r["rank"]) for r in rows], [("A", 1), ("A", 3), ("B", 1)])
        self.assertIn("1 of 2 queries", str(error))
        self.assertEqual(str(error).split("slices: ")[1].split(")")[0], "A")
        self.assertIn("warning: FORBIDDEN SAML enforcement", err)
        self.assertNotIn("PASS", err)

    def test_a_null_node_or_a_missing_answer_counts_without_an_error_entry(self):
        for data in ({"data": {"q0": {"nodes": [None]}, "q1": {"nodes": [repo("o/c")]}}},
                     {"data": {"q0": None, "q1": {"nodes": [repo("o/c")]}}},
                     {"data": {"q1": {"nodes": [repo("o/c")]}}}, {"data": None}):
            self.run_sweep(queries("A", "B"), self.answers(data), code=core.EX_HEAVY)

    def test_unrelated_errors_change_nothing(self):
        ok = {"q0": {"nodes": [repo("o/a")]}, "q1": {"nodes": [repo("o/b")]}}
        for errors in ([{"type": "X", "message": "no path"}], [{"type": "X", "path": ["rateLimit"]}], ["junk"]):
            rows, err, _ = self.run_sweep(queries("A", "B"), self.answers({"data": ok, "errors": errors}))
            self.assertEqual(len(rows), 2, errors)
            self.assertIn("PASS", err)


class Stops(SweepCase):
    """A sweep that stops keeps every row it wrote before; a refused one sends and writes nothing."""

    def test_an_error_answer_exits_1(self):
        for reply, words in ((answer(401, body=b'{"message": "Bad credentials"}'), "HTTP 401"),
                             (answer(200, body=b"<html>"), "isn't a JSON object")):
            github = GitHub(lambda n, body, reply=reply: reply if n == 2 else None)
            rows, _, error = self.run_sweep(queries("A", "B"), github, first=100, code=core.EX_FAIL)
            self.assertEqual([r["slice"] for r in rows], ["A"])
            self.assertIn(words, str(error))

    def test_a_spent_budget_sends_nothing(self):
        github = GitHub()
        rows, _, _ = self.run_sweep(queries("A"), github, budget=0, code=core.EX_BUDGET)
        self.assertEqual((rows, github.bodies), ([], []))

    def test_a_budget_that_runs_out_midway(self):
        github = GitHub()
        rows, _, _ = self.run_sweep(queries("A", "B"), github, first=100, budget=1, code=core.EX_BUDGET)
        self.assertEqual(([r["slice"] for r in rows], len(github.bodies)), (["A"], 1))

    def test_a_rate_limit_midway(self):
        far = primary(self.clock.now() + 3000, resource="graphql")
        github = GitHub(lambda n, body: far if n == 2 else None)
        rows, _, error = self.run_sweep(queries("A", "B"), github, first=100, code=core.EX_LIMITED)
        self.assertEqual([r["slice"] for r in rows], ["A"])
        self.assertIn("graphql is rate-limited", str(error))


if __name__ == "__main__":
    unittest.main()
