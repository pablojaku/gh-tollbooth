"""Guard tests: only GraphQL queries pass.

No GitHub calls: the guard only reads what it is given. Run: uv run pytest
"""
import unittest

import tollbooth_core as core
import tollbooth_guard as guard


class GraphQLDocuments(unittest.TestCase):
    """Only query operations and fragments pass; a document that doesn't parse is refused."""

    def assertRefused(self, doc, hint):
        with self.assertRaises(core.TollboothError, msg=doc) as cm:
            guard.check_graphql_document(doc)
        self.assertEqual(cm.exception.code, core.EX_USAGE, doc)
        self.assertIn(hint, str(cm.exception), doc)

    def test_queries_and_fragments_pass(self):
        for doc in ('{ viewer { login } }',
                    'query($q: String!) { search(query: $q, type: REPOSITORY, first: 5) { repositoryCount } }',
                    'query { search(query: "mutation testing", type: REPOSITORY, first: 5) { repositoryCount } }',
                    'fragment F on Repository { name } query { repository(owner: "a", name: "b") { ...F } }',
                    'query { a(b: """{branch}""") }',
                    '# mutation in a comment\nquery { viewer { login } }'):
            guard.check_graphql_document(doc)

    def test_mutations_subscriptions_and_lone_fragments_are_refused(self):
        for doc in ('mutation { x }', 'query { a } mutation { b }', 'subscription { x }',
                    '# "\nmutation { addStar(input: {}) { clientMutationId } }\n# "',
                    'fragment F on R { a }', 'type Repository { name: String }'):
            self.assertRefused(doc, "only GraphQL query operations")

    def test_a_document_that_does_not_parse_is_refused(self):
        too_deep = "{" + "a{" * 5000 + "b" + "}" * 5001
        for doc in ('query { a ', '"""unterminated', 'mutation', '', too_deep):
            self.assertRefused(doc, "doesn't parse")

    def test_keywords_in_strings_and_comments_are_not_definitions(self):
        self.assertEqual(guard.graphql_definitions('query { a(q: "mutation") } # mutation { b }'), ["query"])
        self.assertEqual(guard.graphql_definitions('{ a } # x\r mutation M { b }'),  # a CR ends a comment
                         ["query", "mutation"])


if __name__ == "__main__":
    unittest.main()
