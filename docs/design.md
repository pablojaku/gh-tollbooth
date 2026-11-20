# How gh-tollbooth works

For people reading or changing the code. Agents only need [SKILL.md](../SKILL.md).

## Code layout

| File | Job |
|---|---|
| `scripts/gh-tollbooth` | CLI: `status`, `call`, `sweep`, `rank`, `repos`; parses arguments, prints, maps errors to exit codes |
| `scripts/tollbooth_core.py` | settings, exit codes, `infer_bucket()`, the clock seam |
| `scripts/tollbooth_throttle.py` | the shared state folder and the rules for one turn: lock, spacing, blocks, budgets, call log |
| `scripts/tollbooth_loop.py` | `Caller.fetch()`: one request through the throttle, and the decision to try again |
| `scripts/tollbooth_http.py` | the token, one HTTPS request to `api.github.com`, and `classify()` for rate limits |
| `scripts/tollbooth_guard.py` | the read-only guard for `call` |
| `scripts/tollbooth_graphql.py` | what gh-tollbooth knows about GraphQL: documents, `post()`, row flattening and cleaning |
| `scripts/tollbooth_sweep.py` | the query file and `sweep()` |
| `scripts/tollbooth_rank.py` | reading JSONL rows, `rank()` and the tables |
| `scripts/tollbooth_repos.py` | the shortlist and `repos()` |

Tests: `python3 -m unittest discover -s tests` from the repo root. They mock only the edges (the HTTP
send, the token, the clock), never run `gh` and never touch the network. CI runs them on Python 3.9
and 3.15, on Ubuntu and macOS.

## One request, start to end

A command that talks to GitHub builds a `Caller`: the label, its budget and the shared throttle. The
token is asked for on first use and then kept. Every request goes through `Caller.fetch()`:

1. `check()`: fail at once if the label's budget is used up (exit 4) or the bucket is blocked for longer
   than `TOLLBOOTH_MAX_WAIT` (exit 75).
2. `wait_turn()`: without the lock, sleep through a short block on the bucket, then its call gap.
3. `attempt()`: under the machine-wide lock, check the budget and the block again, charge the label,
   send the request, record any block GitHub reported, and write one line to the call log.
4. `classify()` reads GitHub's answer. Not a rate limit: return it as it is (404 and 403 included). A
   rate limit: wait and go round again, within the limits below. A 502 or 504: stop with exit 3.

The deadline (`TOLLBOOTH_DEADLINE`, 100 s) covers all of this for one request. It runs on the process's
own monotonic clock.

## Taking turns

One machine-wide file lock (`flock` on `lock`) lets one request through at a time. Each caller sleeps
its call gap (0.5 s or more, per bucket) outside the lock first, and waiters poll every 0.2 s, so an agent
that arrives during a long sweep gets in between the sweep's requests. There is no queue, so turn order is
not first-come. If another agent called while a caller waited for the lock, a new gap of up to 2.5 s is
slept under the lock. A longer one (code search, 6.5 s) never is: the caller lets go and takes its turn
again.

## Budgets

`--as LABEL --budget N` counts the GitHub requests made under that label in the last 24–25 hours: they
are kept per clock hour in `state.json`, and the count covers the current hour and the 24 before it. A
request is charged just before it is sent, so a killed process still pays. The budget is checked again
under the lock, so parallel calls can't overrun it. `--budget` needs a label other than the default
`anon` (`--as`, or `TOLLBOOTH_AS`).

Budgets are cooperative. A budget holds only while every command passes `--budget`; it can't stop an
agent that drops it, picks another label or calls GitHub another way.

## State files

`~/.cache/gh-tollbooth/` (or `TOLLBOOTH_STATE_DIR`):

- `lock`: the machine-wide lock, held for one request.
- `state.lock`: a second, short lock around every change to `state.json` and every write to the log.
  It is taken after `lock`, never around it, and never held across a GitHub request.
- `state.json`: the last call time per bucket (for spacing), blocks, and budget charges per label and hour.
- `calls.log`: one tab-separated line per request: time, label, pid, bucket, HTTP status (0 if no
  answer), seconds, and what was asked (method, path and a GraphQL document, cut to 160 characters,
  control characters replaced). Informational; trimmed to its newest 5,000 lines past 2 MB.

`state.json` is replaced in one step (write a temp file, then rename), so readers see the old file or
the new one. What is read from them is checked before use: a wrongly shaped value reads as missing. If someone
deletes the folder while agents run, it is created again.

Times shared between processes (blocks, call gaps, charges) use the wall clock, because Apple's
`/usr/bin/python3` has a per-process monotonic clock. A block more than an hour ahead, or a last-call
time in the future, is ignored.

## Rate limits, from the status and headers

`classify()` decides from GitHub's HTTP status and rate-limit headers, never from error text, which can
echo a query. It reads the body in two narrow cases: the `documentation_url` of a 403 (GitHub sets it;
a secondary limit can arrive without headers), and whether a GraphQL 200 with nothing left has a
non-empty `errors` list. It never reads what the errors say.

| GitHub answers | Meaning | What happens |
|---|---|---|
| 403/429 with `Retry-After` | secondary limit | every bucket waits that long, for every agent |
| 403/429, or a GraphQL 200 with errors, with `X-RateLimit-Remaining: 0` | primary limit | the bucket in `X-RateLimit-Resource` waits until `X-RateLimit-Reset` |
| 429 without those headers, or a 403 linking GitHub's secondary-limit docs | secondary limit | backoff, see below |
| any other 403, 404, ... | not a limit | returned as it is |
| 502/504 | GitHub gave up | exit 3 (for GraphQL, with advice to make the query lighter) |

- A limit on a side resource gh-tollbooth doesn't track (`dependency_sbom`, `audit_log`, ...) blocks the
  bucket of the request that received it. That is `core` in practice, because side resources are
  REST-only.
- Backoff for a secondary limit that names no wait starts at 60 s and doubles on each such answer within
  one call. The first one blocks every bucket for 60 s for every agent; the call sleeps (lock released)
  and tries again if that fits its deadline. The second blocks for 120 s, which is over
  `TOLLBOOTH_MAX_WAIT`, so the call exits 75 at once.
- Waits happen without the lock. Anything longer than `TOLLBOOTH_MAX_WAIT` (60 s) exits 75 at once.
- A call takes at most 4 rate-limited answers. The fourth exits 75.
- No request starts with less than 15 s left before the deadline (half the deadline, if that is
  shorter). The call exits 76 instead.
- Each request may take up to 90 s, and never more than the time left before the deadline.

`status` asks GitHub for `GET rate_limit` directly, not through the throttle. GitHub doesn't count that
request against any bucket, and `status` is what you run while a bucket is blocked: through the
throttle it would wait behind the very block it is meant to show, and charge a budget for a free
request. So it takes no turn, is not charged and is not logged.

## The HTTP layer

`tollbooth_http.send()` is the only place that talks to GitHub:

- Only `https://api.github.com`, and only GET, HEAD, or POST to `graphql`. A redirect is returned as
  the answer, never followed.
- The token comes from `GH_TOKEN`, then `GITHUB_TOKEN`, then `gh auth token --hostname github.com`,
  run with a stripped-down environment (no agent secrets, no `GH_HOST`). It goes only into the
  `Authorization` header.
- Every request ends within its timeout, even on a hung DNS lookup or a server that drips bytes: the
  exchange runs on a helper thread that is left behind at the deadline.
- An answer over 64 MB, or one cut off before its `Content-Length`, is no answer (exit 1).

## The read-only guard

`gh-tollbooth call` takes `gh api`'s arguments and turns them into one read-only request. It is fail-closed:
anything not explicitly allowed is refused (exit 2). No `gh` process runs.

- **Only `api`.** Other `gh` commands are refused, with a hint to use the API form.
- **REST is GET or HEAD.** GET is the default, so fields become query parameters, never a body.
- **GraphQL documents are parsed.** Strings and comments are stripped first, then every top-level
  definition must be a `query` or a `fragment`. The word "mutation" inside a string is fine. A GraphQL
  call takes no `-X` and no query string, so the checked document is the one sent.
- **Endpoints are relative API paths.** No URLs, and no `%` or `.`/`..` segments in the path (a query
  string may hold `%` escapes): each of those could turn a checked path into another one (including
  `graphql`) on GitHub's side. `:owner`, `:repo` and
  `:branch` are refused, in the endpoint and in `-F` values, and so are `{` and `}` in `-F` values.
- **One call is one request.** `--paginate` and `--slurp` are refused, so budgets and spacing stay
  honest. `--jq`, `--template` and `--cache` are gone, each with a hint.
- **No local reads.** `-F key=@file` is refused.
- **Control and invisible characters** (CR, NEL, BOM, U+2028/9, zero-width and bidi controls) and
  arguments that aren't valid UTF-8 are refused.
- **The output is GitHub's answer, byte for byte.** `-i` prints the status line and headers first, with
  control characters replaced.

`sweep` and `repos` build their own GraphQL documents and pass the queries and names only as variables,
so text from a file never becomes part of a document.

## Sweep

- **The query file** must end in `.tsv`, be a regular file (checked on the opened file; symlinks refused)
  of at most 64 KB, in UTF-8. Every line that is not blank and doesn't start with `#` is
  `slice<TAB>query`: exactly one tab, a slice of 1 to 40 letters, digits, `.`, `_` or `-`, and a query
  with no control or invisible characters. ` sort:stars-desc` is added to a query that names no sort; the
  result must fit GitHub's 256 characters. Errors show the path and line number, never text from the file.
- **Batches.** One request asks for at most 100 results and holds at most 6 queries: `min(6, 100 // first)`
  queries, 5 at the default `--first 20`. Each query is an alias (`q0`, `q1`, ...) with its own variable.
- **Too heavy.** A 502/504 splits the batch in half. A single query is tried once more with `first: 10`.
  If that fails too, the sweep ends at once with exit 3; later batches are not sent.
- **Partial answers.** A query whose alias is missing or null, has an error under it, or holds a null
  node is counted as missing. After every row is written the sweep exits 3 and names those slices.
- `caller.check("graphql")` runs before the first request, so a spent budget or a long block costs nothing.

## Rank

`rank` makes no request: no token, no state folder, no lock.

- **Input.** Regular JSONL files only, 64 MB in total, UTF-8, one JSON object per line. Numbers longer than
  20 digits are refused (Python 3.9 parses those in quadratic time), and so are rows nested more than 32
  levels. The fields rank uses must have the right type: `nameWithOwner` an owner/name, `stargazerCount` an
  integer, `isArchived` and `isFork` booleans, `slice` a slice name, `fetched` an ISO 8601 date-time,
  `pushedAt` starting with a real date, the text fields strings. Errors name the path, line and field,
  never a value from the file.
- **Merging.** Rows with an `error` field or no name are skipped. One entry per repo, whatever the case of
  its name: the newest row by `fetched` wins (a row without one is the oldest), then the one with more
  stars. The entry's slices and queries are those of every row that found the repo; its hits are the
  distinct queries.
- **Output.** Sorted by stars, or by hits then stars, ties by name. Every `tsv` and `md` text cell has
  control and invisible characters, tabs, line breaks and `|` replaced, and is cut to length
  (description 100, slices 80, other cells 40). In `md`, `[`, `]`, `<`, `>` and backticks are neutralised,
  and the link is built from the name, never from the row's `url`.

## Repos

- **The shortlist.** Names on the command line come first. Then, with `--from`, the rows' repos in rank's
  order (newest row, most stars, archived repos and forks left out), cut to `--top`. A name is kept once,
  whatever its case.
- **Batches.** 20 repos per request, or 5 with `--readme`. Owner and name travel only as variables. The
  README is looked up at HEAD under 7 file names; the first non-empty one wins.
- **Too heavy.** A 502/504 splits the batch in half. A single repo with `--readme` is tried once more
  without its README (`readme: null`, `incomplete: "too heavy"`); one still too heavy gets an
  `"error": "too heavy"` row, and the run goes on with the next repos.
- **Missing and partial.** A repo GitHub reports as NOT_FOUND gets `"error": "not found"` and doesn't fail
  the run. Any other missing repo gets an error row with GitHub's error type in lower case, or `no data`.
  A repo that is present but has an error under it gets an `incomplete` field (not `error`, so `rank`
  keeps the row). Any of these ends the run with exit 3, after every row.
- **Cleaning.** A repo's description and README are its owner's text, which agents read. Both lose the
  characters that hide text from a person but not from a model: control characters (newlines and tabs
  stay), zero-width and bidi characters, word joiners, Unicode tag characters, variation selectors and
  the soft hyphen. Visible text stays as it is. Sweep rows get the same cleaning for descriptions.

## Output

`sweep` and `repos` print JSONL to stdout and flush it after every request, so a failed or killed run
keeps what it fetched. A closed stdout is refused before anything is sent. gh-tollbooth writes no files
apart from its state folder, so where output may land is decided by the shell and the agent harness.

## Trust boundary

An agent may choose the arguments, never the environment. Python runs in isolated mode (`-I`), so
`PYTHONPATH` can't put code into the process. The environment still picks the `python3` (`PATH`), the
token (`GH_TOKEN`, `GITHUB_TOKEN`) and a proxy that sees the token (`HTTPS_PROXY` with `SSL_CERT_FILE`).
Don't let agents put variable assignments in front of the command.

## Known limits

- Coordination is cooperative: raw `gh` or `curl` calls bypass gh-tollbooth, and budgets bind only the
  agents that keep their label and budget.
- Turn order is not first-come: agents waiting at the same moment are served in any order. Under heavy
  code-search contention an earlier caller can exit 76 while a later one is served. Nobody waits past the
  deadline.
- If the lock file is deleted while it is held, a later agent can't see the holder and the two overlap once.
- A big backward wall-clock step can stretch a shared block, but only up to an hour.
- Redirects are not followed: a renamed repo answers 301, and `call` exits 1.
- `sweep` stops at once when a single query is still too heavy at `first: 10`, without naming the slices
  already missing. A query that only worked at `first: 10` returns at most 10 rows and is not counted as
  partial.
- Python builds differ: CI's and Homebrew's Python 3.14+ parse JSON nested 100,000 levels deep, while
  Python 3.13 and older, and some 3.14+ builds, run out of recursion first. Some deep rows can be read
  but not written back. So `rank`
  refuses rows nested more than 32 levels on every Python.
- `repos` strips invisible characters from READMEs and descriptions, but visible text that tries to
  instruct an agent stays. It is data, not instructions.
