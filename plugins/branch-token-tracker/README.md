# branch-token-tracker

Attributes token spend to the task-tracker id in your git branch name.

Your branch name already names the ticket: `feature/PROJ-412-add-login` says
which ticket the next few hours belong to. The plugin reads that name, counts
the tokens each turn costs and adds them up per ticket. You don't start or stop
anything.

```
$ btt report
# Tokens by ticket

**3 ticket(s) · 6,988,323 weighted tokens (49,481,875 raw)**

| ticket | turns | sessions | output | cache read | cache create | weighted | est. USD | models | cost shape | idle | active | PRs | last seen |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| PROJ-412 | 41 | 5 | 214,881 | 38,904,203 | 410,984 | 5,479,170 | $14.14 | opus-5-5 100% | 14.0 calls/turn · 121k ctx | 3 gaps · 402k rewritten | 5h 59m | 1 (1 merged) +412/-96 | 2026-08-08T14:22 |
| #883 | 12 | 2 | 61,008 | 9,401,880 | 96,444 | 1,365,879 | $2.73 | sonnet-5-5 100% | 9.0 calls/turn · 88k ctx | none | 1h 48m | — | 2026-08-07T18:05 |
| unassigned | 4 | 1 | 18,328 | 361,004 | 12,408 | 143,274 | $0.50 | opus-5-5 100% | 4.2 calls/turn · 27k ctx | 1 gap · 9k rewritten | 22m 0s | — | 2026-08-05T09:11 |
```

## Install

```bash
/plugin marketplace add /path/to/agent-toolbox
/plugin install branch-token-tracker@agent-toolbox
```

Capture starts with your next session. To read it back, run `/token-report` in
Claude Code or `btt report` in a shell. The plugin puts `btt` on the `PATH` of
Claude Code's Bash tool. To run it from your own terminal, call the bundled
launcher directly:

```bash
bash ~/.claude/plugins/cache/agent-toolbox/branch-token-tracker/<version>/bin/btt report
```

To pick up a new version, update rather than reinstall:

```bash
claude plugin marketplace update agent-toolbox
claude plugin update branch-token-tracker@agent-toolbox
```

Then restart Claude Code. An update keeps the data directory.

> **Warning:** `claude plugin uninstall` deletes `${CLAUDE_PLUGIN_DATA}`, and
> with it `tokens.db` and every ticket total. Copy the database somewhere else
> before you uninstall:
>
> ```bash
> cp ~/.claude/plugins/data/branch-token-tracker-*/tokens.db ~/tokens.db.bak
> ```

## Commands

| Command | What it does |
| --- | --- |
| `btt report` | Shows spend per ticket, per session or over time. See [Reporting](#reporting). |
| `btt enrich` | Fetches pull request outcomes from GitHub. See [Pull request outcomes](#pull-request-outcomes). |
| `btt ticket` | Shows which ticket a branch name files under. See [Ticket patterns](#ticket-patterns). |
| `btt backfill` | Recovers turns from the transcripts on disk. See [Repairing an existing store](#repairing-an-existing-store). |
| `btt purge-unassigned` | Deletes work with no ticket. See [Purging work with no ticket](#purging-work-with-no-ticket). |
| `btt ingest` | The hook entry point. You don't run it. |

`/token-report` in Claude Code runs `btt report` for you and turns a request
such as "what did PROJ-412 cost last week" into the right options.

## How it works

The plugin runs three hooks. Each one times out after 30 seconds, and none can
block your session: they always exit 0.

| Hook | What it does |
| --- | --- |
| `SessionStart` | Creates or migrates the database. If the current ticket has spend, it prints the running total into the session's context. |
| `Stop` | Resolves the branch, stores every turn not yet stored, records the session's title, repo and billing mode and rewrites `current.json`. |
| `SessionEnd` | Captures one last time, rewrites `current.json` and prints a session summary. |

The plugin resolves the branch at every `Stop`, not once per session. If you
switch branches mid-session, the turns before the switch stay on the old ticket
and the turns after it go to the new one.

The plugin keys each turn by the user-message uuid in the transcript and
inserts it only once. So reading the transcript again after every turn is
idempotent, and a turn's ticket stays fixed to the branch it ran on. Later reads
only raise a turn's token counts, which matters when a hook captures a turn that
is still running.

Every figure in a report is a `GROUP BY` at read time. The plugin stores no
totals, so none can drift, and a killed session leaves nothing half-written.

### What counts as a turn

A turn is one user prompt plus the assistant messages that answer it. Tool
results are also `type=user` records, and the parser leaves them out; counting
them would multiply the turn count. The parser keeps one copy of each assistant
message, keyed by `message.id`, because a message repeats as it streams and each
copy carries the running usage.

### Subagent spend

Subagent tokens count against the ticket like any other. The plugin stores each
subagent as its own row, keyed `agent:<agentId>`, with `query_source` set to
`subagent`.

An agent's usage can come from three places. The plugin keeps the best one, and
better evidence replaces a weaker figure rather than adding to it:

| Rank | Source | What it gives |
| --- | --- | --- |
| 3 | The agent's own transcript | The full split by token class |
| 2 | A completed `Agent` tool result | Real usage, when present |
| 1 | `<subagent_tokens>` in a notification | One bare number |

Each agent writes its own transcript beside the session's:

```
<projects>/<slug>/<session-id>.jsonl                        # the session
<projects>/<slug>/<session-id>/subagents/agent-<id>.jsonl   # one per agent
```

The plugin also finds agents through these files. An agent that wrote a
transcript spent tokens, whether or not the main transcript mentions it, so
capture doesn't depend on a notification arriving in a shape the parser
recognizes. Notifications come in three record types, `type=user`,
`type=attachment` and `type=queue-operation`, which is why they are the weakest
source.

The plugin can't weight a bare number, because the token classes bill at
different rates. The plugin stores it in `total_tokens_agg`, where it counts toward the
raw total and stays out of the weighted figure.

### Weighted tokens compared with raw tokens

Reports rank on weighted tokens: input-equivalent units, in which output counts
5 times, a cache write 1.25 times for a 5-minute TTL or 2 times for a 1-hour
TTL and a cache read a tenth. About 95% of a raw sum is cache reads, so ranking
on raw tokens ranks sessions by length, not by cost. Reports still show the raw
total, but it isn't a cost.

## Configuration

Everything works without configuration. Configure the plugin to:

- Match a ticket scheme the defaults miss, such as ClickUp ids.
- Rename the bucket for work on branches with no ticket.
- Label your rows when you share exports.
- State how Anthropic bills you, instead of letting the plugin guess.

### Where the config comes from

The plugin reads one JSON file, the first it finds in this order:

1. The path in `$BTT_CONFIG`.
2. `.branch-tokens.json` in the working directory or the nearest parent
   directory that has one.
3. `~/.claude/branch-tokens.json`.

Files don't merge with each other. The file found sets the keys it names, and
the built-in defaults fill in the rest. A repo-level file therefore hides your
user-level `person` and `billing` unless it repeats them.

The hooks look from the session's working directory. `btt report`,
`btt purge-unassigned` and `btt report --unassigned sessions` look from the
directory you run them in.

### Config file keys

```json
{
  "patterns": ["(?P<id>[A-Z][A-Z0-9]+-\\d+)", "(?P<id>#\\d+)"],
  "fallback": "unassigned",
  "uppercase": true,
  "person": "tester-a",
  "billing": "subscription"
}
```

| Key | Type | Default | What it does |
| --- | --- | --- | --- |
| `patterns` | list of regexes | JIRA-style ids, then `#123` | Pulls the ticket id out of the branch name. The list replaces the defaults; it doesn't add to them. See [Ticket patterns](#ticket-patterns). |
| `pattern` | regex | none | Shorthand for a `patterns` list with one entry. |
| `fallback` | string | `unassigned` | The ticket for a branch no pattern matches, a detached HEAD or a directory outside a git repo. |
| `uppercase` | boolean | `true` | Upper-cases the extracted id. Matching itself is always case-sensitive. |
| `person` | string | none | Labels your rows in CSV and JSON exports. See [Sharing an export](#sharing-an-export). |
| `billing` | `api` or `subscription` | guessed | States how Anthropic bills your sessions. See [Billing mode](#billing-mode). |

The plugin ignores keys it doesn't know. A `patterns` value that isn't a list,
or an empty `fallback`, gets the default instead. A `billing` value other than
`api` or `subscription` counts as unset.

Changing `fallback` affects new turns only. Turns already stored keep the name
the plugin stored them under, so `btt report` shows both names until you purge the old
one or it ages out of your windows.

### Environment variables

| Variable | Read by | What it does |
| --- | --- | --- |
| `BTT_CONFIG` | hooks, `btt` | Path of the config file. Beats both file lookups. If the file can't be read, the lookup carries on. |
| `BTT_PATTERN` | hooks, `btt ticket` | One regex that replaces `patterns` from any file. |
| `BTT_PERSON` | `btt report` | Beats `person` from the config file. |
| `BTT_DEBUG` | hooks | Set to `1` to print hook errors to stderr instead of discarding them. |
| `BTT_PYTHON` | `bin/btt` | The interpreter to run. Defaults to `python3`. |
| `PYENV_VERSION` | `bin/btt` | `bin/btt` sets it to `system` unless you set it, so a project's `.python-version` can't point it at a missing interpreter. |
| `ANTHROPIC_API_KEY`, `CLAUDE_CODE_USE_BEDROCK`, `CLAUDE_CODE_USE_VERTEX`, `CLAUDE_CODE_USE_FOUNDRY` | hooks | Any of them set makes the billing guess `api`. |
| `CLAUDE_PLUGIN_DATA` | hooks, `btt` | The data directory. Claude Code sets it for hooks; `--data-dir` beats it. |

A hook sees the environment Claude Code started with. To change a variable for
the hooks, set it before you launch `claude`, for example
`BTT_DEBUG=1 claude`.

### Ticket patterns

The plugin tries each pattern in order against the whole branch name. The first
match wins. It takes the group named `id` when the pattern has one, and the
whole match when it doesn't.

| Branch | Ticket |
| --- | --- |
| `feature/PROJ-412-add-login` | `PROJ-412` |
| `ENG-1234` | `ENG-1234` |
| `fix/#883-null-deref` | `#883` |
| `main` | `unassigned` |

The defaults keep the `#` in `#883`, so a GitHub issue never collides with a
bare number from another scheme.

Nothing in this path can raise an error. A malformed regex, an unreadable config
file, a detached HEAD or a directory outside a repo all give the fallback. This
code runs inside a hook, and a loud failure would interrupt your work.

To see which ticket a branch files under, with the config that applies in the
current directory, run `btt ticket`:

```bash
btt ticket                                # the branch checked out here
btt ticket feature/PROJ-412-add-login main
```

```
feature/PROJ-412-add-login -> PROJ-412
main -> unassigned
```

### Example: ClickUp ids

The default patterns assume ids that end in digits. ClickUp ids don't:
`CU-86e31q7e3` is base 36. The default JIRA pattern matches the prefix and stops
at the first letter, so the plugin files a branch called
`Add-target-count-CU-86e31q7e3` under `CU-86`. Two tasks can shorten to the same id, and their merged row
looks like a valid total. A `.branch-tokens.json` at the repo root fixes it:

```json
{
  "patterns": [
    "(?P<id>CU-[0-9a-z]+)",
    "(?P<id>[A-Z][A-Z0-9]+-\\d+)",
    "(?P<id>#\\d+)"
  ],
  "uppercase": false
}
```

| Branch | Default | With this config |
| --- | --- | --- |
| `Add-target-count-CU-86e31q7e3` | `CU-86` | `CU-86e31q7e3` |
| `dz/CU-8695abc12/wip` | `CU-8695` | `CU-8695abc12` |
| `PROJ-412-still-jira` | `PROJ-412` | `PROJ-412` |
| `fix-issue-#883` | `#883` | `#883` |

Three rules apply to any custom scheme:

- **Order matters.** The ClickUp pattern comes first. The JIRA pattern matches
  `CU-86` in the same branch, so if it ran first you would get the short id
  back.
- **`patterns` replaces the defaults.** That's why the JIRA and `#883` patterns
  appear again above. Leave them out only if the repo uses no other scheme.
- **`uppercase` changes the id, not the match.** With `true`, the plugin would
  store `CU-86E31Q7E3`, which ClickUp doesn't recognize, so the example sets
  `false`. A branch written `cu-86e31q7e3` then gets the fallback. If your
  branch casing varies, match both cases and normalize instead:

  ```json
  "patterns": ["(?P<id>(?i:cu)-[0-9a-z]+)", "(?P<id>[A-Z][A-Z0-9]+-\\d+)", "(?P<id>#\\d+)"],
  "uppercase": true
  ```

Try a pattern before you commit it. `BTT_PATTERN` replaces the configured
patterns for one command, and `uppercase` still applies:

```bash
BTT_PATTERN='(?P<id>CU-[0-9a-z]+)' btt ticket Add-target-count-CU-86e31q7e3
```

```
Add-target-count-CU-86e31q7e3 -> CU-86E31Q7E3
```

A new pattern only affects turns captured after you set it. Turns already stored
keep their ticket.

### Billing mode

Claude Code bills either per token, through an API account, or under a
subscription, and the transcript doesn't say which. Each session records a
`billing` value and where it came from:

- **config:** you set `"billing": "api"` or `"billing": "subscription"`. A
  stated value always wins.
- **hint:** the plugin guesses from the hook's environment. Any of the
  variables in the [table above](#environment-variables) means `api`; none
  means `subscription`. If you have a subscription and also export an API key
  for other tools, the guess is wrong, so set the value.

### Common setups

To put your name on every export and state your billing, write
`~/.claude/branch-tokens.json`:

```json
{ "person": "tester-a", "billing": "subscription" }
```

To use ClickUp ids in one repo and the defaults elsewhere, put the ClickUp
example in that repo's `.branch-tokens.json`. Repeat `person` and `billing`
there, because the repo file hides the user file.

To file work with no ticket under a clearer name:

```json
{ "fallback": "no-ticket" }
```

## Reporting

```bash
btt report                                  # every ticket, highest spend first
btt report PROJ-412                         # the sessions behind one ticket
btt report unassigned                       # what the work off any ticket was
btt report --unassigned sessions            # every ticket, with unassigned split by session
btt report --since 30d                      # a rolling window
btt report --since 2026-08-01 --until 2026-08-15   # a closed date range
btt report --by week                        # spend per week
btt report --by day --split ticket          # each ticket, day by day, with PR events
btt report --project my-repo                # one project only
btt report --format csv > tokens.csv        # or --format json
```

### Report options

| Option | Values | What it does |
| --- | --- | --- |
| `ticket` (positional) | a ticket id | Lists the sessions and branches behind that ticket instead of every ticket. The id must match exactly as stored. |
| `--since` | `30d`, `12h`, `2026-08-01`, `2026-08-01T09:30` | Starts the window. See [Time ranges](#time-ranges). |
| `--until` | the same forms | Ends the window. A bare date includes that whole day. |
| `--by` | `day`, `week`, `month` | Groups by local calendar period instead of by ticket. Weeks start on Monday. |
| `--split` | `ticket` | With `--by`, gives one row per ticket per period. |
| `--project` | a directory name | Keeps turns from one project, which is the base name of the session's working directory. |
| `--unassigned` | `bucket` (default), `sessions` | Shows work with no ticket as one row or as one row per session. See [Work with no ticket](#work-with-no-ticket). |
| `--format` | `markdown` (default), `csv`, `json` | The output format. |
| `--include-text` | flag | Adds session titles and first prompts to CSV and JSON. See [Sharing an export](#sharing-an-export). |
| `--data-dir` | a path | Reads this data directory instead of finding one. |

### Report columns

Besides the token counts, each ticket row shows:

| Column | What it says |
| --- | --- |
| `est. USD` | Every turn priced at its own model's rates. A `*` means part of the spend was on a model with no known price, so the figure is low. |
| `models` | Each model's share of the weighted tokens, for example `opus-5-5 70%, opus-5 30%`. |
| `cost shape` | Model calls per turn and context tokens per call. Each call re-sends the whole context, so a costly turn has either many calls or a large context, and each needs a different fix. |
| `idle` | Gaps of over 5 minutes between model calls, and the cache writes the next call made. A large figure means idle time is making the prompt cache rebuild. |
| `active` | Working time, with each idle gap counted as 5 minutes at most. |
| `PRs` | Pull requests from the ticket's branches, how many merged and the lines added and removed. `btt enrich` fills this column. |

A dash means the plugin didn't record that figure for those turns. Turns
captured before version 0.7.0 have no cost shape or idle data. `btt backfill`
can fill them in, but only while their transcripts are still on disk; Claude
Code deletes transcripts after `cleanupPeriodDays`, 30 days by default.

### Work with no ticket

A turn on a branch that no pattern matches goes under the fallback ticket,
`unassigned` unless you renamed it. You have three ways to see what that work
was.

`btt report unassigned` lists its sessions, each with a title and repo. The
title is the one Claude Code gave the session. Claude Code titles interactive
sessions only, so an Agent SDK run or a Claude Desktop session shows its first
prompt instead, in quotes. A slash command shows as typed, `/name args`. A local
command such as `/clear` that got no response never counts as the first prompt.

`btt report --unassigned sessions` puts the same breakdown into the ticket list.
Each session gets its own row, named `unassigned: <title> (<session id>)` and
ranked among the tickets by its spend. Real tickets stay one row each, and the
totals don't change. It also works with `--by <period> --split ticket`. In CSV
and JSON the `ticket` column still reads `unassigned`, and a `session_id` column
tells the rows apart.

`btt purge-unassigned` deletes the work instead. See
[Purging work with no ticket](#purging-work-with-no-ticket).

### Time ranges

`--since` and `--until` each take a relative window, a date or a date and time:

```bash
btt report --since 30d                       # the last 30 days; 12h also works
btt report --since 2026-08-01                # from the start of August 1
btt report --until 2026-08-15                # through the end of August 15
btt report --since 2026-08-01 --until 2026-08-15   # a closed range
btt report --since 2026-08-01T09:30          # to the minute
```

A bare `--until` date covers the whole day it names, so the closed range above
includes all of August 15.

Dates and times are local. The plugin stores timestamps in UTC, and the two can
disagree about which day a late session belongs to: the plugin stores a 9 pm session
in UTC-3 under the next UTC date. So the plugin reads bounds and buckets in your
time zone, and `--since 2026-08-01` means your August 1. Relative windows such
as `30d` don't depend on time zone.

If a bound doesn't parse, the report says so and ignores it. It names each bad
bound, and a good bound beside it still applies. The report never widens the
window to all time under a header that claims a range.

### Grouping over time

`--by` shows spend over time instead of one total per ticket:

```bash
btt report --by day                    # or week, or month
btt report --by week --since 2026-08-01
btt report PROJ-412 --by day           # one ticket, day by day
btt report --by day --split ticket     # one row per ticket per day
```

```
# Tokens by day

| day | turns | sessions | tickets | output | cache read | weighted | est. USD | raw total | active |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 2026-08-31 | 4 | 1 | 1 | 52,724 | 5,876,275 | 1,129,946 | $5.65 | 6,068,415 | 13m 27s |
| 2026-09-01 | 22 | 4 | 3 | 258,130 | 50,643,629 | 8,352,460 | $41.45 | 51,987,333 | 1h 9m |
```

Without a ticket, each row also counts the tickets worked in that period. With
one, the table covers that ticket alone. CSV and JSON keep the `period` column,
so a time series exports as is.

`active` is working time, not the span from the first timestamp to the last.
Capture counts each idle gap inside a turn as 5 minutes at most, so a turn left
open overnight adds its real work rather than a day. Capture leaves subagent
time out, because it runs inside a main turn that already counts those minutes.

`--split ticket` adds a `PR events` column that marks the periods in which a
ticket's PR opened or merged. That lines spend up against delivery: how much
went in before the PR opened, and how much after.

### Pull request outcomes

```bash
btt enrich                 # every recorded branch
btt enrich --since 30d     # only branches worked on in the window
```

`btt enrich` asks the GitHub CLI for the pull requests opened from each recorded
branch. It stores their state, merge date, lines added and removed, reviews,
reviews that requested changes and reopens. It needs `gh` on your `PATH` and
logged in (`gh auth login`). It refreshes every PR it finds, so run it again
before each report you share. `--quiet` drops the per-branch lines, and
`--data-dir` works as it does for `btt report`.

No hook runs it, because it needs the network and takes a second or more per
branch. It skips `main`, `master`, `develop` and `trunk`, which carry everyone's
PRs, and branches with no recorded repo.

### Sharing an export

CSV and JSON rows end with columns that say whose data they hold and what they
cover: `person`, `btt_version`, `schema_version`, `window_since`,
`window_until` and `generated_at`. They repeat on every row, so you can stack
exports from several people in one sheet and still tell them apart. `person`
comes from the config file or `$BTT_PERSON`, never from your git identity.

Session titles and first prompts show on screen but stay out of CSV and JSON
unless you pass `--include-text`. An export leaves your machine, and a prompt
can hold secrets or customer data.

> **Warning:** read an `--include-text` export before you send it.

## Live totals for a statusline

`current.json` in the data directory holds the latest session's totals, for a
statusline or a script:

```json
{
  "ticket": "PROJ-412",
  "branch": "feature/PROJ-412-add-login",
  "project": "my-repo",
  "session_id": "0b6c2f1e-…",
  "session_tokens": 1840221,
  "session_weighted": 312904,
  "session_turns": 9,
  "ticket_tokens": 9530715,
  "ticket_weighted": 1677245,
  "ticket_turns": 41,
  "ticket_sessions": 5,
  "updated_at": "2026-08-08T14:22:03.512944+00:00"
}
```

The plugin rewrites it on every `Stop` and at `SessionEnd`. Token fields are
raw; the `_weighted` fields are what reports rank on. For example, a statusline
command can print the ticket and its weighted total:

```bash
jq -r '"\(.ticket) \(.ticket_weighted)"' ~/.claude/plugins/data/branch-token-tracker-agent-toolbox/current.json
```

Claude Code doesn't show the `SessionEnd` summary, so this file is the way to see
a live total. The `SessionStart` line is the one total you see in a session
without asking.

## Maintenance

### Repairing an existing store

```bash
btt backfill           # re-read every captured session's transcript
btt backfill --quiet   # without the per-session lines
```

`btt backfill` rebuilds turns from the transcripts still on disk, so it
recovers spend an older parser couldn't see. Counts only ever grow, running it
twice changes nothing more and a session whose transcript is gone keeps what was
captured at the time.

It doesn't resolve branches again. The stored ticket is the record of where the
work ran, and a session can span several branches. A recovered turn takes the
ticket of the latest stored turn that started no later than it did, which is the
branch checked out while it ran.

Run `btt backfill` after you upgrade the plugin. Earlier versions missed some
subagent spend, so a database they wrote understates its tickets. The
[changelog](CHANGELOG.md) lists which versions.

### Purging work with no ticket

```bash
btt purge-unassigned         # print what would go
btt purge-unassigned --yes   # delete it
```

The command deletes every turn stored under the fallback ticket, and the session
rows, with their titles and first prompts, of sessions left with no other turns.
Without `--yes` it only prints the counts. It reads the fallback name from the
config in the directory you run it in. Capture carries on, so new work with no
ticket is still recorded.

> **Warning:** you can't rebuild purged turns from the transcripts. With
> `--yes`, the command first copies the database to `tokens.db.bak-<timestamp>`
> in the data directory, and restoring that copy is the only way back.

The purge keeps each deleted turn's id in a `purged_turns` table, and both the
hooks and `btt backfill` skip those ids. Without it, resuming a purged session
on a ticket branch would store its old turns again under that ticket.

## Where the data lives

`~/.claude/plugins/data/branch-token-tracker-<marketplace>/tokens.db`, beside
`current.json`.

Claude Code gives hooks a `${CLAUDE_PLUGIN_DATA}` named after the install
source: `-agent-toolbox` for a marketplace install and `-inline` for
`--plugin-dir`. A skill's shell doesn't get that variable, so `btt` scans the
sibling directories and reads the one with the most turns. `--data-dir`
overrides the search. See `_discover_populated_dir` in `scripts/db.py`.

## Troubleshooting

- **No banner at session start:** the banner appears only once the current
  ticket has spend. A new ticket shows nothing until its first `Stop`.
- **A ticket is missing or truncated:** run `btt ticket <branch>` to see what
  your patterns extract, and try a fix with `BTT_PATTERN` as shown in
  [Example: ClickUp ids](#example-clickup-ids).
- **Capture records nothing:** start Claude Code with `BTT_DEBUG=1 claude`. The
  hooks then print their errors to stderr instead of discarding them.
- **`btt` reports an empty store:** you may be reading another data directory.
  Pass `--data-dir` with the path from [Where the data lives](#where-the-data-lives).

## Relationship to perf-tracker

`perf-tracker` is a sibling plugin in the same marketplace, independent by
design: it has its own database and data directory and shares no code.
`perf-tracker` answers which approach is cheapest per successful outcome, and
asks you to mark tasks with `/track` and `/track-done`. This plugin answers what
a ticket cost, and asks nothing of you. Install either or both.

## Layout

```
branch-token-tracker/
├── .claude-plugin/plugin.json
├── hooks/hooks.json                 # SessionStart, Stop, SessionEnd
├── bin/btt                          # launcher: ingest | report | backfill | purge-unassigned | enrich | ticket
├── scripts/
│   ├── db.py                        # data directory, schema and migrations
│   ├── schema.sql                   # turns, sessions, prs and purged_turns
│   ├── config.py                    # config lookup, branch to ticket id, btt ticket
│   ├── transcript.py                # turns and token usage from transcripts
│   ├── ingest.py                    # hook entry point
│   ├── maintenance.py               # backfill and purge-unassigned
│   ├── enrich.py                    # PR outcomes from gh
│   └── report.py                    # markdown, csv and json
├── skills/token-report/SKILL.md     # /token-report
└── tests/
```

The scripts use only the standard library and the Python 3.9 grammar.

## Tests

```bash
cd plugins/branch-token-tracker
python3 -m unittest discover -s tests
```

`scripts/check.sh` at the repo root also runs `claude plugin validate` and both
plugins' suites.

## Changelog

See [CHANGELOG.md](CHANGELOG.md).
