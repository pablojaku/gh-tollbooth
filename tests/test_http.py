"""tollbooth_http tests: the one request it can send.

The tests that send use a real HTTP server on 127.0.0.1, so they check what httpx really does with
redirects, timeouts, framing and headers. Nothing leaves the machine and `gh` is never run.
Run: uv run pytest
"""
import json
import os
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, cast
from unittest import mock

import tollbooth_http as http

TOKEN = "ghx_FAKEtokenFAKEtoken"


class Handler(BaseHTTPRequestHandler):
    def serve(self):
        server = cast("QuietServer", self.server)
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        server.seen.append((self.command, self.path, {k.lower(): v for k, v in self.headers.items()}, body))
        server.reply(self)

    do_GET = do_HEAD = do_POST = do_PUT = do_PATCH = do_DELETE = serve

    def log_message(self, format, *args):
        pass

    def reply(self, status, headers=(), payload=b"{}"):
        self.send_response(status)
        for name, value in headers:
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)


class QuietServer(ThreadingHTTPServer):
    seen: list                          # (method, path, headers, body) of each request; set by LoopbackCase
    reply: Callable[[Handler], object]  # sends the answer; set by each test

    def handle_error(self, request, client_address):
        pass  # a client that gave up (the timeout tests) is not worth a traceback


class LoopbackCase(unittest.TestCase):
    """tollbooth_http.API points at a server on 127.0.0.1; `self.server.reply` decides the answer."""

    def setUp(self):
        self.server = QuietServer(("127.0.0.1", 0), Handler)
        self.server.seen = []
        self.server.reply = lambda h: h.reply(200)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.port = self.server.server_address[1]
        clean = {k: v for k, v in os.environ.items() if "proxy" not in k.lower()}
        clean["NO_PROXY"] = "127.0.0.1"  # a proxy set on this machine must not catch loopback requests
        for p in (mock.patch.object(http, "API", f"http://127.0.0.1:{self.port}"),
                  mock.patch.dict(os.environ, clean, clear=True)):
            p.start()
            self.addCleanup(p.stop)

    def reply_with(self, status, headers=(), payload=b"{}"):
        self.server.reply = lambda h: h.reply(status, headers, payload)


class Send(LoopbackCase):
    def test_get_returns_status_headers_and_body(self):
        self.reply_with(200, [("X-RateLimit-Remaining", "4999"), ("X-Dup", "a"), ("X-Dup", "b")], b'{"a": 1}')
        r = http.send("GET", "repos/o/r?per_page=5", TOKEN)
        self.assertEqual((r.status, r.body), (200, b'{"a": 1}'))
        self.assertEqual(r.headers["x-ratelimit-remaining"], "4999")  # names are lower-cased
        self.assertEqual(r.headers["x-dup"], "a, b")
        method, path, sent, body = self.server.seen[0]
        self.assertEqual((method, path, body), ("GET", "/repos/o/r?per_page=5", b""))
        self.assertEqual(sent["authorization"], f"Bearer {TOKEN}")
        self.assertEqual(sent["user-agent"], "gh-tollbooth")
        self.assertEqual(sent["accept"], "application/vnd.github+json")

    def test_accept_can_be_chosen(self):
        http.send("GET", "repos/o/r/readme", TOKEN, accept="application/vnd.github.raw")
        self.assertEqual(self.server.seen[0][2]["accept"], "application/vnd.github.raw")

    def test_an_error_status_is_an_answer_not_an_exception(self):
        for status in (401, 403, 404, 429, 502):
            self.reply_with(status, [("Retry-After", "7")], b'{"message": "no"}')
            r = http.send("GET", "repos/o/r", TOKEN)
            self.assertEqual((r.status, r.headers["retry-after"], r.body), (status, "7", b'{"message": "no"}'))

    def test_a_redirect_is_returned_not_followed(self):
        # following one would send the token wherever Location points
        for status in (301, 302, 307, 308):
            self.server.seen.clear()
            self.reply_with(status, [("Location", f"http://127.0.0.1:{self.port}/elsewhere")])
            r = http.send("GET", "repos/old/name", TOKEN)
            self.assertEqual((r.status, r.headers["location"]), (status, f"http://127.0.0.1:{self.port}/elsewhere"))
            self.assertEqual([seen[1] for seen in self.server.seen], ["/repos/old/name"])

    def test_post_graphql_sends_the_document_as_json(self):
        doc = {"query": "query($q: String!) { search(query: $q) { repositoryCount } }", "variables": {"q": "a b"}}
        http.send("POST", "graphql", TOKEN, json_body=doc)
        method, path, sent, body = self.server.seen[0]
        self.assertEqual((method, path, sent["content-type"]), ("POST", "/graphql", "application/json"))
        self.assertEqual(json.loads(body), doc)

    def test_head_has_no_body(self):
        self.reply_with(200, [("X-RateLimit-Remaining", "9")], b"ignored")
        r = http.send("HEAD", "repos/o/r", TOKEN)
        self.assertEqual((r.status, r.body, self.server.seen[0][0]), (200, b"", "HEAD"))


class OnlyReadsOnlyGitHub(LoopbackCase):
    """Whatever a caller passes, this module sends GET, HEAD or POST graphql, to API, with clean headers."""

    def refused(self, *args, **kwargs):
        with self.assertRaises(ValueError, msg=(args, kwargs)) as cm:
            http.send(*args, **kwargs)
        self.assertEqual(self.server.seen, [], (args, kwargs))
        self.assertNotIn(TOKEN, str(cm.exception))  # an error message is printed; the token must never be in it

    def test_methods(self):
        for method in ("PUT", "PATCH", "DELETE", "get", "CONNECT", ""):
            self.refused(method, "repos/o/r", TOKEN)
        for path in ("repos/o/r/issues", "graphql?x=1", "graphql/", "GraphQL", "api/graphql"):
            self.refused("POST", path, TOKEN, json_body={"query": "{a}"})

    def test_a_body_goes_with_post_graphql_only(self):
        self.refused("GET", "repos/o/r", TOKEN, json_body={"title": "x"})
        self.refused("HEAD", "repos/o/r", TOKEN, json_body={})
        self.refused("POST", "graphql", TOKEN)

    def test_the_path_cannot_leave_the_api(self):
        for path in ("", "/repos/o/r", "//evil.example/x", "http://evil.example/x", "@evil.example/x",
                     "\\evil.example", ".evil.example", "repos/o r", "repos/o/r\n", "repos/o/r\r\nX-A: b",
                     "repos/\x00", "repos/é", "repos/o/r\u2028", "search/issues?q=bug#123"):
            self.refused("GET", path, TOKEN)

    def test_header_values_cannot_carry_a_second_header(self):
        self.refused("GET", "repos/o/r", TOKEN + "\r\nX-Evil: 1")
        self.refused("GET", "repos/o/r", "tok en")
        self.refused("GET", "repos/o/r", "")
        self.refused("GET", "repos/o/r", TOKEN, accept="text/plain\r\nX-Evil: 1")
        self.refused("GET", "repos/o/r", TOKEN, accept="text/plain\x00")


class TheRealAddress(unittest.TestCase):
    def test_requests_go_to_api_github_com_over_https(self):
        self.assertEqual(str(http.build_request("GET", "rate_limit", TOKEN).url), "https://api.github.com/rate_limit")


class NoAnswer(LoopbackCase):
    def failure(self, timeout=30.0):
        t0 = time.monotonic()
        with self.assertRaises(http.TransportError) as cm:
            http.send("GET", "repos/o/r", TOKEN, timeout=timeout)
        self.assertNotIn(TOKEN, str(cm.exception))
        return cm.exception.kind, time.monotonic() - t0

    def test_a_slow_answer_times_out(self):
        self.server.reply = lambda h: (time.sleep(1.5), h.reply(200))
        kind, took = self.failure(timeout=0.3)
        self.assertEqual(kind, "timeout")
        self.assertLess(took, 1.2)

    def test_a_dripping_answer_still_ends_at_the_timeout(self):
        # every read succeeds within the socket timeout, so only the overall deadline can stop this
        def drip(h):
            h.send_response(200)
            h.send_header("Content-Length", "40")
            h.end_headers()
            try:
                for _ in range(40):
                    h.wfile.write(b"x")
                    h.wfile.flush()
                    time.sleep(0.1)
            except OSError:
                pass
        self.server.reply = drip
        kind, took = self.failure(timeout=0.5)
        self.assertEqual(kind, "timeout")
        self.assertLess(took, 1.5)

    def test_a_refused_connection(self):
        self.server.shutdown()
        self.server.server_close()
        kind, _ = self.failure()
        self.assertEqual(kind, "error")

    def test_a_cut_off_body_is_not_an_answer(self):
        # the connection closes after 1000 of the 5036 bytes the headers promised
        def cut_off(status):
            def reply(h):
                h.send_response(status)
                h.send_header("Content-Length", "5036")
                h.end_headers()
                h.wfile.write(b"x" * 1000)
            return reply
        for status in (200, 404):
            self.server.reply = cut_off(status)
            with self.assertRaises(http.TransportError) as cm:
                http.send("GET", "repos/o/r", TOKEN)
            self.assertEqual(cm.exception.kind, "error")

    def test_the_timeout_is_a_positive_number_of_at_most_an_hour(self):
        for bad in (float("nan"), float("inf"), 0, -1, None, "5", True, 3601, 1e10):
            with self.assertRaises(ValueError, msg=repr(bad)):
                http.send("GET", "repos/o/r", TOKEN, timeout=bad)
        self.assertEqual(self.server.seen, [])

    def test_an_oversized_body(self):
        self.reply_with(200, payload=b"x" * 50)
        with mock.patch.object(http, "MAX_BODY_BYTES", 10):
            kind, _ = self.failure()
        self.assertEqual(kind, "error")
        self.reply_with(200, payload=b"x" * 10)
        with mock.patch.object(http, "MAX_BODY_BYTES", 10):
            self.assertEqual(len(http.send("GET", "repos/o/r", TOKEN).body), 10)


class ContentLength(LoopbackCase):
    """A body is checked against Content-Length only when that header is what frames the body."""

    def raw(self, headers, payload, status=200):
        def reply(h):
            h.send_response(status)
            for name, value in headers:
                h.send_header(name, value)
            h.end_headers()
            h.wfile.write(payload)
        self.server.reply = reply

    def test_a_chunked_answer_is_framed_by_its_chunks(self):
        # HTTP clients ignore a Content-Length sent next to Transfer-Encoding, so this module does too
        for encoding in ("chunked", "Chunked"):
            self.raw([("Transfer-Encoding", encoding), ("Content-Length", "3")], b"5\r\nhello\r\n0\r\n\r\n")
            self.assertEqual(http.send("GET", "repos/o/r", TOKEN).body, b"hello", encoding)

    def test_an_answer_whose_end_cannot_be_checked_is_refused(self):
        # http.client reads these up to the close of the connection, so a cut-off one would look complete
        for headers in ([], [("Transfer-Encoding", "identity"), ("Content-Length", "100")],
                        [("Transfer-Encoding", "gzip"), ("Content-Length", "6")],
                        [("Transfer-Encoding", "gzip, chunked")]):
            self.raw(headers, b'{"a":1')
            with self.assertRaises(http.TransportError, msg=headers) as cm:
                http.send("GET", "repos/o/r", TOKEN)
            self.assertEqual(cm.exception.kind, "error")

    def test_an_answer_that_has_no_body_needs_no_length(self):
        for status in (204, 304):
            self.raw([], b"", status)
            self.assertEqual(http.send("GET", "repos/o/r", TOKEN).status, status)
        self.raw([], b"")
        self.assertEqual(http.send("HEAD", "repos/o/r", TOKEN).status, 200)

    def test_the_same_length_sent_twice_is_one_length(self):
        for headers in ([("Content-Length", "3"), ("Content-Length", "3")], [("Content-Length", "3, 3")]):
            self.raw(headers, b"abc")
            self.assertEqual(http.send("GET", "repos/o/r", TOKEN).body, b"abc", headers)

    def test_a_length_that_is_not_one_number_is_refused(self):
        # the body can't be checked against it, so a cut-off answer would pass as complete
        for headers in ([("Content-Length", "5036"), ("Content-Length", "1000")],
                        [("Content-Length", "1000"), ("Content-Length", "5036")],
                        [("Content-Length", "1000, 5036")], [("Content-Length", "-1")], [("Content-Length", "1e3")]):
            self.raw(headers, b"x" * 1000)
            with self.assertRaises(http.TransportError, msg=headers) as cm:
                http.send("GET", "repos/o/r", TOKEN)
            self.assertEqual(cm.exception.kind, "error")


if __name__ == "__main__":
    unittest.main()
