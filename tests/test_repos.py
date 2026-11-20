"""Repos tests: which repositories (the shortlist), and their details fetched in batches.

Mock-first: tollbooth_http.send and tollbooth_http.token are replaced, so nothing leaves the machine. The
throttle is given a FakeClock, so call gaps and waits finish instantly; the CLI tests run the real clock
with no call gap.
Run: python3 -m unittest discover -s tests -v
"""
import contextlib
import io
import json
import re
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from _fixtures import TOKEN, ThrottleCase, answer, http, load_cli, primary
import tollbooth_core as core  # noqa: E402  # pyright: ignore[reportMissingImports]
import tollbooth_guard as guard  # noqa: E402  # pyright: ignore[reportMissingImports]
import tollbooth_loop as loop  # noqa: E402  # pyright: ignore[reportMissingImports]
import tollbooth_repos as repositories  # noqa: E402  # pyright: ignore[reportMissingImports]
import tollbooth_throttle as throttling  # noqa: E402  # pyright: ignore[reportMissingImports]

FETCHED = "2027-01-15T08:00:00+00:00"  # the FakeClock's wall time
cli = load_cli("tollbooth_cli_repos")


def sweep_row(name: str, stars: int = 10, **fields: Any) -> dict:
    """A row as `sweep` writes it (the fields rank reads)."""
    return {"nameWithOwner": name, "stargazerCount": stars, "isArchived": False, "isFork": False,
            "slice": "s", "query": "q", "fetched": "2026-11-20T09:00:00+00:00", **fields}


def node(name: str, **extra: Any) -> dict:
    """A repository node as GitHub answers repos_doc()."""
    return {"nameWithOwner": name, "stargazerCount": 5, "description": "about " + name,
            "primaryLanguage": {"name": "Go"}, "licenseInfo": {"spdxId": "MIT"}, "repositoryTopics": {"nodes": []},
            "homepageUrl": "https://example.org",
            "latestRelease": {"tagName": "v1", "publishedAt": "2026-11-01T00:00:00Z"}, "issues": {"totalCount": 4},
            "defaultBranchRef": {"target": {"committedDate": "2026-11-19T00:00:00Z"}}, **extra}


def details(body: dict, **extra: Any) -> http.Response:
    """GitHub's answer to repos_doc(): every repository asked for, with a README if the document asks for one."""
    v = body.get("variables", {})
    data: dict = {}
    for i in range(len(v) // 2):
        n = node(f"{v[f'o{i}']}/{v[f'n{i}']}")
        if "rm0:" in body["query"]:
            n.update(rm0=None, rm1={"text": f"# {v[f'n{i}']}\nreadme"})
        data[f"r{i}"] = n
    data["rateLimit"] = {"cost": 1, "remaining": 4999}
    return answer(200, body=json.dumps({"data": data, **extra}).encode())


class GitHub:
    """A fake GitHub for repository lookups: records each request; `rule(n, body)` may answer the n-th one."""

    def __init__(self, rule=None):
        self.rule, self.bodies = rule, []

    def __call__(self, method, path, token, accept=None, json_body=None, timeout=30.0):
        self.bodies.append(json_body)
        reply = self.rule(len(self.bodies), json_body) if self.rule else None
        return reply if reply is not None else details(json_body or {})

    def sizes(self):
        return [len(b["variables"]) // 2 for b in self.bodies]


def asks(body: dict, name: str) -> bool:
    """True if the request asks for repository `name`."""
    v = body["variables"]
    return any(f"{v[f'o{i}']}/{v[f'n{i}']}" == name for i in range(len(v) // 2))


def reply(data: dict) -> http.Response:
    return answer(200, body=json.dumps(data).encode())


class Shortlist(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def write(self, rows, name="raw.jsonl"):
        f = Path(self.tmp.name) / name
        f.write_text("".join(json.dumps(r) + "\n" for r in rows))
        return str(f)

    def refused(self, *args):
        with self.assertRaises(core.TollboothError) as cm:
            repositories.shortlist(*args)
        self.assertEqual(cm.exception.code, core.EX_USAGE)
        return str(cm.exception)

    def test_command_line_names_first_then_the_from_rows_in_rank_order(self):
        raw = self.write([sweep_row("o/a", 5), sweep_row("o/b", 50), sweep_row("o/old", 900, isArchived=True),
                          sweep_row("o/fork", 800, isFork=True), sweep_row("o/a", 5, query="q2")])
        self.assertEqual(repositories.shortlist(["z/z"], [raw], 0), ["z/z", "o/b", "o/a"])
        self.assertEqual(repositories.shortlist(["z/z", "y/y"], [], 0), ["z/z", "y/y"])

    def test_several_from_files_are_ranked_together(self):
        a = self.write([sweep_row("o/a", 5), sweep_row("o/b", 50)], "a.jsonl")
        b = self.write([sweep_row("o/a", 500, fetched="2026-11-21T00:00:00Z"), sweep_row("o/c", 1)], "b.jsonl")
        self.assertEqual(repositories.shortlist([], [a, b], 0), ["o/a", "o/b", "o/c"])

    def test_top_limits_only_the_from_rows(self):
        raw = self.write([sweep_row("o/a", 5), sweep_row("o/b", 50), sweep_row("o/c", 1)])
        self.assertEqual(repositories.shortlist(["x/1", "x/2"], [raw], 1), ["x/1", "x/2", "o/b"])
        self.assertEqual(repositories.shortlist(["o/b"], [raw], 2), ["o/b", "o/a"])  # rank's top 2, o/b once

    def test_a_name_is_kept_once_whatever_its_case(self):
        raw = self.write([sweep_row("o/b", 50), sweep_row("O/A", 5)])
        self.assertEqual(repositories.shortlist(["O/B", "o/b", "x/y", "X/Y"], [raw], 0), ["O/B", "x/y", "O/A"])

    def test_a_name_that_is_not_owner_name_shows_at_most_60_characters(self):
        for bad in ("o", "o/a/b", "o/ a", "/a", "o/", "o/a\n", "x" * 100 + " /y"):
            message = self.refused(["ok/fine", bad], [], 0)
            self.assertIn("owner/name", message, bad)
        self.assertNotIn("x" * 61, self.refused(["x" * 100 + " /y"], [], 0))

    def test_nothing_to_fetch(self):
        self.refused([], [], 0)
        self.refused([], [self.write([sweep_row("o/old", isArchived=True)])], 0)

    def test_top_needs_from(self):
        self.assertIn("--from", self.refused(["o/a"], [], 3))

    def test_a_bad_from_file_is_refused_like_rank_refuses_it(self):
        bad = Path(self.tmp.name) / "bad.jsonl"
        bad.write_text('{"stargazerCount": "many"}\n')
        self.assertIn(":1: bad field 'stargazerCount'", self.refused([], [str(bad)], 0))


class ReposCase(ThrottleCase):
    def setUp(self):
        super().setUp()
        p = mock.patch.object(http, "token", mock.Mock(return_value=TOKEN))
        p.start()
        self.addCleanup(p.stop)

    def run_repos(self, names, github, readme=False, readme_chars=3000, budget=None, code=0):
        """repos() with a fake GitHub. Returns (rows, stderr, error); checks the exit code."""
        out, err, error = io.StringIO(), io.StringIO(), None
        with mock.patch.object(http, "send", github), contextlib.redirect_stderr(err):
            try:
                repositories.repos(loop.Caller("lab", budget, self.throttle), names, readme, readme_chars, out)
            except core.TollboothError as e:
                error = e
        self.assertEqual(error.code if error else 0, code, error)
        return [json.loads(ln) for ln in out.getvalue().splitlines()], err.getvalue(), error


def names(n, owner="o"):
    return [f"{owner}/r{i}" for i in range(n)]


class Batches(ReposCase):
    def test_twenty_per_request_or_five_with_the_readme(self):
        for readme, n, sizes in ((False, 45, [20, 20, 5]), (True, 12, [5, 5, 2])):
            github = GitHub()
            rows, _, _ = self.run_repos(names(n), github, readme=readme)
            self.assertEqual((github.sizes(), len(rows)), (sizes, n), readme)

    def test_names_travel_only_as_variables(self):
        github = GitHub()
        self.run_repos(["octo/cat", "acme/kit"], github)
        body = github.bodies[0]
        guard.check_graphql_document(body["query"])
        self.assertEqual(body["variables"], {"o0": "octo", "n0": "cat", "o1": "acme", "n1": "kit"})
        self.assertNotIn("octo", body["query"])
        self.assertNotIn("rm0:", body["query"])
        self.assertEqual(self.throttle.used("lab"), 1)

    def test_one_row_per_repository(self):
        rows, err, _ = self.run_repos(["o/a"], GitHub(), readme=True)
        self.assertEqual(rows, [{"nameWithOwner": "o/a", "stargazerCount": 5, "description": "about o/a",
                                 "homepageUrl": "https://example.org", "language": "Go", "license": "MIT", "topics": [],
                                 "release": "v1", "releasedAt": "2026-11-01T00:00:00Z", "openIssues": 4,
                                 "lastCommit": "2026-11-19T00:00:00Z", "readme": "# a\nreadme", "fetched": FETCHED}])
        self.assertIn("[1/1] o/a", err)
        self.assertIn("cost 1, graphql left 4999", err)
        self.assertIn("PASS: 1 repos", err)

    def test_the_readme_is_cut_to_readme_chars(self):
        rows, _, _ = self.run_repos(["o/a"], GitHub(), readme=True, readme_chars=3)
        self.assertEqual(rows[0]["readme"], "# a")

    def test_readme_chars_is_1_to_20000(self):
        for chars in (0, 20001):
            github = GitHub()
            self.run_repos(["o/a"], github, readme=True, readme_chars=chars, code=core.EX_USAGE)
            self.assertEqual(github.bodies, [])

    def test_progress_is_cut_to_70_characters(self):
        _, err, _ = self.run_repos(names(9, owner="owner-with-a-long-name"), GitHub())
        line = next(ln for ln in err.splitlines() if ln.startswith("[1/1] "))
        self.assertEqual(len(line), len("[1/1] ") + 70)

    def test_a_name_that_github_spells_oddly_keeps_the_name_asked_for(self):
        github = GitHub(lambda n, body: reply({"data": {"r0": node("o/a", nameWithOwner=5)}}))
        rows, _, _ = self.run_repos(["o/a"], github)
        self.assertEqual(rows[0]["nameWithOwner"], "o/a")


class Missing(ReposCase):
    def test_not_found_is_a_row_and_not_a_failure(self):
        data = {"data": {"r0": node("o/a"), "r1": None},
                "errors": [{"type": "NOT_FOUND", "path": ["r1"], "message": "Could not resolve to a Repository"}]}
        rows, err, _ = self.run_repos(["o/a", "o/gone"], GitHub(lambda n, body: reply(data)))
        self.assertEqual(rows[1], {"nameWithOwner": "o/gone", "error": "not found", "fetched": FETCHED})
        self.assertIn("PASS: 2 repos", err)

    def test_any_other_missing_repo_exits_3_after_every_row(self):
        def rule(n, body):
            if n == 1:
                answers: dict = {f"r{i}": node(f"o/r{i}") for i in range(20)}
                answers.update(r3=None, r4=None)
                return reply({"data": answers, "errors": [{"type": "FORBIDDEN", "path": ["r4"]}]})
            return None
        rows, _, error = self.run_repos(names(21), GitHub(rule), code=core.EX_HEAVY)
        self.assertEqual(len(rows), 21)
        self.assertEqual((rows[3]["error"], rows[4]["error"], rows[20]["nameWithOwner"]),
                         ("no data", "forbidden", "o/r20"))
        self.assertIn("2 of 21 repos", str(error))

    def test_part_of_a_repo_missing_marks_it_incomplete(self):
        data = {"data": {"r0": node("o/a", latestRelease=None)},
                "errors": [{"type": "SERVICE_UNAVAILABLE", "path": ["r0", "latestRelease"]}]}
        rows, err, error = self.run_repos(["o/a"], GitHub(lambda n, body: reply(data)), code=core.EX_HEAVY)
        self.assertEqual((rows[0]["incomplete"], rows[0]["release"]), ("service_unavailable", None))
        self.assertIn("warning: SERVICE_UNAVAILABLE", err)
        self.assertIn("1 of 1 repos", str(error))

    def test_a_null_data_or_unrelated_errors(self):
        rows, _, _ = self.run_repos(["o/a"], GitHub(lambda n, body: reply({"data": None})), code=core.EX_HEAVY)
        self.assertEqual(rows[0]["error"], "no data")
        rows, err, _ = self.run_repos(["o/a"], GitHub(lambda n, body: details(body, errors=[{"type": "X"}])))
        self.assertNotIn("error", rows[0])
        self.assertIn("PASS", err)


class TooHeavy(ReposCase):
    def test_a_heavy_batch_is_split_in_half(self):
        github = GitHub(lambda n, body: answer(502) if len(body["variables"]) > 4 else None)
        rows, err, _ = self.run_repos(names(5), github)
        self.assertEqual(github.sizes(), [5, 2, 3, 1, 2])
        self.assertEqual([r["nameWithOwner"] for r in rows], names(5))
        self.assertIn("too heavy; splitting", err)

    def test_one_repo_is_tried_again_without_its_readme(self):
        github = GitHub(lambda n, body: answer(504) if "rm0:" in body["query"] else None)
        rows, err, error = self.run_repos(["o/a"], github, readme=True, code=core.EX_HEAVY)
        self.assertEqual(len(github.bodies), 2)
        self.assertNotIn("rm0:", github.bodies[1]["query"])
        self.assertEqual((rows[0]["readme"], rows[0]["incomplete"], rows[0]["release"]), (None, "too heavy", "v1"))
        self.assertIn("trying again without its README", err)
        self.assertIn("1 of 1 repos", str(error))

    def test_a_repo_still_too_heavy_gets_an_error_row_and_the_run_goes_on(self):
        for readme, sent in ((False, 5), (True, 6)):  # [a b c], [a], [b c], [b], ([b] without README), [c]
            github = GitHub(lambda n, body: answer(502) if asks(body, "o/b") else None)
            rows, _, error = self.run_repos(["o/a", "o/b", "o/c"], github, readme=readme, code=core.EX_HEAVY)
            self.assertEqual(len(github.bodies), sent, readme)
            self.assertEqual([r.get("error") for r in rows], [None, "too heavy", None])
            self.assertEqual(rows[1], {"nameWithOwner": "o/b", "error": "too heavy", "fetched": rows[0]["fetched"]})
            self.assertIn("1 of 3 repos", str(error))


class Stops(ReposCase):
    """A run that stops keeps every row it wrote before; a refused one sends and writes nothing."""

    def test_an_error_answer_exits_1(self):
        for bad, words in ((answer(401, body=b'{"message": "Bad credentials"}'), "HTTP 401"),
                           (answer(200, body=b"<html>"), "isn't a JSON object")):
            github = GitHub(lambda n, body, bad=bad: bad if n == 2 else None)
            rows, _, error = self.run_repos(names(21), github, code=core.EX_FAIL)
            self.assertEqual(len(rows), 20)
            self.assertIn(words, str(error))

    def test_a_spent_budget_sends_nothing(self):
        github = GitHub()
        rows, _, _ = self.run_repos(["o/a"], github, budget=0, code=core.EX_BUDGET)
        self.assertEqual((rows, github.bodies), ([], []))

    def test_a_budget_that_runs_out_midway(self):
        github = GitHub()
        rows, _, _ = self.run_repos(names(21), github, budget=1, code=core.EX_BUDGET)
        self.assertEqual((len(rows), len(github.bodies)), (20, 1))

    def test_a_rate_limit_midway(self):
        far = primary(self.clock.now() + 3000, resource="graphql")
        github = GitHub(lambda n, body: far if n == 2 else None)
        rows, _, error = self.run_repos(names(21), github, code=core.EX_LIMITED)
        self.assertEqual(len(rows), 20)
        self.assertIn("graphql is rate-limited", str(error))


class Command(unittest.TestCase):
    """`gh-tollbooth repos` end to end, with a fake token and a fake GitHub."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name) / "state"
        for target, name, value in ((core, "STATE_DIR", self.state), (core, "SPACING", {k: 0 for k in core.SPACING}),
                                    (http, "token", mock.Mock(return_value=TOKEN))):
            p = mock.patch.object(target, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.raw = Path(self.tmp.name) / "raw.jsonl"
        rows = (sweep_row("o/a", 5), sweep_row("o/b", 50), sweep_row("o/c", 1))
        self.raw.write_text("".join(json.dumps(r) + "\n" for r in rows))

    def main(self, *argv):
        """Run the CLI. Returns (exit code, stdout, stderr, the fake GitHub)."""
        github, out, err = GitHub(), io.StringIO(), io.StringIO()
        with mock.patch.object(http, "send", github), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = cli.main(list(argv))
            except SystemExit as e:  # argparse refuses an argument
                code = e.code
        self.assertNotIn(TOKEN, out.getvalue() + err.getvalue())
        return code, out.getvalue(), err.getvalue(), github

    def test_rows_on_stdout_and_progress_on_stderr(self):
        code, out, err, github = self.main("--as", "lab", "repos", "x/y", "--from", str(self.raw), "--top", "2",
                                           "--readme", "--readme-chars", "4", "--no-cache")
        rows = [json.loads(ln) for ln in out.splitlines()]
        self.assertEqual((code, [r["nameWithOwner"] for r in rows]), (0, ["x/y", "o/b", "o/a"]), err)
        self.assertEqual(rows[1]["readme"], "# b\n")
        self.assertEqual(github.sizes(), [3])
        self.assertIn("PASS: 3 repos", err)
        self.assertEqual(throttling.Throttle(self.state).used("lab"), 1)

    def test_bad_arguments_exit_2_and_send_nothing(self):
        raw = str(self.raw)
        for argv in (["o/a", "--top", "1"], ["o/a", "--readme-chars", "0"], ["o/a", "--readme-chars", "20001"],
                     ["o/a", "--readme-chars", "many"], ["--from", raw, "--top", "-1"], ["not-a-name"], [],
                     ["--fro", raw], ["--from", str(Path(self.tmp.name) / "missing.jsonl")]):
            with self.subTest(argv=argv):
                code, out, _, github = self.main("--as", "lab", "repos", *argv)
                self.assertEqual((code, out, github.bodies), (core.EX_USAGE, "", []))

    def test_a_closed_stdout_is_refused_before_anything_is_sent(self):
        err, github = io.StringIO(), GitHub()
        with mock.patch.object(http, "send", github), contextlib.redirect_stdout(None), contextlib.redirect_stderr(err):
            code = cli.main(["repos", "o/a"])
        self.assertEqual((code, github.bodies), (core.EX_USAGE, []))
        self.assertIn("stdout is closed", err.getvalue())

    def test_the_help_lists_repos(self):
        help_lines = [ln for ln in (cli.__doc__ or "").splitlines() if re.match(r"  gh-tollbooth \w", ln)]
        self.assertEqual({ln.index(ln.split("  ")[-1].strip()) for ln in help_lines}, {48})
        self.assertTrue(any(ln.startswith("  gh-tollbooth repos") for ln in help_lines))


if __name__ == "__main__":
    unittest.main()
