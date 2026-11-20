# GitHub query reference

Run every recipe through gh-tollbooth: `gh-tollbooth call -- api graphql -f query='…'`. Nothing is
cached: every call is a live, charged request. `call` prints GitHub's body byte for byte, so pipe it to `jq`
to pick fields (`--jq` and `--template` are gone). Only queries pass: a mutation, a document that doesn't
parse and an argument with a hidden character are refused (exit 2). Each GraphQL recipe below cost 1 point
when checked in November 2026.

## Repository search syntax

Search terms and qualifiers go in the query string: in a sweep file's query column, and in the GraphQL
`search(query:)` argument.

| Want | Qualifier |
|---|---|
| Match in a field | `in:name`, `in:description`, `in:topics`, `in:readme` (without `in:`, GitHub searches name, description and topics) |
| Topic / language / license | `topic:typst`, `language:rust`, `license:mit` |
| Popularity | `stars:>=100`, `stars:10..500`, `forks:>10` |
| Activity | `pushed:>=2025-06-01` (last push), `created:2024-01-01..2024-12-31` |
| Scope | `user:NAME`, `org:NAME`, `repo:OWNER/NAME` |
| Exclude junk | `archived:false`, `fork:true` to *include* forks (forks are excluded by default) |
| Order | `sort:stars-desc`, `sort:updated-desc`, `sort:forks-desc` (`sweep` adds `sort:stars-desc` to a query that names no sort) |
| Logic | `"exact phrase"`, `a OR b`, `NOT term`, `-language:javascript` |

Limits: 256 characters per query (the added sort included), at most 5 `AND`/`OR`/`NOT` operators,
1,000 results per query.

## Sweep query files

One `slice<TAB>query` per line, in a file that ends in `.tsv`. The slice (1 to 40 letters, digits, `.`, `_`
or `-`) names the part of the topic a query covers; every row the query finds carries it, and
`rank --slice NAME` keeps only the repos a query of that slice found. Blank lines and lines starting with `#`
are skipped. Run it with `gh-tollbooth sweep queries.tsv > raw.jsonl`.

To get past 1,000 results, split by star band or date instead of paging:
```
wiki	llm wiki stars:>=1000
wiki	llm wiki stars:200..999
wiki	llm wiki stars:50..199
```

Query-writing tips:
- Use one concept per line. A repo's hits are the distinct queries that found it, so
  `gh-tollbooth rank --by hits` shows which repos many phrasings agree on.
- Mix free-text lines (`code wiki`) with `topic:` lines (`topic:deepwiki`). Topics are precise but
  many repos don't set them.
- Add `pushed:>=<6 months ago>` when you only want maintained projects, or filter afterwards with
  `rank --pushed-since YYYY-MM-DD`, which costs nothing.
- When searching for agent skills, repo search misses most of them because they sit inside skill
  collections. Add a code search on the skill file, e.g.
  `gh-tollbooth call -- api search/code -f q='"devil'"'"'s advocate" filename:SKILL.md' -f per_page=30 | jq -r '.items[] | "\(.repository.full_name)\t\(.path)"'`.
  Then fetch several of the files in one GraphQL call with aliased
  `object(expression: "HEAD:path/SKILL.md")` lookups.

## REST calls

REST requests are GET unless you pass `-X HEAD`; `-f` and `-F` fields become query parameters. Add
`-i` to see the status line and headers first.

```
gh-tollbooth call -- api search/issues -f q='repo:cli/cli is:issue is:open label:bug' -f per_page=50 | jq '.total_count'
gh-tollbooth call -- api repos/cli/cli/releases -f per_page=5 | jq -r '.[].tag_name'
gh-tollbooth call -- api repos/cli/cli/contents/README.md -H 'Accept: application/vnd.github.raw' > README.md
```

REST lists page with `-f per_page=100 -f page=2`; each page is one charged call.

## GraphQL recipes

### Page deep through one search (more than 100 results)

gh-tollbooth refuses `--paginate` (one call = one request). Page by hand with the cursor:

```
gh-tollbooth call -- api graphql -f query='query($after: String) {
  search(query: "deepwiki stars:>200 sort:stars-desc", type: REPOSITORY, first: 50, after: $after) {
    pageInfo { hasNextPage endCursor }
    nodes { ... on Repository { nameWithOwner stargazerCount pushedAt } } } }' | jq '.data.search'
```
That is the first page. For each next page, add `-f after=CURSOR` before the `|`, with CURSOR the
`endCursor` of the page before (it looks like `Y3Vyc29yOjUw`). Stop when `hasNextPage` is false. Every
field other than `query` becomes a GraphQL variable. Each page is one charged call.

### Issues and PRs (`type: ISSUE` covers both; filter with `is:issue` / `is:pr`)

```
gh-tollbooth call -- api graphql -f query='{ search(query: "repo:typst/typst is:issue is:open html export sort:reactions-desc", type: ISSUE, first: 20) {
  issueCount
  nodes { ... on Issue { number title url createdAt reactions { totalCount } comments { totalCount } } } } }'
```

### Discussions

```
gh-tollbooth call -- api graphql -f query='{ search(query: "typst html", type: DISCUSSION, first: 10) {
  discussionCount
  nodes { ... on Discussion { title url upvoteCount repository { nameWithOwner } } } } }'
```

### Users and organizations

```
gh-tollbooth call -- api graphql -f query='{ search(query: "typst", type: USER, first: 10) {
  userCount
  nodes { ... on User { login name followers { totalCount } } ... on Organization { login name } } } }'
```

### List a directory, or read a file, at HEAD

```
gh-tollbooth call -- api graphql -f query='{ repository(owner: "typst", name: "typst") {
  tree: object(expression: "HEAD:crates") { ... on Tree { entries { name type } } }
  file: object(expression: "HEAD:Cargo.toml") { ... on Blob { byteSize text } } } }'
```

### Recent commits touching a path

```
gh-tollbooth call -- api graphql -f query='{ repository(owner: "typst", name: "typst") {
  defaultBranchRef { target { ... on Commit {
    history(first: 10, path: "crates/typst-html") { nodes { committedDate messageHeadline url } } } } } } }'
```

### Price a query before running it

```
gh-tollbooth call -- api graphql -f query='{ …your fields… rateLimit(dryRun: true) { cost remaining } }'
```
With `dryRun: true`, GitHub returns only the `rateLimit` block (the cost) and doesn't run the rest of
the query.

## Cost rules of thumb

- Cost is roughly (requests needed for every connection) ÷ 100, rounded, with a minimum of 1.
  A nested connection runs once per parent node: `search(first: 50) { … topics(first: 6) }`
  counts 50 topic requests.
- Plain fields and `object(expression:)` lookups are free. Connections such as `history`,
  `issues` and `topics` cost.
- `totalCount`-only connections such as `issues(states: OPEN) { totalCount }` need no `first:`
  and are cheap.
- Aliasing up to 20 `repository(owner:, name:)` lookups in one call costs about 1 point.
  `gh-tollbooth repos` batches 20 per request, or 5 when `--readme` is set.
