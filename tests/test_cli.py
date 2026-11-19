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

from _fixtures import SCRIPTS, answer, settings

core, http, throttling = cli.core, cli.http, cli.tollbooth_throttle
TOKEN = "ghx_FAKEtokenFAKEtoken"
RESET = 1_800_000_600
RATE_LIMIT = json.dumps({"resources": {
    "core": {"limit": 5000, "remaining": 4990, "reset": RESET},
    "graphql": {"limit": 5000, "remaining": 4321, "reset": RESET},
    "search": {"limit": 30, "remaining": 0, "reset": RESET},
    "code_search": {"limit": 10, "remaining": 10, "reset": RESET},
    "dependency_sbom": {"limit": 100, "remaining": 100, "reset": RESET}}}).encode()


class InferBucket(unittest.TestCase):
    def test_buckets(self):
        cases = {
            "graphql": "graphql",
            "search/code?q=x": "code_search",
            "search/repositories?q=x": "search",
            "search/issues": "search",
            "repos/a/b": "core",
            "/repos/a/b": "core",
            "rate_limit": "core",
            "": "core",
            "SEARCH/repositories?q=x": "search",  # GitHub routes paths case-insensitively
            "Search/Code": "code_search",
            "GraphQL": "graphql",
        }
        for path, want in cases.items():
            self.assertEqual(core.infer_bucket(path), want, path)


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
