# gh-tollbooth

One polite, shared, read-only request stream to GitHub for every AI agent on a machine.

Coding agents on one machine all use the same GitHub login, so they share every GitHub rate-limit
bucket. One agent looping searches, or five subagents searching in parallel, can lock every session
out until the limit resets. `gh-tollbooth` is a small command that agents call instead of `gh api`
or `curl`. It talks to the GitHub API directly, with the login the [GitHub CLI](https://cli.github.com)
already has:

- **One at a time.** A machine-wide lock and per-bucket spacing turn every agent's calls into one
  request stream.
- **Shared rate-limit handling.** It reads GitHub's status code and rate-limit headers. When GitHub
  reports a limit, every agent on the machine waits or stops with a clear exit code.
- **Read-only.** `call` sends only REST GET/HEAD requests and GraphQL queries.
- **Cheap research.** `sweep` batches repo searches into GraphQL requests, `rank` merges and sorts
  the results offline, and `repos` fetches details and READMEs for a shortlist.
- **Per-agent budgets.** `--as LABEL --budget N` caps the GitHub requests a subagent makes.

It ships as an agent skill: [SKILL.md](SKILL.md) teaches the agent when and how to use it.

## Requirements

- macOS or Linux (it uses `fcntl` file locks, and its first line runs `env -S`)
- Python 3.9 or newer, standard library only. Apple's `/usr/bin/python3` works. Nothing to install
  with pip.
- A GitHub token: log in with the GitHub CLI (`gh auth login`), or set `GH_TOKEN` or `GITHUB_TOKEN`.
  gh-tollbooth asks `gh auth token` for the token and never runs `gh` for anything else.

## Install

```sh
git clone https://github.com/pablojaku/gh-tollbooth ~/.claude/skills/gh-tollbooth
ln -s ~/.claude/skills/gh-tollbooth/scripts/gh-tollbooth ~/.local/bin/gh-tollbooth
gh-tollbooth status
```

`~/.claude/skills` is where Claude Code looks for skills. For another agent, clone into its skills
folder (or anywhere) and keep the symlink. `~/.local/bin` must be on your `PATH`.

## Use

```sh
gh-tollbooth status                                   # rate-limit buckets and recent calls (free)

printf 'cli\ttopic:cli language:go stars:>=500\n' > q.tsv
gh-tollbooth sweep q.tsv > raw.jsonl                  # batched repo search
gh-tollbooth rank raw.jsonl --top 20 --format md      # merge and sort, no API calls
gh-tollbooth repos --from raw.jsonl --top 5 --readme > details.jsonl

gh-tollbooth call -- api repos/cli/cli | jq .stargazers_count
gh-tollbooth call -- api graphql -f query='{ viewer { login } }'
```

Each line of `q.tsv` is `slice<TAB>query`: the slice names the part of the topic the query covers.

Exit codes: `0` ok, `1` no token, no answer from GitHub, an HTTP error answer or no state folder,
`2` refused (usage, a request or an input file), `3` GitHub gave up on all or part of a request,
`4` budget used up, `75` GitHub rate limit (stop), `76` busy or out of time (retry once).

The full command reference and the agent playbooks are in [SKILL.md](SKILL.md).

## What the guard promises

If you let an agent run `gh-tollbooth` without asking (for example with a `Bash(gh-tollbooth *)`
allow rule in Claude Code), you are trusting its guard. Through `gh-tollbooth`, an agent:

- **can't write to GitHub**: no POST, PUT, PATCH or DELETE (POST goes only to `graphql`), and no
  GraphQL mutations or subscriptions;
- **can't reach another host**: every request goes to `https://api.github.com`, and redirects are
  not followed;
- **can't make it read local files** into a request: `-F key=@file` is refused, and the only files it
  reads are the ones you name to `sweep`, `rank` and `repos --from`;
- **can't see the token**: it goes only into the request's `Authorization` header, never to stdout,
  stderr or the call log.

What it does not promise:

- **It is not a sandbox.** An agent that can run `gh` or `curl` directly bypasses it. Budgets and
  turn-taking only bind agents that go through `gh-tollbooth`.
- **Arguments are checked, the environment is trusted.** Python runs in isolated mode, so
  `PYTHONPATH` can't load code into it. But the environment still picks the `python3` (`PATH`), the
  token (`GH_TOKEN`) and a proxy that sees the token (`HTTPS_PROXY` with `SSL_CERT_FILE`). Don't let
  agents put variable assignments in front of the command.
- **Budgets are cooperative.** The caller picks its own `--as` label and `--budget`, so a budget only
  holds for an agent that keeps the label and budget it was given.
- **It reads the files you name.** `sweep` takes only a regular `.tsv` file (no symlink, 64 KB at most)
  of `slice<TAB>query` lines and sends each query to GitHub as a search; the query text also appears in
  the output rows. `rank` and `repos --from` take regular JSONL files, 64 MB in total, and print fields
  from their rows. Errors name a path and line, never text from a file.
- **README text is the repo owner's text.** `repos` removes invisible characters from descriptions and
  READMEs, but visible text that tries to instruct an agent stays. Treat it as data.
- **It doesn't decide where output goes.** Rows go to stdout; your shell and agent harness decide where
  a redirect may write.

The guard's attack cases are in [tests/test_guard.py](tests/test_guard.py). How the guard and the rest
work, and the known limits:
[docs/design.md](docs/design.md).

## Tests

```sh
python3 -m unittest discover -s tests
```

The tests never run `gh` or touch the network.

## License

[MIT](LICENSE)
