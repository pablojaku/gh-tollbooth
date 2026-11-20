# How gh-tollbooth works

For people reading or changing the code. Agents only need [SKILL.md](../SKILL.md).

## Code layout

A uv project: `pyproject.toml` and `uv.lock` pin Python 3.11 or newer and the libraries (httpx, pydantic,
pydantic-settings, filelock, tenacity, click, graphql-core). `scripts/gh-tollbooth` is a bash wrapper that runs
`uv run --locked --project <repo> python -I scripts/tollbooth_cli.py`, so it needs uv. Each module hides one
thing; the first line of its docstring says which.

| File | What it hides |
|---|---|
| `scripts/gh-tollbooth` | how the command starts: uv, the locked environment, isolated mode |
| `scripts/tollbooth_cli.py` | the command line (Click): `status`, `call`, `sweep`, `rank`, `repos`; prints, maps errors to exit codes |
| `scripts/tollbooth_core.py` | settings (`TOLLBOOTH_*`), exit codes, `TollboothError`, the clock seam, reading a file an agent names |
| `scripts/tollbooth_text.py` | the hidden-character rules: refuse in, `?` out, strip from owner text |
| `scripts/tollbooth_throttle.py` | the lock, `state.db` and the rules for one turn: spacing, blocks, budgets, call log, deadline |
| `scripts/tollbooth_loop.py` | `Caller.fetch()`: one request through the throttle, and what GitHub's answer means for the command |
| `scripts/tollbooth_http.py` | the token, one HTTPS request to `api.github.com`, and `classify()` for rate limits |
| `scripts/tollbooth_guard.py` | the read-only guard for `call` |
| `scripts/tollbooth_graphql.py` | what gh-tollbooth knows about GraphQL: documents, batched search and lookup, reading answers |
| `scripts/tollbooth_sweep.py` | the query file and `sweep()` |
| `scripts/tollbooth_rank.py` | reading JSONL rows, `rank()` and the tables |
| `scripts/tollbooth_repos.py` | the shortlist and `repos()` |

Tests: `uv run --locked pytest`. They mock only the edges (the HTTP send, the token, the clock) and never
touch the network. CI runs them on Python 3.11 and 3.15, on Ubuntu and macOS.

## One request, start to end

A command that talks to GitHub builds a `Caller`: the label, its budget and the shared `Throttle`. The token
is asked for on first use and then kept. Every request goes through `Caller.fetch()`:

1. Start the call's `Deadline` (`TOLLBOOTH_DEADLINE`, 100 s) first, so the time `gh auth token` takes counts.
   A request `tollbooth_http` would refuse is refused here (exit 2), before anything is charged.
2. `Throttle.turn()` runs the rest. `check()`: fail at once if the label's budget is used up (exit 4) or the
   bucket is blocked for longer than `TOLLBOOTH_MAX_WAIT` (exit 75).
3. Without the lock: sleep through a short block on the bucket, then its call gap.
4. Under the lock: check the block and the budget again, charge the label, send the request, record any block
   GitHub reported, and write one line to the call log.
5. `classify()` reads GitHub's answer. Not a rate limit: return it as it is (404 and 403 included). A rate
   limit: wait and go round again, within the limits below. A 502 or 504: stop with exit 3.

## Taking turns

One machine-wide file lock (`filelock`, which uses `flock` on Unix) on `lock` lets one request through at a
time. Each caller sleeps its call gap (0.5 s or more, per bucket) outside the lock first, and waiters poll
every 0.2 s, so an agent that arrives during a long sweep gets in between the sweep's requests. There is no
queue, so turn order is not first-come. If another agent called while a caller waited for the lock, a new gap
of up to 2.5 s is slept under the lock. A longer one (code search, 6.5 s) never is: the caller lets go and
takes its turn again.

## Budgets

`--as LABEL --budget N` counts the GitHub requests made under that label in the last 24 to 25 hours: they are
kept per clock hour in `state.db`, and the count covers the current hour and the 24 before it. A request is
charged just before it is sent, so a killed process still pays. The budget is checked again under the lock, so
parallel calls can't overrun it. `--budget` needs a label other than the default `anon` (`--as`, or
`TOLLBOOTH_AS`). `--as` and `--budget` go before or after the command; a value after it wins.

Budgets are cooperative. A budget holds only while every command passes `--budget`; it can't stop an agent
that drops it, picks another label or calls GitHub another way.

## State files

The state folder is `~/.cache/gh-tollbooth/` (or `TOLLBOOTH_STATE_DIR`, an absolute path):

- `lock`: the machine-wide lock, held for one request.
- `state.db`: one SQLite file, the only shared state. Tables: `last_call` (the last call time per bucket, for
  spacing), `block` (until when each bucket is blocked), `charge` (budget charges per label and hour),
  `backoff` (the shared secondary-limit backoff) and `call` (the call log).
- The call log keeps the newest 5,000 calls: time, label, pid, bucket, HTTP status (0 if no answer), seconds,
  and what was asked (method, path and a GraphQL document, cut to 160 characters, hidden characters shown as
  `?`). It is informational; budgets don't depend on it.

Every step is one short SQLite transaction. A write starts with `BEGIN IMMEDIATE`, so what it reads and what it
writes are one step for every agent; another agent waits up to 10 s, then gets exit 76. The tables are
`STRICT`, so a value of the wrong type is refused when written and readers need not check what they get. A
file that isn't a database is moved to `state.db.broken` and a new one is started, with one stderr line. If
someone deletes the folder while agents run, it is made again (up to 5 tries). A folder or file that can't be
used is exit 1.

Times shared between processes use the wall clock; a call's own deadline uses the monotonic clock. A block more
than 3,700 s ahead, or a last-call time in the future, is ignored; a block is never recorded for over an hour.

## Rate limits

`classify()` decides from GitHub's HTTP status and rate-limit headers, never from error text, which can echo
a query. It reads the body in two narrow cases: the `documentation_url` of a 403 (GitHub sets it; a secondary
limit can arrive without headers), and whether a GraphQL 200 with nothing left has errors and no data. It
never reads what the errors say.

| GitHub answers | Meaning | What happens |
|---|---|---|
| 403/429, or a GraphQL 200, with `Retry-After` | secondary limit | every bucket waits that long, for every agent |
| 403/429, or a GraphQL 200 with errors and no data, with `X-RateLimit-Remaining: 0` | primary limit | the bucket in `X-RateLimit-Resource` waits until `X-RateLimit-Reset` |
| 429 without those headers, or a 403 linking GitHub's secondary-limit docs | secondary limit | backoff, see below |
| any other 403, 404, ... | not a limit | returned as it is |
| 502/504 | GitHub gave up | exit 3 (for GraphQL, with advice to make the query lighter) |

- A primary limit blocks the bucket GitHub names, if gh-tollbooth tracks it: `graphql`, `core`, `search` or
  `code_search`. A limit on a resource it doesn't track (`dependency_sbom`, `audit_log`, ...) is not shared:
  nothing is recorded, only that call stops, with exit 75.
- A secondary limit blocks every bucket. The wait is `Retry-After` when GitHub gives one. Otherwise it is a
  shared backoff in `state.db`: 60 s, doubled on each such answer to any agent, reset by any answer that is
  not a limit. The first blocks every bucket for 60 s for every agent; the call sleeps (lock released) and
  tries again if that fits its deadline. The second is 120 s, which is over `TOLLBOOTH_MAX_WAIT`, so the call
  exits 75 at once.
- Waits happen without the lock. Anything longer than `TOLLBOOTH_MAX_WAIT` (60 s) exits 75 at once.
- A call takes at most 4 rate-limited answers. The 4th exits 75, whatever the wait.
- No request starts with less than 15 s left before the deadline (half the deadline, if that is shorter).
  The call exits 76 instead (75 if the wait was for a rate limit). A request that was charged is always
  sent, with at least 1 s.
- Each request may take up to 90 s, and never more than the time left before the deadline.
- When a call stops on GitHub's own answer, `call` still prints that answer before it exits.

### Why `status` skips the throttle

`status` asks GitHub for `GET rate_limit` directly, through `Caller.rate_limit()`, not through
`Throttle.turn()`. GitHub doesn't count that endpoint against any bucket, and `status` is what you run while
buckets are blocked. Routed through the throttle it would wait or fail exactly when it is needed: it would sit
behind the very block it is meant to show, and charge a budget for a free request. So it takes no turn, makes
no charge, keeps no call gap and writes no log line. It has its own 30 s timeout. It then adds what gh-tollbooth
knows: the blocks in `state.db` and the recent calls from the log.

## The HTTP layer

`tollbooth_http.send()` is the only place that talks to GitHub:

- Only `https://api.github.com`, and only GET, HEAD, or POST to `graphql`. A redirect is returned as the
  answer, never followed.
- The token comes from `GH_TOKEN`, then `GITHUB_TOKEN`, then `gh auth token --hostname github.com`, run with
  a stripped-down environment (no agent secrets, no `GH_HOST`). It goes only into the `Authorization` header.
- Every request ends within its timeout, even on a hung DNS lookup or a server that drips bytes: the exchange
  runs on a helper thread that is left behind at the deadline.
- An answer over 64 MB, one cut off before its end, or one with no `Content-Length` and no chunking (so it
  can't be checked for being whole) is no answer (exit 1).

## The read-only guard

`gh-tollbooth call` takes `gh api`'s arguments and turns them into one read-only request. It is fail-closed:
anything not explicitly allowed is refused (exit 2). No `gh` process runs. Click reads the arguments after
`--`; it knows only `-X`, `-f`/`-F`, `-H`, `-i` and `--silent`.

- **Only `api`.** Other `gh` commands are refused, with a hint to use the API form.
- **REST is GET or HEAD.** GET is the default, so fields become query parameters, never a body.
- **GraphQL documents are parsed** by graphql-core. Every top-level definition must be a `query` or a
  `fragment`. A document that doesn't parse is refused too: gh-tollbooth refuses what it can't verify. The
  word "mutation" inside a string is fine, because the parser tells strings from keywords. A GraphQL call
  takes no `-X` and no query string, so the checked document is the one sent.
- **Endpoints are relative API paths.** No URLs, and no `%` or `.`/`..` segments in the path (a query string
  may hold `%` escapes): each of those could turn a checked path into another one (including `graphql`) on
  GitHub's side. `:owner`, `:repo` and `:branch` are refused, in the endpoint and in `-F` values, and so are
  `{` and `}` in `-F` values.
- **One call is one request.** `--paginate` and `--slurp` are refused, so budgets and spacing stay honest.
  `--jq`, `--template` and `--cache` are gone, each with a hint (`--no-cache` is accepted and ignored).
- **No local reads.** `-F key=@file` is refused.
- **Hidden characters** and arguments that aren't valid UTF-8 are refused (see below).
- **The output is GitHub's answer, byte for byte.** `-i` prints the status line and headers first, with hidden
  characters shown as `?`. A non-2xx answer is printed too, then `HTTP 404 Not Found` goes to stderr and the
  exit code is 1. A closed stdout is refused before anything is sent (unless `--silent`); a reader that closes
  the pipe still gets the stop line on stderr and the right exit code.

`sweep` and `repos` build their own GraphQL documents and pass the queries and names only as variables, so
text from a file never becomes part of a document.

## Hidden characters

`tollbooth_text` is the one place. A hidden character is one a person reading a terminal can't see but a
program or model still reads: Unicode categories Cc (controls), Cf (format: zero-width, bidi, tag characters),
Zl, Zp and Cs, plus every variation selector (U+180B-180D, U+180F, U+FE00-FE0F, U+E0100-E01EF), which Unicode
files as combining marks. Three rules:

- **In from an agent: refuse.** An argument of `call`, or a query-file line, with one is refused (exit 2). Tab
  and newline are allowed in arguments (a GraphQL document has lines). Argv bytes that weren't UTF-8 are
  refused too.
- **Out to a terminal: show as `?`.** Everything gh-tollbooth prints (stderr lines, `-i` headers, `status`, the
  call log) goes through `printable()`, tab and newline included.
- **Owner text: strip.** A repository's description and README are written by its owner, and agents read
  them. `strip_hidden()` removes the characters (tab and newline stay), so an emoji keeps its symbol and
  loses only the selector after it. Sweep rows get the same for descriptions. Visible text that tries to
  instruct an agent stays: it is data, not instructions.

## Sweep

- **The query file** must end in `.tsv`, be a regular file (checked on the opened file; symlinks refused) of
  at most 64 KB, in UTF-8. Every line that is not blank and doesn't start with `#` is `slice<TAB>query`:
  exactly one tab, a slice of 1 to 40 letters, digits, `.`, `_` or `-`, and a query with no hidden
  characters. ` sort:stars-desc` is added to a query that names no sort; the result must fit GitHub's 256
  characters. Errors show the path and line number, never text from the file.
- **Batches.** One request asks for at most 100 results and holds at most 6 queries: `min(6, 100 // first)`
  queries, 5 at the default `--first 20`. Each query is an alias (`q0`, `q1`, ...) with its own variable.
- **Too heavy.** A 502/504 splits the batch in half. A single query is tried once more with `first: 10`. If
  that fails too, the sweep ends at once with exit 3; later batches are not sent.
- **Partial answers.** A query whose alias is missing or null, has an error under it, or holds a null node is
  counted as missing. After every row is written the sweep exits 3 and names those slices.
- `caller.check("graphql")` runs before the first request, so a spent budget or a long block costs nothing.

## Rank

`rank` makes no request: no token, no state folder, no lock.

- **Input.** Regular JSONL files only, 64 MB in total, UTF-8, one JSON object per line, read by pydantic's
  JSON parser, which has the same limits on every Python build: nesting 200 levels, numbers up to 4,300
  digits. Rows are read strictly: a wrong-typed field refuses the file (exit 2, `path:line` and the field),
  never a value from it. `nameWithOwner` must be an owner/name, `stargazerCount` an integer, `isArchived` and
  `isFork` booleans, `slice` a slice name, `fetched` an ISO 8601 date-time, `pushedAt` starting with a real
  date, the text fields strings.
- **Merging.** Rows with an `error` field or no name are skipped. One entry per repo, whatever the case of its
  name: the newest row by `fetched` wins (a row without one is the oldest), then the one with more stars. The
  entry's slices and queries are those of every row that found the repo; its hits are the distinct queries.
- **Output.** Sorted by stars, or by hits then stars, ties by name. Every `tsv` and `md` text cell has hidden
  characters, tabs, line breaks and `|` removed, and is cut to length (description 100, slices 80, other
  cells 40). In `md`, `[` and `]` become parentheses and `<`, `>` and backticks spaces, and the link is built
  from the name, never from the row's `url`.

## Repos

- **The shortlist.** Names on the command line come first. Then, with `--from`, the rows' repos in rank's
  order (newest row, most stars, archived repos and forks left out), cut to `--top`. A name is kept once,
  whatever its case. A refusal costs nothing.
- **Batches.** 20 repos per request, or 5 with `--readme`. Owner and name travel only as variables. The README
  is looked up at HEAD under 7 file names; the first of them with visible text wins.
- **Too heavy.** A 502/504 splits the batch in half. A single repo with `--readme` is tried once more without
  its README (`readme: null`, `incomplete: "too heavy"`); one still too heavy gets an `"error": "too heavy"`
  row, and the run goes on with the next repos.
- **Missing and partial.** A repo GitHub reports as NOT_FOUND gets `"error": "not found"` and doesn't fail
  the run. Any other missing repo gets an error row with GitHub's error type in lower case, or `no data`. A
  repo that is present but has an error under it gets an `incomplete` field (not `error`, so `rank` keeps the
  row). Any of these ends the run with exit 3, after every row.

## Output

`sweep` and `repos` print JSONL to stdout and flush it after every batch, so a failed or killed run keeps
what it fetched. A closed stdout is refused before anything is sent. gh-tollbooth writes no files apart from
its state folder, so where output may land is decided by the shell and the agent harness.

## Errors and exit codes

Every failure gh-tollbooth reports is one `gh-tollbooth: ...` line on stderr, never a traceback. Messages show hidden characters
as `?` and never quote a file's text or a token. A bug prints `internal error (Type: message); please report
it` and exits 1. A usage error prints Click's own usage message and exits 2, and abbreviated options are refused. Ctrl-C
exits 130. The `Caller` turns every
failure of a request into a `TollboothError` with its exit code, so a command catches one kind.

| Code | Meaning |
|---|---|
| 0 | ok |
| 1 | no token, no answer from GitHub, an HTTP error answer, no usable state folder, or a bug |
| 2 | usage, a request or an input file gh-tollbooth refuses |
| 3 | GitHub gave up on all or part of a request (`sweep` and `repos` print what they have first) |
| 4 | budget used up |
| 75 | GitHub rate limit: stop and report the time |
| 76 | busy or out of time: retry once later |

## Trust boundary

An agent may choose the arguments, never the environment. Python runs in isolated mode (`-I`), so
`PYTHONPATH` can't put code into the process. The environment still picks uv and its Python (`PATH`), the
token (`GH_TOKEN`, `GITHUB_TOKEN`) and a proxy that sees the token (`HTTPS_PROXY` with `SSL_CERT_FILE`).
Don't let agents put variable assignments in front of the command. The note at the top of
`scripts/tollbooth_cli.py` says the same.

## Known limits

- Coordination is cooperative: raw `gh` or `curl` calls bypass gh-tollbooth, and budgets bind only the agents
  that keep their label and budget.
- Turn order is not first-come: agents waiting at the same moment are served in any order. Under heavy
  code-search contention an earlier caller can exit 76 while a later one is served. Nobody waits past the
  deadline.
- If the lock file is deleted while it is held, a later agent can't see the holder and the two overlap once.
- Redirects are not followed: a renamed repo answers 301, and `call` exits 1.
- `sweep` stops at once when a single query is still too heavy at `first: 10`, without naming the slices
  already missing. A query that only worked at `first: 10` returns at most 10 rows and is not counted as
  partial.
- `repos` strips hidden characters from READMEs and descriptions, but visible text that tries to instruct an
  agent stays. It is data, not instructions.
