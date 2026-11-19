"""Tests for tollbooth_graphql: what gh-tollbooth knows about GitHub's GraphQL API.
Run: uv run pytest
"""
import unittest

import tollbooth_graphql as gql


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


if __name__ == "__main__":
    unittest.main()
