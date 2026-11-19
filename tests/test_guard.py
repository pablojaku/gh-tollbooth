"""Guard tests: every known way to turn a read-only call into a write, a local read or a leak stays refused,
and what is allowed becomes exactly one request.

No GitHub calls: parse_call only reads arguments. Run: python3 -m unittest discover -s tests -v
"""
import tempfile
import unittest
from pathlib import Path

import _fixtures  # noqa: F401  # puts scripts/ on the path
import tollbooth_core as core  # noqa: E402  # pyright: ignore[reportMissingImports]
import tollbooth_guard as guard  # noqa: E402  # pyright: ignore[reportMissingImports]
from tollbooth_guard import CallRequest  # noqa: E402  # pyright: ignore[reportMissingImports]


class GuardCase(unittest.TestCase):
    def assertRefused(self, args, hint=None):
        with self.assertRaises(core.TollboothError, msg=args) as cm:
            guard.parse_call(args)
        self.assertEqual(cm.exception.code, core.EX_USAGE, args)
        if hint:
            self.assertIn(hint, str(cm.exception), args)
        return cm.exception


class Requests(GuardCase):
    """What is allowed becomes one request: REST GET/HEAD, or a GraphQL query POSTed to graphql."""

    def test_a_rest_read(self):
        self.assertEqual(guard.parse_call(["api", "/repos/a/b"]),
                         CallRequest("GET", "repos/a/b", None, None, include=False, silent=False))
        self.assertEqual(guard.parse_call(["api", "-X", "head", "repos/a/b", "-i", "--silent"]),
                         CallRequest("HEAD", "repos/a/b", None, None, include=True, silent=True))
        req = guard.parse_call(["api", "repos/a/b/contents/x", "-H", "Accept: application/vnd.github.raw"])
        self.assertEqual(req.accept, "application/vnd.github.raw")

    def test_rest_fields_become_query_parameters_without_x_get(self):
        for args in (["api", "search/code", "-f", "q=x language:go", "-F", "per_page=5"],
                     ["api", "-X", "GET", "search/code", "-f", "q=x language:go", "-F", "per_page=5"],
                     ["api", "--method=GET", "search/code", "--raw-field=q=x language:go", "--field", "per_page=5"]):
            req = guard.parse_call(args)
            self.assertEqual((req.method, req.path, req.json_body), ("GET", "search/code?q=x+language%3Ago&per_page=5",
                                                                     None), args)
        req = guard.parse_call(["api", "search/issues?q=repo:cli/cli+is:open", "-F", "per_page=5", "-F", "x=null",
                                "-F", "draft=true", "-f", "s=a&b"])
        self.assertEqual(req.path, "search/issues?q=repo:cli/cli+is:open&per_page=5&x=&draft=true&s=a%26b")

    def test_a_graphql_query_with_typed_variables(self):
        doc = "query($q: String!, $n: Int!) { search(query: $q, type: REPOSITORY, first: $n) { repositoryCount } }"
        req = guard.parse_call(["api", "graphql", "-f", f"query={doc}", "-f", "q=typst", "-F", "n=5",
                                "-F", "flag=false", "-F", "none=null", "-F", "big=99999999999999999999",
                                "-f", "operationName=Q", "-f", "raw=5"])
        self.assertEqual((req.method, req.path), ("POST", "graphql"))
        self.assertEqual(req.json_body, {"query": doc, "operationName": "Q",
                                         "variables": {"q": "typst", "n": 5, "flag": False, "none": None,
                                                       "big": "99999999999999999999", "raw": "5"}})
        self.assertEqual(guard.parse_call(["api", "graphql", "-f", "query={ viewer { login } }"]).json_body,
                         {"query": "{ viewer { login } }"})  # no variables: none sent

    def test_the_query_document_is_never_typed_and_may_span_lines(self):
        doc = 'query { a(b: """{branch}""") }'
        self.assertEqual(guard.parse_call(["api", "graphql", "-F", f"query={doc}"]).json_body, {"query": doc})
        guard.parse_call(["api", "graphql", "-f", "query=query {\n  viewer { login }\n}"])

    def test_graphql_documents(self):
        ok = ['{ viewer { login } }',
              'query($q: String!) { search(query: $q, type: REPOSITORY, first: 5) { repositoryCount } }',
              'query { search(query: "mutation testing", type: REPOSITORY, first: 5) { repositoryCount } }',
              'fragment F on Repository { name } query { repository(owner: "a", name: "b") { ...F } }',
              '# mutation in a comment\nquery { viewer { login } }']
        for doc in ok:
            guard.parse_call(["api", "graphql", "-f", f"query={doc}"])
        bad = ['mutation { x }', 'query { a } mutation { b }', 'subscription { x }',
               '# "\nmutation { addStar(input: {}) { clientMutationId } }\n# "',
               'query { a ', '"""unterminated', 'mutation', '', 'fragment F on R { a }']
        for doc in bad:
            self.assertRefused(["api", "graphql", "-f", f"query={doc}"])
        self.assertEqual(guard.graphql_definitions("{ a } # x\r mutation M { b }"), ["query", "mutation"])


class GoneOptions(GuardCase):
    """Options gh had and gh-tollbooth doesn't: refused with the hint an agent needs."""

    def test_jq_and_template_point_to_jq(self):
        for args in (["api", "repos/o/r", "--jq", ".name"], ["api", "repos/o/r", "-q", ".name"],
                     ["api", "repos/o/r", "--jq=.name"], ["api", "repos/o/r", "-t", "{{.name}}"],
                     ["api", "repos/o/r", "--template", "{{.name}}"]):
            self.assertRefused(args, "| jq")

    def test_cache_is_gone(self):
        for args in (["api", "repos/o/r", "--cache", "1h"], ["api", "repos/o/r", "--cache=1h"]):
            self.assertRefused(args, "live, charged request")

    def test_one_call_is_one_request(self):
        for args in (["api", "search/code", "-f", "q=x", "--paginate"], ["api", "repos/o/r", "--slurp"]):
            self.assertRefused(args, "per_page=100")

    def test_other_options_list_what_is_allowed(self):
        for args in (["api", "--hostname", "evil.example", "repos/o/r"], ["api", "--input", "body.json", "graphql"],
                     ["api", "-p", "x", "repos/o/r"], ["api", "--preview", "x", "repos/o/r"],
                     ["api", "--verbose", "repos/o/r"], ["api", "--", "repos/o/r"], ["api", "repos/o/r", "--as", "x"],
                     ["api", "--include=1", "repos/o/r"]):
            self.assertRefused(args, "Allowed:")


class Attacks(GuardCase):
    """Bypasses of the read-only guard that must stay refused."""

    def test_attached_and_duplicate_method_flags(self):
        for args in (["api", "-XDELETE", "repos/o/r"], ["api", "repos/o/r", "-XPOST"],
                     ["api", "-iXPATCH", "repos/o/r"], ["api", "-X=DELETE", "repos/o/r"],
                     ["api", "-X", "GET", "-X", "DELETE", "repos/o/r"],
                     ["api", "-X", "GET", "--method", "DELETE", "repos/o/r"],
                     ["api", "--method=PUT", "repos/o/r"], ["api", "-X", "POST", "repos/o/r/issues", "-f", "t=x"],
                     ["api", "-X", "POST", "graphql", "-f", "query={ a }"], ["api", "-X", "GET", "graphql"],
                     ["api", "repos/o/r", "-X"]):
            self.assertRefused(args)

    def test_attached_fields_and_file_reads(self):
        with tempfile.TemporaryDirectory() as d:
            cred = Path(d) / "q.gql"
            cred.write_text('mutation { addStar(input: {starrableId: "x"}) { clientMutationId } }')
            for args in (["api", "repos/o/r/issues", "-ftitle=pwn"], ["api", "graphql", f"-Fquery=@{cred}"],
                         ["api", "graphql", "-F", f"query=@{cred}"], ["api", "search/code", "-F", "q=@/etc/hosts"],
                         ["api", "graphql", "-F", "query=@-"],
                         ["api", "graphql", "-f", "query={a}", "-F", f"v=@{cred}"]):
                self.assertRefused(args)

    def test_hosts_urls_and_odd_headers(self):
        for args in (["api", "https://api.github.com/graphql", "-f", "query=mutation{x}"],
                     ["api", "https://example.com/x"], ["api", "//evil.example/x"],
                     ["api", "-H", "X-HTTP-Method-Override: DELETE", "repos/o/r"],
                     ["api", "-H", "Accept: a", "-H", "Accept: b", "repos/o/r"],
                     ["api", "-H", "Accept:", "repos/o/r"], ["api", "-H", "Accept: x\nX-Evil: 1", "repos/o/r"],
                     ["api", "repos/o/r", "repos/o/s"], ["api"], []):
            self.assertRefused(args)

    def test_other_gh_commands(self):
        for args in (["pr", "checks", "1", "--watch"], ["issue", "list", "-S", "--", "--jq", "env"],
                     ["repo", "view", "o/r"], ["search", "repos", "typst"], ["run", "list"], ["gist", "view", "abc"]):
            self.assertRefused(args, "API form")

    def test_endpoint_cannot_become_an_option_or_a_look_alike(self):
        for ep in ("/--hostname", "/-qenv", "-XPOST", "//x", "/-", ".-x/y"):
            self.assertRefused(["api", ep])
        self.assertRefused(["api", "/-q", "-X", "GET", "-f", "title=pwn"])
        for ep in ("graphql?query=mutation%7Bx%7D", "GraphQL?query=x", "graphql/", "api/graphql"):
            self.assertRefused(["api", "-X", "GET", ep])
        guard.parse_call(["api", "_private/x"])

    def test_no_placeholders_encoded_or_dot_paths(self):
        for ep in ("{branch}", "repos/{owner}/{repo}/pulls", ":owner", "%67raphql", "repos/o/r/%2e%2e",
                   "graphql/.", "x/../graphql", "./graphql", "repos/o/r/issues?x=:branch",
                   "search/repositories?q=:owner/:repo"):
            self.assertRefused(["api", "-X", "HEAD", ep])

    def test_no_placeholders_in_typed_fields(self):
        for args in (["api", "x/y", "-F", "x={branch}"], ["api", "x/y", "-F", "q={owner}/{repo}"],
                     ["api", "x/y", "--field=x={branch}"], ["api", "x/y", "-F", "x=:branch"],
                     ["api", "graphql", "-f", "query={a}", "-F", "v={branch}"]):
            self.assertRefused(args)
        self.assertEqual(guard.parse_call(["api", "x/y", "-f", "x={branch}"]).path, "x/y?x=%7Bbranch%7D")

    def test_field_keys_are_plain(self):
        for kv in ("noequals", "=x", "a b=x", "labels[]=bug", "a[b]=c", "1x=y"):
            self.assertRefused(["api", "x/y", "-f", kv])

    def test_a_field_given_twice_in_graphql(self):
        self.assertRefused(["api", "graphql", "-f", "query={a}", "-f", "query={b}"])
        self.assertRefused(["api", "graphql", "-f", "query={a}", "-f", "n=1", "-F", "n=2"])
        self.assertRefused(["api", "graphql", "-f", "n=1"], "query=")

    def test_newlines_only_in_the_document(self):
        for args in (["api", "search/code", "-f", "q=x", "-f", "ref=zz\ngh: boom (HTTP 429)"],
                     ["api", "graphql", "-f", "query={a}", "-f", "n=zz\nx"], ["api", "repos/x\n"],
                     ["api", "graphql", "-f", "query={a}", "-f", "query\n=mutation{b}"]):
            self.assertRefused(args)

    def test_bytes_that_are_not_utf8(self):
        # Python turns such argv bytes into lone surrogates (U+DC80-U+DCFF): refused on every path
        for args in (["api", "repos/o/r", "-f", "a=\udcff"], ["api", "graphql", "-f", 'query={ x(a: "\udcff") }'],
                     ["api", "repos/o/r\udcff"], ["api", "repos/o/r", "-H", "Accept: \udcff"]):
            self.assertRefused(args, "UTF-8")

    def test_invisible_and_control_characters(self):
        hidden = 'mutation M { addStar(input: {starrableId: "x"}) { clientMutationId } }'
        for sep in ("\r", " ", "​", "\x85", "﻿", "\x0b", "\x00"):
            self.assertRefused(["api", "graphql", "-f", f"query={{viewer{{login}}}} #{sep} {hidden}",
                                "-f", "operationName=M"], "invisible")


if __name__ == "__main__":
    unittest.main()
