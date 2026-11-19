"""GraphQL tests: the documents gh-tollbooth builds, how it reads GitHub's answers, and post().

Mock-first: tollbooth_http.send and tollbooth_http.token are replaced, so nothing leaves the machine.
Run: python3 -m unittest discover -s tests -v
"""
import json
import unittest
from unittest import mock

from _fixtures import TOKEN, ThrottleCase, answer, http
import tollbooth_core as core  # noqa: E402  # pyright: ignore[reportMissingImports]
import tollbooth_graphql as graphql  # noqa: E402  # pyright: ignore[reportMissingImports]
import tollbooth_guard as guard  # noqa: E402  # pyright: ignore[reportMissingImports]
import tollbooth_loop as loop  # noqa: E402  # pyright: ignore[reportMissingImports]


class SearchDoc(unittest.TestCase):
    def test_one_alias_and_one_variable_per_search(self):
        doc = graphql.search_doc(3, 20)
        guard.check_graphql_document(doc)  # a query only: the same check `call` applies
        self.assertTrue(doc.startswith("query($q0: String!, $q1: String!, $q2: String!) {"))
        for i in range(3):
            self.assertIn(f"q{i}: search(query: $q{i}, type: REPOSITORY, first: 20) {{ repositoryCount nodes", doc)
        self.assertNotIn("q3:", doc)
        self.assertIn("rateLimit { cost remaining resetAt }", doc)


class Flatten(unittest.TestCase):
    def test_a_repository_node_becomes_one_flat_row(self):
        node = {"nameWithOwner": "o/r", "stargazerCount": 7, "description": None, "isFork": False,
                "primaryLanguage": {"name": "Go"}, "licenseInfo": {"spdxId": "MIT"},
                "repositoryTopics": {"nodes": [{"topic": {"name": "wiki"}}, {"topic": {"name": "notes"}}]}}
        self.assertEqual(graphql.flatten(node), {"nameWithOwner": "o/r", "stargazerCount": 7, "isFork": False,
                                                 "language": "Go", "license": "MIT", "topics": ["wiki", "notes"]})

    def test_odd_shapes_read_as_missing(self):
        node = {"nameWithOwner": "o/r", "primaryLanguage": "Go", "licenseInfo": None,
                "repositoryTopics": {"nodes": [None, {"topic": None}, {"topic": {"name": 5}}, "x",
                                               {"topic": {"name": "ok"}}]}}
        row = graphql.flatten(node)
        self.assertEqual((row["language"], row["license"], row["topics"]), (None, None, ["ok"]))
        self.assertEqual(graphql.flatten({"repositoryTopics": "junk"})["topics"], [])


class Errors(unittest.TestCase):
    def test_errors_map_to_the_aliases_they_touch(self):
        data = {"errors": [{"type": "FORBIDDEN", "path": ["q0", "nodes", 1]}, {"type": "X", "path": ["q0"]},
                           {"path": ["q2"]}]}
        self.assertEqual(graphql.error_aliases(data, ["q0", "q1", "q2"]), {"q0": "FORBIDDEN", "q2": ""})

    def test_unrelated_errors_touch_no_alias(self):
        for errors in ([{"type": "X", "message": "no path"}], [{"type": "X", "path": ["rateLimit"]}],
                       [{"type": "X", "path": []}], [{"type": "X", "path": [0]}], ["junk"], "junk", None):
            self.assertEqual(graphql.error_aliases({"errors": errors}, ["q0", "q1"]), {}, errors)

    def test_warnings_replace_control_characters_and_are_short(self):
        data = {"errors": [{"type": "FORBIDDEN", "message": "SAML \x1b[2J\x07 enforcement"},
                           {"type": "X", "message": "m" * 500}, "junk"]}
        lines = graphql.warnings(data)
        self.assertEqual(lines[0], "warning: FORBIDDEN SAML ?[2J? enforcement")
        self.assertEqual(len(lines[1]), graphql.WARNING_CHARS)
        self.assertEqual(lines[2], "warning:")
        self.assertEqual(graphql.warnings({"errors": "junk"}), [])


class CostNote(unittest.TestCase):
    def test_numbers_only(self):
        self.assertEqual(graphql.cost_note({"data": {"rateLimit": {"cost": 1, "remaining": 4999}}}),
                         "cost 1, graphql left 4999")
        for junk in ({}, {"data": None}, {"data": {"rateLimit": {"cost": "\x1b[2J", "remaining": True}}}):
            self.assertEqual(graphql.cost_note(junk), "cost ?, graphql left ?", junk)


class Summary(unittest.TestCase):
    def test_what_the_call_log_shows(self):
        self.assertEqual(graphql.summary("GET", "repos/o/r", None), "GET repos/o/r")
        self.assertEqual(graphql.summary("POST", "graphql", {"query": "{ viewer { login } }", "variables": {}}),
                         "POST graphql { viewer { login } }")
        self.assertEqual(graphql.summary("POST", "graphql", {"query": 5}), "POST graphql")


class Post(ThrottleCase):
    def setUp(self):
        super().setUp()
        p = mock.patch.object(http, "token", mock.Mock(return_value=TOKEN))
        p.start()
        self.addCleanup(p.stop)
        self.caller = loop.Caller("lab", None, self.throttle)

    def post(self, reply, variables=None):
        with mock.patch.object(http, "send", mock.Mock(return_value=reply)) as send:
            data = graphql.post(self.caller, "query($q0: String!) { a }", variables or {})
        return data, send

    def test_one_charged_request_with_the_document_and_its_variables(self):
        data, send = self.post(answer(200, body=b'{"data": {"a": 1}}'), {"q0": "x"})
        self.assertEqual(data, {"data": {"a": 1}})
        args, kwargs = send.call_args
        self.assertEqual((args[:2], kwargs["json_body"]),
                         (("POST", "graphql"), {"query": "query($q0: String!) { a }", "variables": {"q0": "x"}}))
        self.assertEqual(self.throttle.used("lab"), 1)
        _, send = self.post(answer(200, body=b"{}"))
        self.assertNotIn("variables", send.call_args.kwargs["json_body"])

    def test_an_error_answer_or_a_body_that_is_not_json_exits_1(self):
        for reply, words in ((answer(401, body=b'{"message": "Bad credentials"}'), "HTTP 401 Unauthorized"),
                             (answer(403, body=b'{"message": "Resource not accessible"}'), "HTTP 403 Forbidden"),
                             (answer(200, body=b"<html>"), "isn't a JSON object"),
                             (answer(200, body=json.dumps([1]).encode()), "isn't a JSON object")):
            error = self.refused(core.EX_FAIL, self.post, reply)
            self.assertIn(words, str(error))
        self.assertIn("gh auth login", str(self.refused(core.EX_FAIL, self.post, answer(401))))


if __name__ == "__main__":
    unittest.main()
