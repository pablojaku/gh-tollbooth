"""Tests for tollbooth_graphql: what gh-tollbooth knows about GitHub's GraphQL API.
search() runs through a real Caller and Throttle; tollbooth_http.send and token are replaced, so nothing
leaves the machine.
Run: uv run pytest
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import tollbooth_core as core
import tollbooth_graphql as gql
import tollbooth_guard as guard
import tollbooth_http as http
import tollbooth_loop as loop
import tollbooth_throttle as throttling
from _fixtures import FakeClock, answer, settings

TOKEN = "ghx_FAKEtokenFAKEtoken"


def node(name="o/r", **changed):
    """A repository node as GitHub sends it for the search document."""
    raw = {"nameWithOwner": name, "url": f"https://github.com/{name}", "description": "A tool.",
           "stargazerCount": 42, "forkCount": 3, "pushedAt": "2026-11-01T10:00:00Z", "createdAt": "2020-01-02T03:04:05Z",
           "isArchived": False, "isFork": False, "primaryLanguage": {"name": "Go"}, "licenseInfo": {"spdxId": "MIT"},
           "repositoryTopics": {"nodes": [{"topic": {"name": "cli"}}, {"topic": {"name": "github"}}]}}
    raw.update(changed)
    return raw


def results(*nodes, total=None):
    """One alias's answer: repositoryCount and its nodes."""
    return {"repositoryCount": len(nodes) if total is None else total, "nodes": list(nodes)}


def reply(*searches, errors=None, rate=(1, 4999)):
    """GitHub's 200 answer: q0, q1, ... in order (None leaves that alias out), plus rateLimit."""
    data = {f"q{i}": s for i, s in enumerate(searches) if s is not None}
    data["rateLimit"] = {"cost": rate[0], "remaining": rate[1], "resetAt": "2026-11-19T19:00:00Z"}
    body = {"data": data} if errors is None else {"data": data, "errors": errors}
    return answer(200, body=json.dumps(body).encode())


class Summary(unittest.TestCase):
    """What the call log shows for a request."""

    def test_the_method_the_path_and_a_documents_text(self):
        self.assertEqual(gql.summary("GET", "repos/o/r", None), "GET repos/o/r")
        self.assertEqual(gql.summary("POST", "graphql", {"query": "{ viewer { login } }", "variables": {"q": "x"}}),
                         "POST graphql { viewer { login } }")

    def test_anything_else_shows_no_document(self):
        for body in ({"query": 5}, ["{ a }"], "{ a }", {}):
            self.assertEqual(gql.summary("POST", "graphql", body), "POST graphql", body)


class HeavyQueryAdvice(unittest.TestCase):
    def test_says_how_to_make_a_query_lighter(self):
        for words in ("502/504", "fewer aliases", "smaller first:", "fewer nested connections"):
            self.assertIn(words, gql.HEAVY_QUERY_ADVICE)


class SearchCase(unittest.TestCase):
    """A Caller on a temporary state folder with a fake clock; GitHub answers from `self.answers`."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.throttle = throttling.Throttle(Path(tmp.name) / "state", FakeClock())
        self.caller = loop.Caller("lab", None, self.throttle)
        self.answers = []
        self.send = mock.Mock(side_effect=lambda *a, **k: self.answers.pop(0))
        for obj, name, value in ((core, "SETTINGS", settings(deadline=100, max_wait=60)), (http, "send", self.send),
                                 (http, "token", mock.Mock(return_value=TOKEN))):
            p = mock.patch.object(obj, name, value)
            p.start()
            self.addCleanup(p.stop)

    def search(self, queries=("topic:cli",), first=20, *replies):
        self.answers = list(replies) or [reply(*[results(node()) for _ in queries])]
        return gql.search(self.caller, list(queries), first)

    def sent(self):
        return self.send.call_args.kwargs["json_body"]


class Document(SearchCase):
    def test_one_alias_and_one_variable_per_query(self):
        self.search(["topic:cli lang:go", "rate limit"], 7)
        doc, variables = self.sent()["query"], self.sent()["variables"]
        self.assertEqual(variables, {"q0": "topic:cli lang:go", "q1": "rate limit"})
        self.assertIn("query($q0: String!, $q1: String!)", doc)
        self.assertIn("q0: search(query: $q0, type: REPOSITORY, first: 7)", doc)
        self.assertIn("q1: search(query: $q1, type: REPOSITORY, first: 7)", doc)
        self.assertIn("rateLimit { cost remaining resetAt }", doc)

    def test_a_query_the_guard_lets_through_with_no_query_text_in_it(self):
        self.search(["topic:cli lang:go"])
        guard.check_graphql_document(self.sent()["query"])  # raises if it isn't a query
        self.assertNotIn("topic:cli", self.sent()["query"])

    def test_one_charged_request(self):
        self.search(["a", "b", "c"])
        self.assertEqual((self.send.call_count, self.throttle.used("lab")), (1, 1))
        self.assertTrue(self.throttle.entries()[0].summary.startswith("POST graphql query("))


class Repos(SearchCase):
    def test_a_node_becomes_one_flat_repo(self):
        (found,) = self.search().found
        ((place, repo),) = found.ranked
        self.assertEqual(place, 1)
        self.assertEqual(repo.model_dump(), {
            "nameWithOwner": "o/r", "url": "https://github.com/o/r", "description": "A tool.", "stargazerCount": 42,
            "forkCount": 3, "pushedAt": "2026-11-01T10:00:00Z", "createdAt": "2020-01-02T03:04:05Z",
            "isArchived": False, "isFork": False, "language": "Go", "license": "MIT", "topics": ["cli", "github"]})

    def test_a_description_loses_its_hidden_characters(self):
        smuggled = "".join(chr(0xE0000 + ord(c)) for c in "obey")
        (found,) = self.search(["q"], 20, reply(results(node(description=f"A \u202efast\u202c tool.{smuggled}")))).found
        self.assertEqual(found.ranked[0][1].description, "A fast tool.")

    def test_a_value_of_the_wrong_type_reads_as_missing(self):
        odd = node(stargazerCount="42", forkCount=True, isArchived="no", description=7, primaryLanguage="Go",
                   licenseInfo={"spdxId": 5},
                   repositoryTopics={"nodes": [{"topic": {"name": "ok"}}, {"topic": 5}, "x", {"topic": {"name": 7}}]})
        (found,) = self.search(["q"], 20, reply(results(odd))).found
        repo = found.ranked[0][1]
        self.assertEqual((repo.stargazerCount, repo.forkCount, repo.isArchived, repo.description, repo.language,
                          repo.license, repo.topics), (None, None, None, None, None, None, ["ok"]))
        self.assertEqual(repo.nameWithOwner, "o/r")


class Complete(SearchCase):
    def test_a_full_answer_is_complete_with_githubs_total(self):
        (found,) = self.search(["q"], 20, reply(results(node("a/b"), node("c/d"), total=1234))).found
        self.assertEqual(([p for p, _ in found.ranked], found.total, found.complete), ([1, 2], 1234, True))

    def test_a_missing_answer_an_error_or_a_null_node_is_incomplete(self):
        errors = [{"type": "TIMEOUT", "message": "took too long", "path": ["q2", "nodes", 1]}]
        got = self.search(["a", "b", "c", "d"], 20,
                          reply(results(node()), None, results(node("x/y")), results(node("e/f"), None, node("g/h")),
                                errors=errors)).found
        self.assertEqual([f.complete for f in got], [True, False, False, False])
        self.assertEqual(got[1].ranked, [])
        self.assertEqual([r.nameWithOwner for _, r in got[2].ranked], ["x/y"])  # its rows still count
        self.assertEqual([p for p, _ in got[3].ranked], [1, 3])  # places keep the null node's gap

    def test_errors_on_nothing_it_asked_change_nothing(self):
        errors = [{"type": "X", "message": "about rateLimit", "path": ["rateLimit"]}, {"message": "no path"}, "junk"]
        answer_ = self.search(["a"], 20, reply(results(node()), errors=errors))
        self.assertTrue(answer_.found[0].complete)
        self.assertEqual(len(answer_.warnings), 2)

    def test_a_total_that_isnt_a_number_is_none(self):
        for total in ("many", True, None):
            (found,) = self.search(["q"], 20, reply({"repositoryCount": total, "nodes": [node()]})).found
            self.assertIsNone(found.total, total)


class WarningsAndCost(SearchCase):
    def test_one_warning_per_error_marked_and_cut(self):
        errors = [{"type": "TIMEOUT", "message": "x\x1b]0;t\x07" + "y" * 300, "path": ["q0"]}]
        (line,) = self.search(["a"], 20, reply(results(node()), errors=errors)).warnings
        self.assertTrue(line.startswith("warning: TIMEOUT x?]0;t?y"), line)
        self.assertEqual(len(line), gql.WARNING_CHARS)

    def test_the_cost_note_shows_numbers_only(self):
        self.assertEqual(self.search(["a"], 20, reply(results(node()), rate=(1, 4999))).cost,
                         "cost 1, graphql left 4999")
        self.assertEqual(self.search(["a"], 20, reply(results(node()), rate=("1", True))).cost,
                         "cost ?, graphql left ?")


class ErrorAnswers(SearchCase):
    def test_an_error_answer_is_exit_1(self):
        for status, words in ((401, "gh auth login"), (403, "HTTP 403 Forbidden")):
            with self.assertRaises(core.TollboothError) as cm:
                self.search(["a"], 20, answer(status, body=b'{"message": "no"}'))
            self.assertEqual(cm.exception.code, core.EX_FAIL)
            self.assertIn(words, str(cm.exception))

    def test_a_body_that_isnt_a_json_object_is_exit_1_and_never_quoted(self):
        for body in (b"<html>secret", b"[]", b'"secret"', b"[" * 300 + b"]" * 300, b'{"data": 1' + b"0" * 5000 + b"}"):
            with self.assertRaises(core.TollboothError) as cm:
                self.search(["a"], 20, answer(200, body=body))
            self.assertEqual(cm.exception.code, core.EX_FAIL, body[:20])
            self.assertIn("isn't a JSON object", str(cm.exception))
            self.assertNotIn("secret", str(cm.exception))

    def test_a_query_github_gave_up_on_stops_with_its_answer(self):
        with self.assertRaises(loop.StoppedOnAnswer) as cm:
            self.search(["a"], 20, answer(502))
        self.assertEqual(cm.exception.code, core.EX_HEAVY)


if __name__ == "__main__":
    unittest.main()
