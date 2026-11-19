"""CLI tests for tollbooth_cli. Click's CliRunner runs the commands in this process; GitHub and `gh` are
never contacted. The last classes start real processes, through scripts/gh-tollbooth too.
Run: uv run pytest
"""
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import tollbooth_cli as cli
from click.testing import CliRunner

from _fixtures import SCRIPTS, answer, primary, settings

core, http, throttling = cli.core, cli.http, cli.tollbooth_throttle
TOKEN = "ghx_FAKEtokenFAKEtoken"
RESET = 1_800_000_600
RATE_LIMIT = json.dumps({"resources": {
    "core": {"limit": 5000, "remaining": 4990, "reset": RESET},
    "graphql": {"limit": 5000, "remaining": 4321, "reset": RESET},
    "search": {"limit": 30, "remaining": 0, "reset": RESET},
    "code_search": {"limit": 10, "remaining": 10, "reset": RESET},
    "dependency_sbom": {"limit": 100, "remaining": 100, "reset": RESET}}}).encode()


def fake(value):
    """A mock that raises `value` if it is an exception, and returns it otherwise."""
    return mock.Mock(side_effect=value) if isinstance(value, Exception) else mock.Mock(return_value=value)


class CliCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name) / "state"
        self.use_state_dir(self.state)

    def use_state_dir(self, folder: Path):
        p = mock.patch.object(core, "SETTINGS", settings(state_dir=folder, deadline=100))
        p.start()
        self.addCleanup(p.stop)

    def main(self, argv, send=None, token=TOKEN, env=None):
        """Run the CLI with a fake token and a fake GitHub. Returns (exit code, stdout, stderr).

        TOLLBOOTH_AS is cleared unless `env` sets it, so this machine's value can't change a result."""
        send = send if send is not None else answer(200, body=RATE_LIMIT)
        with mock.patch.object(http, "send", fake(send)) as self.send, mock.patch.object(http, "token", fake(token)):
            result = CliRunner().invoke(cli.cli, argv, env={"TOLLBOOTH_AS": None, **(env or {})},
                                        prog_name="gh-tollbooth")
        if result.exception is not None and not isinstance(result.exception, SystemExit):
            raise result.exception
        self.assertNotIn(TOKEN, result.stdout + result.stderr)
        return result.exit_code, result.stdout, result.stderr


class TestSettings(CliCase):
    """A CLI test runs with the settings it sets and the defaults, never this machine's TOLLBOOTH_* values."""

    def test_this_machines_values_dont_leak_in(self):
        with mock.patch.dict(os.environ, {"TOLLBOOTH_MAX_WAIT": "5", "TOLLBOOTH_DEADLINE": "7"}):
            self.use_state_dir(self.state)
        self.assertEqual((core.SETTINGS.max_wait, core.SETTINGS.deadline), (core.DEFAULT_MAX_WAIT, 100))


class Status(CliCase):
    def test_shows_the_four_buckets(self):
        code, out, _ = self.main(["status"])
        self.assertEqual(code, 0)
        rows = {ln.split()[0]: ln.split()[1] for ln in out.splitlines()[1:5]}
        self.assertEqual(rows, {"graphql": "4321/5000", "core": "4990/5000", "search": "0/30", "code_search": "10/10"})
        self.assertNotIn("dependency_sbom", out)

    def test_reads_exactly_the_tracked_buckets(self):
        self.assertEqual(tuple(cli._Resources.model_fields), throttling.BUCKETS)

    def test_is_one_free_request_outside_the_throttle(self):
        self.main(["--as", "lab", "status"])
        self.send.assert_called_once_with("GET", "rate_limit", TOKEN, timeout=30)
        throttle = throttling.Throttle(self.state)
        self.assertEqual((throttle.used("lab"), throttle.entries()), (0, []))

    def test_flags_a_bucket_another_agent_blocked(self):
        throttling.Throttle(self.state)._mark("search", {"search": time.time() + 120})
        _, out, _ = self.main(["status"])
        flagged = [ln.split()[0] for ln in out.splitlines() if "BLOCKED until" in ln]
        self.assertEqual(flagged, ["search"])

    def test_lists_recent_calls_by_label(self):
        throttle = throttling.Throttle(self.state)
        for i in range(3):
            throttle._log("scout", "search", 200, 0.4, f"GET search/repositories?q=term{i}")
        throttle._log("other", "graphql", 502, 9.9, "POST graphql")
        _, out, _ = self.main(["status", "--last", "2"])
        self.assertIn("calls in the last hour: 4", out)
        self.assertRegex(out, r"3\s+search\s+scout")
        self.assertIn("term2", out)
        self.assertIn("http=502", out)
        self.assertNotIn("term0", out)  # --last 2 shows the two newest calls

    def test_last_zero_lists_no_calls_and_a_negative_count_is_refused(self):
        throttle = throttling.Throttle(self.state)
        for i in range(3):
            throttle._log("scout", "search", 200, 0.4, f"GET search/repositories?q=term{i}")
        _, out, _ = self.main(["status", "--last", "0"])
        self.assertIn("calls in the last hour: 3", out)
        self.assertNotIn("term", out)
        code, _, _ = self.main(["status", "--last", "-2"])
        self.assertEqual(code, core.EX_USAGE)

    def test_control_characters_are_never_printed(self):
        throttling.Throttle(self.state)._log("scout", "core", 200, 0.1, "GET q=\x1b[2J\x07")
        doc = json.loads(RATE_LIMIT)
        doc["resources"]["core"].update(limit="\x1b[31m5000", remaining="\x1b]0;x\x07")
        code, out, _ = self.main(["status"], send=answer(200, body=json.dumps(doc).encode()))
        self.assertEqual(code, 0)
        self.assertNotRegex(out, r"[\x00-\x09\x0b-\x1f\x7f]")
        self.assertRegex(out, r"core\s+\?/\?")  # only numbers are shown from GitHub's answer

    def test_github_saying_no(self):
        code, out, err = self.main(["status"], send=answer(401, body=b'{"message": "Bad credentials"}'))
        self.assertEqual((code, out), (core.EX_FAIL, ""))
        self.assertIn("HTTP 401", err)
        self.assertIn("gh auth login", err)
        code, _, err = self.main(["status"], send=answer(503))
        self.assertEqual(code, core.EX_FAIL)
        self.assertIn("HTTP 503", err)

    def test_an_answer_that_is_not_the_rate_limit_document(self):
        for body in (b"<html>", b"[]", b'{"resources": 5}', b'{"resources": {"core": {"reset": "soon"}}}',
                     b"\xff\xfe", b"[" * 100_000):
            code, out, _ = self.main(["status"], send=answer(200, body=body))
            self.assertEqual((code, out), (core.EX_FAIL, ""), body)

    def test_no_token_and_no_answer(self):
        code, _, err = self.main(["status"], token=http.NoToken("no GitHub token: run `gh auth login`"))
        self.assertEqual(code, core.EX_FAIL)
        self.assertIn("gh auth login", err)
        self.send.assert_not_called()
        code, _, err = self.main(["status"], send=http.TransportError("timeout", "no answer in 30 s"))
        self.assertEqual(code, core.EX_FAIL)
        self.assertIn("no answer in 30 s", err)

    def test_logged_text_this_locale_cannot_print_is_shown_with_question_marks(self):
        throttling.Throttle(self.state)._log("scout", "core", 200, 0.1, "GET café ☃")
        raw = io.BytesIO()
        out = io.TextIOWrapper(raw, encoding="latin-1")  # strict, as under LANG=en_US.ISO8859-1
        with mock.patch.object(http, "send", return_value=answer(200, body=RATE_LIMIT)), \
                mock.patch.object(http, "token", return_value=TOKEN), mock.patch.dict(os.environ, {"TOLLBOOTH_AS": ""}), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = cli.main(["status"])
        out.flush()
        self.assertEqual(code, 0)
        self.assertIn(b"GET caf\xe9 ?", raw.getvalue())


class GlobalOptions(CliCase):
    def test_labels_are_validated(self):
        for bad in ("bad\nlabel", "a\tb", "a b", "x" * 41, "a/b"):
            code, _, _ = self.main(["--as", bad, "status"])
            self.assertEqual(code, core.EX_USAGE, bad)
            self.send.assert_not_called()
        self.assertEqual(self.main(["--as", "run-1.ok_x", "status"])[0], 0)

    def test_a_label_can_come_from_the_environment_or_after_the_command(self):
        self.assertEqual(self.main(["status"], env={"TOLLBOOTH_AS": "bad label"})[0], core.EX_USAGE)
        self.assertEqual(self.main(["status", "--as", "fine"], env={"TOLLBOOTH_AS": "bad label"})[0], 0)

    def test_a_budget_needs_a_label(self):
        code, _, err = self.main(["--budget", "3", "status"])
        self.assertEqual(code, core.EX_USAGE)
        self.assertIn("--as", err)
        self.assertEqual(self.main(["--as", "lab", "--budget", "3", "status"])[0], 0)
        self.assertEqual(self.main(["status", "--as", "lab", "--budget", "3"])[0], 0)

    def test_abbreviated_options_are_refused(self):
        for argv in (["--a", "lab", "status"], ["--as", "lab", "--bud", "3", "status"], ["status", "--la", "2"],
                     ["call", "--as", "lab", "--no-c", "--", "api", "repos/o/r"]):
            self.assertEqual(self.main(argv)[0], core.EX_USAGE, argv)
            self.send.assert_not_called()


class Call(CliCase):
    """`call -- api ...`: one read-only request through the throttle, its answer on stdout byte for byte."""

    def setUp(self):
        super().setUp()
        p = mock.patch.dict(throttling.SPACING, {k: 0 for k in throttling.SPACING})  # the real clock runs here
        p.start()
        self.addCleanup(p.stop)

    def call(self, argv, send=None):
        """Run the CLI with a fake token and a fake GitHub. Returns (exit code, stdout bytes, stderr)."""
        send = send if send is not None else answer(200, body=b'{"ok": 1}')
        with mock.patch.object(http, "send", fake(send)) as self.send, mock.patch.object(http, "token", fake(TOKEN)):
            result = CliRunner().invoke(cli.cli, argv, env={"TOLLBOOTH_AS": None}, prog_name="gh-tollbooth")
        if result.exception is not None and not isinstance(result.exception, SystemExit):
            raise result.exception
        self.assertNotIn(TOKEN.encode(), result.stdout_bytes)
        self.assertNotIn(TOKEN, result.stderr)
        return result.exit_code, result.stdout_bytes, result.stderr

    def test_prints_githubs_answer_byte_for_byte(self):
        body = b"line one\r\nnot utf-8: \xff\xfe\x00 end"
        code, out, err = self.call(["call", "--", "api", "repos/o/r/contents/f", "-H",
                                    "Accept: application/vnd.github.raw"], send=answer(200, body=body))
        self.assertEqual((code, out, err), (0, body, ""))
        args, kwargs = self.send.call_args
        self.assertEqual((args, kwargs["accept"], kwargs["json_body"]),
                         (("GET", "repos/o/r/contents/f", TOKEN), "application/vnd.github.raw", None))

    def test_an_http_error_prints_the_body_and_exits_1(self):
        for status, reason in ((404, "Not Found"), (401, "Unauthorized"), (301, "Moved Permanently")):
            body = b'{"message": "nope"}'
            code, out, err = self.call(["call", "--", "api", "repos/o/r"], send=answer(status, body=body))
            self.assertEqual((code, out), (core.EX_FAIL, body), status)
            self.assertEqual(err, f"gh-tollbooth: HTTP {status} {reason}\n")

    def test_include_prints_the_status_line_and_headers_first(self):
        reply = answer(200, {"X-RateLimit-Remaining": "4999", "Content-Type": "application/json", "X-Odd": "a\x1bb"},
                       b"{}")
        code, out, _ = self.call(["call", "--", "api", "-i", "rate_limit"], send=reply)
        self.assertEqual((code, out), (0, b"HTTP/1.1 200 OK\nX-Ratelimit-Remaining: 4999\n"
                                          b"Content-Type: application/json\nX-Odd: a?b\n\n{}"))

    def test_include_marks_every_hidden_character_in_a_header(self):
        reply = answer(200, {"X-Odd": "a\u202eb\u2764\ufe0fc"}, b"{}")
        _, out, _ = self.call(["call", "--", "api", "-i", "rate_limit"], send=reply)
        self.assertIn("X-Odd: a?b\u2764?c\n".encode(), out)

    def test_no_hidden_character_reaches_stderr(self):
        code, _, err = self.call(["call", "--", "api", "search/repositories", "-f", "q=\u2764\ufe0f"])
        self.assertEqual(code, core.EX_USAGE)
        self.assertIn("hidden characters", err)
        self.assertNotIn("\ufe0f", err)  # the argument is quoted back, the selector as "?"
        code, _, err = self.call(["call", "--", "api", "repos/o/r"],
                                 send=primary(time.time() + 3000, resource="evil\x1b]0;x\x07"))
        self.assertEqual(code, core.EX_LIMITED)
        self.assertIn("an untracked resource is rate-limited until", err)
        self.assertNotIn("\x1b", err)

    def test_silent_prints_nothing_and_still_exits_by_status(self):
        self.assertEqual(self.call(["call", "--", "api", "--silent", "repos/o/r"])[:2], (0, b""))
        code, out, err = self.call(["call", "--", "api", "--silent", "repos/o/r"], send=answer(404, body=b"{}"))
        self.assertEqual((code, out), (core.EX_FAIL, b""))
        self.assertIn("HTTP 404", err)

    def test_a_refused_request_exits_2_with_its_hint_and_costs_nothing(self):
        for args, hint in ((["api", "repos/o/r", "--jq", ".name"], "| jq"),
                           (["api", "repos/o/r", "--cache", "1h"], "live, charged request"),
                           (["api", "graphql", "-f", "query=mutation { x }"], "only GraphQL query"),
                           (["issue", "list"], "API form")):
            code, out, err = self.call(["--as", "lab", "call", "--"] + args)
            self.assertEqual((code, out), (core.EX_USAGE, b""), args)
            self.assertIn(hint, err)
            self.send.assert_not_called()
        throttle = throttling.Throttle(self.state)
        self.assertEqual((throttle.used("lab"), throttle.entries()), (0, []))

    def test_a_graphql_query_is_posted_with_its_variables(self):
        doc = 'query($n: Int!) { search(query: "x", type: REPOSITORY, first: $n) { repositoryCount } }'
        self.assertEqual(self.call(["call", "--", "api", "graphql", "-f", f"query={doc}", "-F", "n=3"])[0], 0)
        args, kwargs = self.send.call_args
        self.assertEqual((args, kwargs["json_body"]),
                         (("POST", "graphql", TOKEN), {"query": doc, "variables": {"n": 3}}))

    def test_rest_fields_and_head(self):
        self.call(["call", "--", "api", "search/code", "-f", "q=x", "-F", "per_page=5"])
        self.assertEqual(self.send.call_args.args, ("GET", "search/code?q=x&per_page=5", TOKEN))
        code, out, _ = self.call(["call", "api", "-X", "HEAD", "repos/o/r"], send=answer(200, body=b""))
        self.assertEqual((code, out), (0, b""))  # without `--` too
        self.assertEqual(self.send.call_args.args, ("HEAD", "repos/o/r", TOKEN))

    def test_as_and_budget_charge_the_label(self):
        argv = ["call", "--as", "lab", "--budget", "1", "--no-cache", "--", "api", "repos/o/r"]
        self.assertEqual(self.call(argv)[0], 0)
        code, out, err = self.call(argv)
        self.assertEqual((code, out), (core.EX_BUDGET, b""))
        self.assertIn("budget of 1", err)
        self.send.assert_not_called()
        self.assertEqual(throttling.Throttle(self.state).used("lab"), 1)

    def test_a_rate_limit_keeps_its_exit_code_and_prints_githubs_answer(self):
        far = answer(403, {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": int(time.time() + 3000)},
                     b'{"message": "API rate limit exceeded"}')
        code, out, err = self.call(["call", "--", "api", "repos/o/r"], send=far)
        self.assertEqual((code, out), (core.EX_LIMITED, far.body))
        self.assertIn("rate-limited until", err)

    def test_a_502_or_504_exits_3_and_prints_githubs_answer(self):
        for status in (502, 504):
            body = b'{"message": "Server Error"}'
            code, out, err = self.call(["call", "--", "api", "repos/o/r"], send=answer(status, body=body))
            self.assertEqual((code, out), (core.EX_HEAVY, body), status)
            self.assertIn("502/504", err)
        code, out, _ = self.call(["call", "--", "api", "--silent", "repos/o/r"], send=answer(502, body=b"x"))
        self.assertEqual((code, out), (core.EX_HEAVY, b""))

    def test_a_closed_stdout_is_refused_before_anything_is_sent(self):
        err = io.StringIO()
        for argv, code_wanted, sends in ((["--as", "lab", "call", "--", "api", "repos/o/r"], core.EX_USAGE, 0),
                                         (["--as", "lab", "call", "--", "api", "--silent", "repos/o/r"], 0, 1)):
            with mock.patch.object(http, "send", fake(answer(200, body=b"{}"))) as send, \
                    mock.patch.object(http, "token", fake(TOKEN)), \
                    contextlib.redirect_stdout(None), contextlib.redirect_stderr(err):  # like `>&-`
                code = cli.main(argv)
            self.assertEqual((code, send.call_count), (code_wanted, sends), argv)
        self.assertIn("stdout is closed", err.getvalue())
        self.assertEqual(throttling.Throttle(self.state).used("lab"), 1)  # only the --silent call

    def test_status_works_with_a_spent_budget(self):
        self.assertEqual(self.main(["--as", "lab", "--budget", "0", "status"])[0], 0)


class Sweep(CliCase):
    """`sweep QUERIES.tsv`: rows on stdout, progress on stderr, the exit code says whether they're complete."""

    def setUp(self):
        super().setUp()
        p = mock.patch.dict(throttling.SPACING, {k: 0 for k in throttling.SPACING})  # the real clock runs here
        p.start()
        self.addCleanup(p.stop)
        self.queries = Path(self.tmp.name) / "q.tsv"
        self.queries.write_text("A\tone\nB\ttwo\n")

    @staticmethod
    def found(q1=None):
        data = {"q0": {"repositoryCount": 1, "nodes": [{"nameWithOwner": "o/one", "stargazerCount": 3}]},
                "q1": q1 if q1 is not None else {"repositoryCount": 1, "nodes": [{"nameWithOwner": "o/two"}]},
                "rateLimit": {"cost": 1, "remaining": 4999}}
        return answer(200, body=json.dumps({"data": data}).encode())

    def test_rows_on_stdout_and_progress_on_stderr(self):
        with self.assertLogs("gh-tollbooth", "INFO") as notices:
            code, out, _ = self.main(["--as", "lab", "sweep", str(self.queries), "--no-cache"], send=self.found())
        rows = [json.loads(ln) for ln in out.splitlines()]
        self.assertEqual((code, [(r["slice"], r["nameWithOwner"]) for r in rows]),
                         (0, [("A", "o/one"), ("B", "o/two")]))
        self.assertIn("PASS: 2 queries, 2 rows", "\n".join(notices.output))
        self.assertEqual(self.send.call_count, 1)
        self.assertEqual(throttling.Throttle(self.state).used("lab"), 1)

    def test_an_incomplete_answer_exits_3_with_the_rows_it_got(self):
        code, out, err = self.main(["sweep", str(self.queries)], send=self.found(q1={"nodes": [None]}))
        self.assertEqual((code, len(out.splitlines())), (core.EX_HEAVY, 1))
        self.assertIn("slices: B", err)

    def test_a_refused_file_or_option_exits_2_and_costs_nothing(self):
        bad = Path(self.tmp.name) / "bad.tsv"
        bad.write_text("A\tx\x00y\n")
        for argv in (["sweep", str(bad)], ["sweep", str(Path(self.tmp.name) / "missing.tsv")],
                     ["sweep", str(self.queries), "--first", "0"], ["sweep", str(self.queries), "--first", "101"],
                     ["sweep", str(self.queries), "--fir", "5"], ["sweep"]):
            code, out, _ = self.main(["--as", "lab"] + argv, send=self.found())
            self.assertEqual((code, out), (core.EX_USAGE, ""), argv)
        self.send.assert_not_called()
        self.assertEqual(throttling.Throttle(self.state).used("lab"), 0)

    def test_a_closed_stdout_is_refused_before_anything_is_sent(self):
        err = io.StringIO()
        with mock.patch.object(http, "send", fake(self.found())) as send, \
                mock.patch.object(http, "token", fake(TOKEN)), \
                contextlib.redirect_stdout(None), contextlib.redirect_stderr(err):  # like `>&-`
            code = cli.main(["sweep", str(self.queries)])
        self.assertEqual((code, send.call_count), (core.EX_USAGE, 0))
        self.assertIn("stdout is closed", err.getvalue())


class StateFolder(CliCase):
    def test_a_folder_that_cannot_be_created_is_one_line_not_a_traceback(self):
        a_file = Path(self.tmp.name) / "file"
        a_file.write_text("")
        for folder in (a_file, a_file / "state"):  # the path is a file; a parent of the path is a file
            self.use_state_dir(folder)
            code, out, err = self.main(["status"])
            self.assertEqual((code, out, len(err.splitlines())), (core.EX_FAIL, "", 1), err)
            self.assertIn(str(folder), err)
            self.assertIn("TOLLBOOTH_STATE_DIR", err)
            self.send.assert_not_called()


    @unittest.skipIf(os.geteuid() == 0, "root can open a file without permission")
    def test_a_lock_file_only_root_can_open_is_one_line_not_a_traceback(self):
        self.state.mkdir()
        (self.state / "lock").write_text("")
        (self.state / "lock").chmod(0)
        self.addCleanup((self.state / "lock").chmod, 0o644)
        code, out, err = self.main(["call", "--", "api", "repos/o/r"])
        self.assertEqual((code, out, len(err.splitlines())), (core.EX_FAIL, "", 1), err)
        self.assertIn("TOLLBOOTH_STATE_DIR", err)
        self.send.assert_not_called()


class Bugs(CliCase):
    """A bug's exception is still one line on stderr and exit 1, never a traceback."""

    def test_a_bug_is_one_line_saying_internal_error(self):
        with mock.patch.object(cli.Caller, "fetch", side_effect=ValueError("bad\x1bthing")):
            code, out, err = self.main(["call", "--", "api", "repos/o/r"])
        self.assertEqual((code, out), (core.EX_FAIL, ""))
        self.assertEqual(err, "gh-tollbooth: internal error (ValueError: bad?thing); please report it\n")

    def test_its_message_is_cut(self):
        with mock.patch.object(cli.Caller, "fetch", side_effect=KeyError("x" * 500)):
            _, _, err = self.main(["call", "--", "api", "repos/o/r"])
        self.assertIn("x" * 190, err)
        self.assertLess(len(err), 300)

    def test_click_still_exits_its_own_way(self):
        code, _, err = self.main(["status", "--last", "-1"])
        self.assertEqual(code, 2)
        self.assertNotIn("internal error", err)


class NeverATraceback(unittest.TestCase):
    """Real processes: these failures happen outside what a test can hand to the command."""

    # `status` with a fake token and a fake GitHub, writing to the real stdout
    CHILD = """
import sys
from unittest import mock
sys.path[:0] = [sys.argv[1], sys.argv[2]]
from _fixtures import answer
import tollbooth_cli as cli
with mock.patch.object(cli.http, "send", return_value=answer(200, body=sys.argv[3].encode())), \\
        mock.patch.object(cli.http, "token", return_value="ghx_FAKEtokenFAKEtoken"):
    sys.exit(cli.main(["status"]))
"""

    # `call` with a fake token and a GitHub that answers with a rate limit longer than TOLLBOOTH_MAX_WAIT
    CHILD_LIMITED = """
import sys, time
from unittest import mock
sys.path[:0] = [sys.argv[1], sys.argv[2]]
from _fixtures import primary
import tollbooth_cli as cli
with mock.patch.object(cli.http, "send", return_value=primary(time.time() + 3000)), \\
        mock.patch.object(cli.http, "token", return_value="ghx_FAKEtokenFAKEtoken"):
    sys.exit(cli.main(["call", "--", "api", "repos/o/r"]))
"""

    def env(self, folder: str, **extra) -> dict:
        env = {k: v for k, v in os.environ.items() if not k.startswith("TOLLBOOTH_")}
        return dict(env, TOLLBOOTH_STATE_DIR=str(Path(folder) / "state"), **extra)

    def test_a_reader_that_closes_the_pipe(self):
        # `gh-tollbooth status | head -1`
        with tempfile.TemporaryDirectory() as d:
            p = subprocess.Popen([sys.executable, "-c", self.CHILD, str(SCRIPTS), str(Path(__file__).parent),
                                  RATE_LIMIT.decode()], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                 env=self.env(d))
            assert p.stdout is not None and p.stderr is not None  # PIPE sets both; tells the type checker
            p.stdout.close()  # before the child has written anything
            err = p.stderr.read()
            p.stderr.close()
            self.assertEqual((p.wait(), err), (0, ""))

    def test_a_reader_that_closes_the_pipe_still_gets_the_stop_line(self):
        # `gh-tollbooth call -- api ... | jq 'bad['`: jq exits at once, and GitHub said "rate-limited"
        with tempfile.TemporaryDirectory() as d:
            p = subprocess.Popen([sys.executable, "-c", self.CHILD_LIMITED, str(SCRIPTS), str(Path(__file__).parent)],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=self.env(d))
            assert p.stdout is not None and p.stderr is not None
            p.stdout.close()  # before the child has written anything
            err = p.stderr.read()
            p.stderr.close()
            self.assertEqual(p.wait(), core.EX_LIMITED, err)
            self.assertRegex(err, r"^gh-tollbooth: core is rate-limited until \d\d:\d\d:\d\d\. Stop and report")

    def test_a_deadline_that_is_not_a_number(self):
        with tempfile.TemporaryDirectory() as d:
            p = subprocess.run([sys.executable, "-I", str(SCRIPTS / "tollbooth_cli.py"), "--help"],
                               capture_output=True, text=True, env=self.env(d, TOLLBOOTH_DEADLINE="150.5"))
            self.assertEqual((p.returncode, len(p.stderr.splitlines())), (0, 1), p.stderr[-300:])
            self.assertIn("TOLLBOOTH_DEADLINE", p.stderr)


class Launcher(unittest.TestCase):
    """scripts/gh-tollbooth runs the CLI through uv, in isolated mode, so PYTHONPATH can't put code in it."""

    def test_it_runs_the_cli_with_the_locked_dependencies_in_isolated_mode(self):
        text = (SCRIPTS / "gh-tollbooth").read_text()
        self.assertIn('uv run --quiet --locked --project "$root" python -I "$root/scripts/tollbooth_cli.py" "$@"', text)
        self.assertTrue(os.access(SCRIPTS / "gh-tollbooth", os.X_OK))

    @unittest.skipUnless(shutil.which("uv"), "uv is not on PATH")
    def test_pythonpath_cannot_replace_a_module(self):
        with tempfile.TemporaryDirectory() as d:
            marker = Path(d) / "ran"
            (Path(d) / "click.py").write_text(f"open({str(marker)!r}, 'w').close()\nraise SystemExit(99)\n")
            (Path(d) / "sitecustomize.py").write_text(f"open({str(marker)!r}, 'w').close()\n")
            env = {k: v for k, v in os.environ.items() if not k.startswith(("TOLLBOOTH_", "VIRTUAL_ENV"))}
            env.update(PYTHONPATH=d, PYTHONSTARTUP=str(Path(d) / "click.py"), TOLLBOOTH_STATE_DIR=str(Path(d) / "s"))
            p = subprocess.run([str(SCRIPTS / "gh-tollbooth"), "--help"], capture_output=True, text=True, env=env, cwd=d)
            self.assertEqual((p.returncode, marker.exists()), (0, False), p.stderr[-300:])
            self.assertIn("Usage: gh-tollbooth", p.stdout)


if __name__ == "__main__":
    unittest.main()
