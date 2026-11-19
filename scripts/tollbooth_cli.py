"""tollbooth_cli: the gh-tollbooth command line, parsed by Click. Agents run it through scripts/gh-tollbooth.

Results go to stdout; progress and errors go to stderr, one "gh-tollbooth: ..." line each, never a traceback.

Trust boundary: an agent may choose the arguments, never the environment. Python runs in isolated mode
(-I), so PYTHONPATH can't put code into this process. The environment still picks uv and its Python
(PATH), the token (GH_TOKEN) and a proxy that sees the token (HTTPS_PROXY with SSL_CERT_FILE), so don't
let agents put variable assignments in front of the command.
"""
from __future__ import annotations

import datetime as dt
import io
import os
import re
import sys
from collections import Counter
from contextlib import suppress
from http.client import responses as REASONS
from pathlib import Path
from typing import Annotated, NoReturn

sys.path.insert(0, str(Path(__file__).resolve().parent))  # isolated mode (-I) leaves this folder off the path

import click  # noqa: E402
from pydantic import BaseModel, BeforeValidator, Field, ValidationError  # noqa: E402

import tollbooth_core as core  # noqa: E402
import tollbooth_http as http  # noqa: E402
import tollbooth_throttle  # noqa: E402
from tollbooth_guard import CallRequest, parse_call  # noqa: E402
from tollbooth_loop import Caller  # noqa: E402

LABEL_RE = re.compile(r"[A-Za-z0-9_.-]{1,40}")  # always use .fullmatch: "$" would accept a trailing newline
MAX_RESET = 10 ** 11  # seconds; a reset time later than this is not a real Unix time
CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")  # never printed from a header: it could drive the terminal


# ---------- GitHub's rate_limit answer ----------

def _count_or_none(value: object) -> object:
    """A count from GitHub's answer, or None for anything else: text could carry terminal codes."""
    return value if type(value) is int else None


Count = Annotated["int | None", BeforeValidator(_count_or_none)]


class _Bucket(BaseModel):
    limit: Count = None
    remaining: Count = None
    reset: Annotated[int, Field(ge=0, le=MAX_RESET, strict=True)] = 0  # Unix time


class _Resources(BaseModel):
    graphql: _Bucket = Field(default_factory=_Bucket)
    core: _Bucket = Field(default_factory=_Bucket)
    search: _Bucket = Field(default_factory=_Bucket)
    code_search: _Bucket = Field(default_factory=_Bucket)


class _RateLimit(BaseModel):
    """The part of GitHub's GET rate_limit answer that `status` shows: the tracked buckets (tollbooth_throttle.BUCKETS,
    in that order). Other buckets are ignored."""

    resources: _Resources


# ---------- what every command shares ----------

def _clock(at: float) -> str:
    """A wall-clock time as HH:MM:SS, local time."""
    return dt.datetime.fromtimestamp(at).strftime("%H:%M:%S")


def _shown(count: int | None) -> str:
    return "?" if count is None else str(count)


def _start(ctx: click.Context, label: str | None, budget: int | None) -> Caller:
    """Merge --as and --budget given before and after the command, check them, and open the state folder."""
    who: str = label or ctx.obj["label"] or "anon"
    limit: int | None = budget if budget is not None else ctx.obj["budget"]
    if not LABEL_RE.fullmatch(who):
        raise click.UsageError("--as LABEL may use only letters, digits, '.', '_' and '-' (max 40)")
    if limit is not None and who == "anon":
        raise click.UsageError("--budget needs a unique --as LABEL (calls are counted per label)")
    folder = core.SETTINGS.state_dir
    try:
        throttle = tollbooth_throttle.Throttle(folder)
    except OSError as e:
        raise core.TollboothError(f"can't create the state folder {folder} ({e.strerror or e}); "
                                  "set TOLLBOOTH_STATE_DIR to a folder you can write to", core.EX_FAIL) from None
    return Caller(who, limit, throttle)


def _reader_left() -> None:
    """The reader closed the pipe (`| head`). Not an error: stdout goes nowhere, so the flush at exit is quiet."""
    with suppress(OSError, ValueError):
        fd = sys.stdout.fileno()
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, fd)
        os.close(devnull)


def _fail(ctx: click.Context, message: str, code: int) -> NoReturn:
    click.echo(f"gh-tollbooth: {message}", err=True)
    ctx.exit(code)


class _Guarded(click.Group):
    """Every failure is one line on stderr and an exit code, never a traceback."""

    def invoke(self, ctx: click.Context):
        try:
            result = super().invoke(ctx)
            if sys.stdout is not None:  # None when stdout was closed (`>&-`)
                sys.stdout.flush()  # a closed pipe shows here when the output was small
            return result
        except BrokenPipeError:
            _reader_left()
            ctx.exit(0)
        except core.TollboothError as e:
            _fail(ctx, str(e), e.code)
        except http.NoToken as e:
            _fail(ctx, str(e), core.EX_FAIL)
        except http.TransportError as e:
            _fail(ctx, f"no answer from GitHub ({e})", core.EX_FAIL)
        except KeyboardInterrupt:
            ctx.exit(130)


def _global_options(command):
    """--as and --budget after the command too; a value given there wins over one given before it."""
    command = click.option("--budget", type=int, metavar="N",
                           help="Cooperative cap on GitHub calls for --as LABEL in 24 h.")(command)
    return click.option("--as", "label", metavar="LABEL", help="Tag calls in the log (default: TOLLBOOTH_AS).")(command)


@click.group(cls=_Guarded, context_settings={"help_option_names": ["-h", "--help"]})
@click.option("--as", "label", envvar="TOLLBOOTH_AS", metavar="LABEL",
              help="Tag calls in the log (default: TOLLBOOTH_AS, else anon).")
@click.option("--budget", type=int, metavar="N", help="Cooperative cap on GitHub calls for --as LABEL in 24 h.")
@click.pass_context
def cli(ctx: click.Context, label: str | None, budget: int | None) -> None:
    """gh-tollbooth: one polite, shared, read-only request stream to GitHub for every agent on a machine.

    It talks to the GitHub API directly, with the login `gh` already has (or GH_TOKEN).

    --budget caps the GitHub calls LABEL makes in 24 h. It is cooperative: it holds while every command
    passes it, and can't stop an agent that drops it, picks another label or calls GitHub another way.

    \b
    Exit codes: 0 ok; 1 no token, no answer from GitHub, an HTTP error answer or no state folder;
    2 usage or a request gh-tollbooth refuses; 3 GitHub gave up on the request; 4 budget used up;
    75 GitHub rate limit (stop and report the time); 76 busy or out of time (retry once later).
    """
    ctx.obj = {"label": label, "budget": budget}


@cli.command()
@_global_options
@click.option("--last", type=click.IntRange(min=0), default=8, show_default=True, help="Recent calls to list.")
@click.pass_context
def status(ctx: click.Context, label: str | None, budget: int | None, last: int) -> None:
    """Rate-limit buckets and recent calls. Free: GitHub doesn't count it."""
    caller = _start(ctx, label, budget)
    # Asked directly, not through the throttle: GitHub doesn't count rate_limit against any bucket,
    # and status is what you run while buckets are blocked.
    resp = http.send("GET", "rate_limit", caller.token(), timeout=30)
    if resp.status != 200:
        hint = " (GitHub rejected the token: run `gh auth login`, or check GH_TOKEN)" if resp.status == 401 else ""
        raise core.TollboothError(f"GET rate_limit answered HTTP {resp.status}{hint}", core.EX_FAIL)
    try:
        resources = _RateLimit.model_validate_json(resp.body).resources
    except ValidationError:
        raise core.TollboothError("GET rate_limit: the answer is not GitHub's rate-limit document",
                                  core.EX_FAIL) from None
    click.echo("bucket        remaining/limit  resets")
    for name in tollbooth_throttle.BUCKETS:
        b: _Bucket = getattr(resources, name)
        left = caller.throttle.blocked_for(name)
        blocked = f"  BLOCKED until {_clock(caller.throttle.clock.now() + left)}" if left > 0 else ""
        click.echo(f"{name:13} {_shown(b.remaining):>6}/{_shown(b.limit):<6}  {_clock(b.reset)}{blocked}")
    recent = caller.throttle.entries(since=caller.throttle.clock.now() - 3600)
    click.echo(f"\ngh-tollbooth calls in the last hour: {len(recent)}")
    for (who, bucket), n in Counter((c.label, c.bucket) for c in recent).most_common(15):
        click.echo(f"  {n:4}  {bucket:12} {who}")
    for c in recent[-last:] if last else []:
        click.echo(f"  {_clock(c.at)}  {c.label:<16} {c.bucket:<12} http={c.status:<3} {c.secs:>5.1f}s  "
                   f"{c.summary[:70]}")


@cli.command(context_settings={"allow_interspersed_args": False})
@_global_options
@click.option("--no-cache", is_flag=True, help="Accepted and ignored: gh-tollbooth doesn't cache.")
@click.argument("gh_args", nargs=-1, type=click.UNPROCESSED, metavar="-- api ENDPOINT [OPTIONS]")
@click.pass_context
def call(ctx: click.Context, label: str | None, budget: int | None, no_cache: bool, gh_args: tuple[str, ...]) -> None:
    """One read-only GitHub API request, throttled. GitHub's answer goes to stdout as it is.

    \b
    It takes `gh api`'s form: one endpoint (or graphql) with -X GET|HEAD, -f/-F key=value,
    -H 'Accept: ...', -i and --silent. Pipe the output to jq to pick fields.
    --as and --budget go before `--`. An HTTP error answer is printed too, and exits 1.
    """
    caller = _start(ctx, label, budget)
    req = parse_call(list(gh_args))
    if sys.stdout is None and not req.silent:  # `>&-`: the call would be charged and its answer lost
        raise core.TollboothError("stdout is closed, so GitHub's answer would be lost: redirect it, or pass --silent",
                                  core.EX_USAGE)
    try:
        resp = caller.fetch(req.method, req.path, accept=req.accept, json_body=req.json_body)
    except ValueError as e:  # the guard let through something tollbooth_http refuses
        raise core.TollboothError(f"refused: {e}", core.EX_USAGE) from None
    except tollbooth_throttle.StoppedOnAnswer as e:  # a 502/504 or a rate limit: GitHub's answer, then the exit
        _write_answer(e.answer, req)
        raise
    _write_answer(resp, req)
    if not 200 <= resp.status < 300:
        _fail(ctx, f"HTTP {resp.status} {REASONS.get(resp.status, '')}".rstrip(), core.EX_FAIL)


def _write_answer(resp: http.Response, req: CallRequest) -> None:
    """GitHub's answer on stdout, byte for byte: with -i the status line and headers first; with --silent nothing."""
    if req.silent:
        return
    sys.stdout.flush()  # anything printed as text goes first
    out = sys.stdout.buffer
    if req.include:
        head = [f"HTTP/1.1 {resp.status} {REASONS.get(resp.status, '')}".rstrip()]
        head += [f"{_header_name(k)}: {v}" for k, v in resp.headers.items()]
        out.write("".join(CONTROL_RE.sub("?", line) + "\n" for line in head).encode("utf-8", "replace") + b"\n")
    out.write(resp.body)
    out.flush()


def _header_name(name: str) -> str:
    """x-ratelimit-remaining -> X-Ratelimit-Remaining, the way gh prints header names."""
    return "-".join(part.capitalize() for part in name.split("-"))


def main(argv: list[str] | None = None) -> int:
    """Run the command line and return its exit code."""
    for stream in (sys.stdout, sys.stderr):  # text this locale can't encode (a logged query) prints as '?'
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(errors="replace")
    try:
        cli.main(args=argv, prog_name="gh-tollbooth")
    except SystemExit as e:
        return e.code if isinstance(e.code, int) else 0 if e.code is None else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
