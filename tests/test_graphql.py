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


class ReposDoc(unittest.TestCase):
    def test_names_only_as_variables_and_the_detail_fields(self):
        doc = graphql.repos_doc(2, readme=False)
        guard.check_graphql_document(doc)
        self.assertTrue(doc.startswith("query($o0: String!, $n0: String!, $o1: String!, $n1: String!) {"))
        self.assertIn("r0: repository(owner: $o0, name: $n0) {", doc)
        self.assertIn("r1: repository(owner: $o1, name: $n1) {", doc)
        self.assertNotIn("r2:", doc)
        for part in ("nameWithOwner", "homepageUrl", "latestRelease { tagName publishedAt }",
                     "issues(states: OPEN) { totalCount }",
                     "defaultBranchRef { target { ... on Commit { committedDate } } }",
                     "repositoryTopics(first: 6)", "rateLimit { cost remaining resetAt }"):
            self.assertIn(part, doc)
        self.assertNotIn("object(", doc)

    def test_readme_aliases_only_when_asked(self):
        doc = graphql.repos_doc(1, readme=True)
        guard.check_graphql_document(doc)
        paths = ("README.md", "readme.md", "Readme.md", "README.rst", "README", "README.markdown", "README.txt")
        for j, path in enumerate(paths):
            self.assertIn(f'rm{j}: object(expression: "HEAD:{path}") {{ ... on Blob {{ text }} }}', doc)
        self.assertNotIn(f"rm{len(paths)}:", doc)


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


    def test_a_detail_node_adds_release_issues_last_commit_and_readme(self):
        node = {"nameWithOwner": "o/r", "homepageUrl": "https://o.example", "repositoryTopics": {"nodes": []},
                "latestRelease": {"tagName": "v2", "publishedAt": "2026-11-01T00:00:00Z"}, "issues": {"totalCount": 4},
                "defaultBranchRef": {"target": {"committedDate": "2026-11-19T10:00:00Z"}},
                "rm0": None, "rm1": {"text": ""}, "rm2": {"text": "# Title\nbody"}, "rm3": {"text": "second"}}
        self.assertEqual(graphql.flatten(node), {
            "nameWithOwner": "o/r", "homepageUrl": "https://o.example", "language": None, "license": None, "topics": [],
            "release": "v2", "releasedAt": "2026-11-01T00:00:00Z", "openIssues": 4,
            "lastCommit": "2026-11-19T10:00:00Z",
            "readme": "# Title\nbody"})

    def test_detail_fields_of_the_wrong_shape_read_as_missing(self):
        node = {"latestRelease": None, "issues": {"totalCount": True}, "defaultBranchRef": {"target": "x"},
                "rm0": {"text": 5}, "rm1": "junk", "rm2": None}
        row = graphql.flatten(node)
        self.assertEqual([row[k] for k in ("release", "releasedAt", "openIssues", "lastCommit", "readme")], [None] * 5)
        self.assertEqual(graphql.flatten({"latestRelease": {"tagName": 7}, "issues": {"totalCount": 3}})["release"],
                         None)

    def test_a_search_node_gets_no_detail_fields(self):
        row = graphql.flatten({"nameWithOwner": "o/r", "stargazerCount": 1})
        for key in ("release", "releasedAt", "openIssues", "lastCommit", "readme"):
            self.assertNotIn(key, row)

    def test_the_readme_is_cut_after_cleaning(self):
        row = graphql.flatten({"rm0": {"text": "\u200b" * 10 + "abcdef"}}, readme_chars=3)
        self.assertEqual(row["readme"], "abc")
        self.assertEqual(len(graphql.flatten({"rm0": {"text": "x" * 5000}})["readme"]), graphql.README_CHARS)


class Cleaning(unittest.TestCase):
    """Text a repository's owner wrote loses what hides text from a reader but not from a model."""

    # control characters, C1 controls, CR, zero-width, bidi, separators, BOM, word joiners and every tag character
    HIDDEN = ("\x00\x07\x1b\x7f\x85\x9b\r\u200b\u200d\u200f\u202a\u202e\u2066\u2069\u2028\u2029\ufeff"
              "\u2060\u2064" + "".join(map(chr, range(0xE0000, 0xE0080))))

    def test_hidden_characters_leave_the_description_and_the_readme(self):
        text = "a" + self.HIDDEN + "b\nc\td"
        row = graphql.flatten({"description": text, "rm0": {"text": text}})
        self.assertEqual((row["description"], row["readme"]), ("ab\nc\td", "ab\nc\td"))

    def test_visible_text_is_kept(self):
        text = "\u00dcn\u00efc\u00f6d\u00e9 \u2014 \u65e5\u672c \u2764\ufe0f [link](x) <b> | ok"
        row = graphql.flatten({"description": text, "rm0": {"text": text}})
        self.assertEqual((row["description"], row["readme"]), (text, text))

    def test_other_fields_are_left_as_github_sent_them(self):
        self.assertEqual(graphql.flatten({"homepageUrl": "x\u200by"})["homepageUrl"], "x\u200by")


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
