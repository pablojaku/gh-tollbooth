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
import tollbooth_rank  # noqa: E402
import tollbooth_sweep  # noqa: E402
import tollbooth_throttle  # noqa: E402
from tollbooth_guard import CallRequest, parse_call  # noqa: E402
from tollbooth_loop import Caller  # noqa: E402
from tollbooth_text import LABEL_RE, printable  # noqa: E402

MAX_RESET = 10 ** 11  # seconds; a reset time later than this is not a real Unix time


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
    return Caller(who, limit, tollbooth_throttle.Throttle(core.SETTINGS.state_dir))


def _reader_left() -> None:
    """The reader closed the pipe (`| head`). Not an error: stdout goes nowhere, so the flush at exit is quiet."""
    with suppress(OSError, ValueError):
        fd = sys.stdout.fileno()
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, fd)
        os.close(devnull)


def _fail(ctx: click.Context, message: str, code: int) -> NoReturn:
    click.echo(f"gh-tollbooth: {printable(message)}", err=True)
    ctx.exit(code)


class _Guarded(click.Group):
    """Every failure is one line on stderr and an exit code, never a traceback; a bug too ("internal error",
    exit 1). The Caller turns every failure of a request into a TollboothError."""

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
        except KeyboardInterrupt:
            ctx.exit(130)
        except (click.exceptions.Exit, click.ClickException, click.Abort):
            raise  # Click's own ways out: ctx.exit() and usage errors
        except Exception as e:  # a bug
            _fail(ctx, f"internal error ({type(e).__name__}: {str(e)[:200]}); please report it", core.EX_FAIL)


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
    Exit codes: 0 ok; 1 no token, no answer from GitHub, an HTTP error answer, no state folder or a bug;
    2 usage, a request or an input file gh-tollbooth refuses; 3 GitHub gave up on all or part of a request;
    4 budget used up; 75 GitHub rate limit (stop and report the time); 76 busy or out of time (retry once later).
    """
    ctx.obj = {"label": label, "budget": budget}


@cli.command()
@_global_options
@click.option("--last", type=click.IntRange(min=0), default=8, show_default=True, help="Recent calls to list.")
@click.pass_context
def status(ctx: click.Context, label: str | None, budget: int | None, last: int) -> None:
    """Rate-limit buckets and recent calls. Free: GitHub doesn't count it."""
    caller = _start(ctx, label, budget)
    body = caller.rate_limit()  # outside the throttle (ADR-0001): it works while buckets are blocked
    try:
        resources = _RateLimit.model_validate_json(body).resources
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
    except tollbooth_throttle.StoppedOnAnswer as e:  # a 502/504 or a rate limit: GitHub's answer, then the exit
        _write_answer(e.answer, req)
        raise
    _write_answer(resp, req)
    if not 200 <= resp.status < 300:
        _fail(ctx, f"HTTP {resp.status} {REASONS.get(resp.status, '')}".rstrip(), core.EX_FAIL)


@cli.command()
@_global_options
@click.option("--first", type=click.IntRange(1, tollbooth_sweep.NODE_BUDGET), default=20, show_default=True,
              metavar="N", help="Results per query, 1 to 100.")
@click.option("--no-cache", is_flag=True, help="Accepted and ignored: gh-tollbooth doesn't cache.")
@click.argument("queries_file", metavar="QUERIES.tsv")
@click.pass_context
def sweep(ctx: click.Context, label: str | None, budget: int | None, first: int, no_cache: bool,
          queries_file: str) -> None:
    """Many repository searches, a few per GraphQL request: one JSON row per repository on stdout.

    \b
    QUERIES.tsv has one slice<TAB>query per line; # comments and blank lines are skipped. The slice names
    the part of the topic a query covers and goes into every row the query finds. sort:stars-desc is
    added to a query that names no sort. Progress goes to stderr. Exit 3 means some queries came back
    incomplete; the rows printed are still good.
    """
    caller = _start(ctx, label, budget)
    queries = tollbooth_sweep.read_queries(queries_file)  # a refused file costs nothing
    if sys.stdout is None:  # `>&-`: every row would be lost
        raise core.TollboothError("stdout is closed, so the rows would be lost: redirect it to a file", core.EX_USAGE)
    tollbooth_sweep.sweep(caller, queries, first, sys.stdout)


class _Day(click.ParamType):
    """A real date written YYYY-MM-DD."""

    name = "YYYY-MM-DD"

    def convert(self, value: object, param: click.Parameter | None, ctx: click.Context | None) -> dt.date:
        if isinstance(value, dt.date):
            return value
        if isinstance(value, str) and tollbooth_rank.DATE_RE.fullmatch(value):
            try:
                return dt.date.fromisoformat(value)
            except ValueError:
                pass
        self.fail("must be a real date written YYYY-MM-DD", param, ctx)


def _slice_name(ctx: click.Context, param: click.Parameter, value: str | None) -> str | None:
    if value is not None and not LABEL_RE.fullmatch(value):
        raise click.BadParameter("a slice is 1 to 40 letters, digits, '.', '_' or '-'")
    return value


@cli.command()
@_global_options
@click.option("--min-stars", type=click.IntRange(min=0), default=0, metavar="N", help="Drop repos with fewer stars.")
@click.option("--pushed-since", type=_Day(), default=None, help="Drop repos last pushed before this day.")
@click.option("--slice", "slice_", metavar="SLICE", callback=_slice_name, help="Keep repos a query of this slice found.")
@click.option("--by", type=click.Choice(["stars", "hits"]), default="stars", show_default=True,
              help="Sort by stars, or by hits (the queries that found a repo), then stars.")
@click.option("--top", type=click.IntRange(min=0), default=0, metavar="N", help="Repos to print after sorting; 0: all.")
@click.option("--format", "fmt", type=click.Choice(["tsv", "md", "jsonl"]), default="tsv", show_default=True,
              help="Output format.")
@click.option("--include-archived", is_flag=True, help="Keep archived repos.")
@click.option("--include-forks", is_flag=True, help="Keep forks.")
@click.argument("files", nargs=-1, required=True, metavar="RAW.jsonl...")
def rank(label: str | None, budget: int | None, min_stars: int, pushed_since: dt.date | None, slice_: str | None,
         by: str, top: int, fmt: str, include_archived: bool, include_forks: bool, files: tuple[str, ...]) -> None:
    """Merge, filter and sort sweep rows: one line per repository. Offline: no token, no state folder, no request.

    \b
    Reads the JSONL files sweep wrote (64 MB in total). Each repository's newest row wins; its hits are the
    distinct queries that found it. --as and --budget are accepted and ignored.
    """
    rows = tollbooth_rank.read_rows(list(files))  # a refused file prints nothing
    if sys.stdout is None:  # `>&-`: the table would be lost
        raise core.TollboothError("stdout is closed, so the table would be lost: redirect it", core.EX_USAGE)
    options = tollbooth_rank.RankOptions(min_stars, pushed_since, slice_, by, top, include_archived, include_forks)
    entries = tollbooth_rank.rank(rows, options)
    tollbooth_rank.write_table(entries, fmt, sys.stdout)
    core.log.info("%d repos from %d rows (sorted by %s)", len(entries), len(rows), by)


def _write_answer(resp: http.Response, req: CallRequest) -> None:
    """GitHub's answer on stdout, byte for byte: with -i the status line and headers first; with --silent nothing.

    A reader that closed the pipe (`| jq` that failed) gets nothing more, and the command goes on: its line
    on stderr and its exit code still say what GitHub answered (exit 75 for a rate limit, not 0).
    """
    if req.silent:
        return
    try:
        sys.stdout.flush()  # anything printed as text goes first
        out = sys.stdout.buffer
        if req.include:
            head = [f"HTTP/1.1 {resp.status} {REASONS.get(resp.status, '')}".rstrip()]
            head += [f"{_header_name(k)}: {v}" for k, v in resp.headers.items()]
            out.write("".join(printable(line) + "\n" for line in head).encode("utf-8", "replace") + b"\n")
        out.write(resp.body)
        out.flush()
    except BrokenPipeError:
        _reader_left()


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
