# branch-token-tracker

Attributes token spend to the task-tracker id in your git branch name.

Your branch name already names the ticket: `feature/PROJ-412-add-login` says
exactly which ticket the next few hours belong to. This plugin reads that,
counts the tokens every turn costs, and rolls them up per ticket. No `/start`,
no `/stop`, no rubric, no judge.

```
$ btt report
# Tokens by ticket

**3 ticket(s) · 12,481,903 weighted tokens (66,035,157 raw)**

| ticket | turns | sessions | input | output | cache read | cache create | weighted | est. USD | raw total | elapsed | last seen |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| PROJ-412 | 41 | 5 | 612 | 214,880 | 8,904,221 | 411,002 | 9,530,715 | $47.65 | 42,109,882 | 6h 12m | 2026-08-08T14:22 |
| #883 | 12 | 2 | 98 | 61,004 | 2,401,880 | 96,441 | 2,559,423 | $12.79 | 18,441,004 | 1h 48m | 2026-08-07T18:05 |
| unassigned | 4 | 1 | 22 | 18,330 | 361,003 | 12,410 | 391,765 | $1.95 | 5,484,271 | 22m 4s | 2026-08-05T09:11 |
```

## Install

```bash
/plugin marketplace add /path/to/agent-toolbox
/plugin install branch-token-tracker@agent-toolbox
```

Capture starts immediately. Read it back with `/token-report`, or `btt report`
from a shell.

> **Warning:** `claude plugin uninstall` deletes `${CLAUDE_PLUGIN_DATA}`,
> including `tokens.db` and every ticket total in it. Back it up outside that
> directory first:
>
> ```bash
> cp ~/.claude/plugins/data/branch-token-tracker-*/tokens.db ~/tokens.db.bak
> ```
>
> Prefer `claude plugin update` with a version bump over uninstall and
> reinstall. An update preserves the data directory.

## How it works

Three hooks, one table, no state machine.

| Hook | What it does |
| --- | --- |
| `SessionStart` | Initializes the database and prints the current ticket's running total into session context. |
| `Stop` | Resolves the branch, then inserts any turns not yet stored. |
| `SessionEnd` | Captures a final time, writes `current.json`, and echoes a session summary. |

The plugin resolves the branch at every `Stop`, not once per session. Switch
branches mid-session and the turns before the switch stay on the old ticket
while everything after lands on the new one. That is the reason `Stop` exists
here at all, rather than capturing only at `SessionEnd`.

Turns are keyed by the transcript's user-message uuid and inserted only if
absent. Re-reading the transcript after every turn is therefore idempotent, and
a turn's ticket stays pinned to the branch it actually ran on.

Every reported figure is a `GROUP BY` at read time. Nothing is pre-aggregated,
so nothing can fall out of sync, and a killed session has no in-flight state to
corrupt.

### What counts as a turn

One user prompt plus the assistant messages answering it. Tool results are
`type=user` records too, and the parser excludes them. Counting them would
inflate the turn count several-fold. Assistant records are deduplicated by
`message.id`, last one winning, because the same message id repeats as it
streams and each copy carries the cumulative usage.

### Subagent spend

Subagent tokens count against the ticket like any other. A completed agent
reports its usage in the `Agent` tool's `toolUseResult`, and the plugin stores
each subagent as its own row, with `query_source = 'subagent'` and keyed
`agent:<agentId>`.

An async agent's tool result carries no usage at all, just an `agentId` and a
status. Its real envelope comes from its own transcript, which sits beside the
session's:

```
<projects>/<slug>/<session-id>.jsonl                        # the session
<projects>/<slug>/<session-id>/subagents/agent-<id>.jsonl   # one per agent
```

Those logs are also how the plugin discovers agents. An agent that left a log
spent tokens whether or not the main transcript ever mentioned it, so capture
does not depend on a notification arriving, or on the parser recognizing the
shape it arrives in. That matters because notifications are the fragile part:
one arrives on any of three record shapes, `type=user`, `type=attachment` or
`type=queue-operation`.

The plugin ranks the evidence and keeps the best source. Better evidence
replaces the earlier figure rather than adding to it, since it measures the same
spend more precisely:

| Rank | Source | What it gives |
| --- | --- | --- |
| 3 | The agent's own log | The full per-class split |
| 2 | A completed `Agent` tool result | Real usage, when present |
| 1 | `<subagent_tokens>` in a notification | One bare number |

Only the bare number cannot be weighted. Output bills 5x input and cache writes
1.25x, so a single total cannot be split by guesswork. It lands in
`total_tokens_agg`, counts toward the raw total, and stays out of the weighted
figure.

### Weighted compared with raw tokens

The headline figure is weighted tokens: input-equivalent units, since cache
reads bill at 0.1x input, cache writes at 1.25x for a 5-minute TTL or 2x for an
hour, and output at 5x. A raw sum of the four classes is about 95% cache reads,
so ranking tickets by it ranks them by how long their sessions were rather than
by what they cost. The report still shows the raw total, but it is not a cost.

### Live totals for a statusline

`current.json` in the plugin's data directory carries the latest session's
totals for a statusline or a script:

```json
{
  "ticket": "PROJ-412",
  "branch": "feature/PROJ-412-add-login",
  "session_tokens": 1840221,
  "ticket_tokens": 9530715,
  "ticket_sessions": 5
}
```

The plugin rewrites it on every `Stop`, and stamps `updated_at` at write time.
This file, not the echoed summary, is what makes the live total reachable:
Claude Code does not surface `SessionEnd` hook stdout in the transcript. The
`SessionStart` line is the part you see in-session.

## Configuring the ticket pattern

Ticket extraction is the entire customization surface. The plugin looks for a
config in this order and takes the first hit:

1. `$BTT_CONFIG`, an explicit file path
2. `.branch-tokens.json`, walking up from the session's working directory
3. `~/.claude/branch-tokens.json`, a user-level default
4. Built-in defaults

```json
{
  "patterns": ["(?P<id>[A-Z][A-Z0-9]+-\\d+)", "(?P<id>#\\d+)"],
  "fallback": "unassigned",
  "uppercase": true
}
```

The plugin tries each pattern in order against the full branch name. The first
match wins, taking the `id` named group when present and the whole match
otherwise. `pattern`, a bare string, is a synonym for a one-element `patterns`.
`$BTT_PATTERN` overrides every config file for a one-off run.

| Branch | Ticket |
| --- | --- |
| `feature/PROJ-412-add-login` | `PROJ-412` |
| `ENG-1234` | `ENG-1234` |
| `fix/#883-null-deref` | `#883` |
| `main` | `unassigned` |

Nothing here can raise. A malformed regex, an unreadable config, a detached
HEAD, or a working directory outside a repo all degrade to the fallback. This
code runs inside a session hook, so failing loudly would interrupt real work.

### Worked example: ClickUp ids

The default patterns assume a tracker whose ids end in digits. ClickUp's do not.
`CU-86e31q7e3` is base36, and the failure is not a clean miss:
`[A-Z][A-Z0-9]+-\d+` matches the prefix of such an id and stops at the first
letter, so a branch called `Add-target-count-CU-86e31q7e3` is filed under
`CU-86`. Two different tasks can truncate to the same id and have their costs
merged, and the merged row looks like a valid total. A repo-level
`.branch-tokens.json` fixes it:

```json
{
  "patterns": [
    "(?P<id>CU-[0-9a-z]+)",
    "(?P<id>[A-Z][A-Z0-9]+-\\d+)",
    "(?P<id>#\\d+)"
  ],
  "fallback": "unassigned",
  "uppercase": false
}
```

| Branch | Default | With the config above |
| --- | --- | --- |
| `Add-target-count-CU-86e31q7e3` | `CU-86` | `CU-86e31q7e3` |
| `dz/CU-8695abc12/wip` | `CU-8695` | `CU-8695abc12` |
| `PROJ-412-still-jira` | `PROJ-412` | `PROJ-412` |
| `fix-issue-#883` | `#883` | `#883` |

Three things are easy to get wrong here, and all three apply to any custom
scheme:

- **Order matters.** The ClickUp pattern has to come first. The first match
  wins, and the JIRA pattern matches `CU-86` out of the same branch. Put it
  second and you get the truncated id back.
- **`patterns` replaces the defaults rather than extending them.** The layered
  config is a dict update, which is why JIRA and `#883` are repeated above. Drop
  them only if the repo has no other scheme.
- **`uppercase` applies to the extracted id, not to the branch.** Matching is
  always case-sensitive against the raw branch name. `true` would store
  `CU-86E31Q7E3`, which is not what you paste back into ClickUp, so this example
  sets `false`. The tradeoff is that a branch written `cu-86e31q7e3` then falls
  to the fallback. If branch casing varies, match either case and normalize
  instead:

  ```json
  "patterns": ["(?P<id>(?i:cu)-[0-9a-z]+)", "(?P<id>[A-Z][A-Z0-9]+-\\d+)", "(?P<id>#\\d+)"],
  "uppercase": true
  ```

Try a pattern before committing it. `$BTT_PATTERN` beats every config file:

```bash
BTT_PATTERN='(?P<id>CU-[0-9a-z]+)' btt report
```

## Reporting

```bash
btt report                             # every ticket, biggest first
btt report PROJ-412                    # the sessions and branches behind one ticket
btt report --project my-repo           # one repo only
btt report --format csv > tokens.csv   # or --format json
```

### Time ranges

`--since` and `--until` each accept a relative window, an absolute date, or an
absolute datetime:

```bash
btt report --since 30d                       # relative: <n>d / <n>h
btt report --since 2026-08-01                # from the start of Aug 1
btt report --until 2026-08-15                # through the END of Aug 15
btt report --since 2026-08-01 --until 2026-08-15   # a closed range
btt report --since 2026-08-01T09:30          # to the minute
```

A bare `--until` date covers the whole day named, so
`--since 2026-08-01 --until 2026-08-15` is the range you meant rather than
fourteen days and a truncated fifteenth.

Absolute values are local. Timestamps are stored in UTC, and the two disagree
about which day a late-evening session belongs to: a 9pm session in UTC−3 is
stored under the next UTC date. The plugin therefore interprets bounds and
buckets in your timezone, so `--since 2026-08-01` means your August 1st.
Relative windows such as `30d` are unaffected either way.

An unparsable bound is reported as ignored, rather than silently widening the
query to all time under a header that claims otherwise. Each bad bound is named
individually, and a good one alongside it still applies.

### Grouping over time

`--by` groups by local calendar period instead of by ticket, giving spend over
time rather than a single total:

```bash
btt report --by day                    # or week (Monday-start) | month
btt report --by week --since 2026-08-01
btt report PROJ-412 --by day           # one ticket's spend, day by day
```

```
# Tokens by day

| day | turns | sessions | tickets | output | cache read | weighted | est. USD | raw total | active |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 2026-08-31 | 4 | 1 | 1 | 52,724 | 5,876,275 | 1,129,946 | $5.65 | 6,068,415 | 30m 21s |
| 2026-09-01 | 15 | 2 | 2 | 133,358 | 31,036,125 | 4,900,210 | $24.50 | 31,973,691 | 1h 12m |
```

Without a ticket argument, each row also counts the distinct tickets worked that
period. With one, the table covers that ticket alone. `--format csv|json`
carries the `period` column through, so a spend-over-time series exports
directly.

The dollar figure prices each bucket on the model that produced most of its
output, so a bucket that mixed models is an approximation. That is why the exact
raw and weighted columns sit beside it.

## Repairing an existing store

```bash
btt backfill        # re-read every captured session's transcript
```

`backfill` re-derives turns from the transcripts still on disk, so spend an
older parser could not see is recovered rather than lost. Counts only ever grow,
it is idempotent, and a session whose transcript has been deleted keeps whatever
was captured at the time.

It does not re-resolve the branch. A session can span several branches, which is
the whole point of resolving at every `Stop`, so the stored ticket is the
contemporaneous record of where the work actually ran. A recovered turn instead
inherits the ticket of the most recent stored turn that started no later than it
did, which puts it on the branch that was checked out while it ran.

Run `btt backfill` after upgrading the plugin. Earlier versions could not see
every subagent's spend, so a database written by one understates its ticket
totals. See [CHANGELOG.md](CHANGELOG.md) for which versions were affected.

## Where the data lives

`~/.claude/plugins/data/branch-token-tracker*/tokens.db`.

Claude Code hands hooks a `${CLAUDE_PLUGIN_DATA}` suffixed with the install
source: `…-agent-toolbox` for a marketplace install, `…-inline` for
`--plugin-dir`. The shell a skill runs in does not inherit that variable, so the
read side scans the sibling directories and picks the populated one rather than
guessing the unsuffixed name. See `_discover_populated_dir` in `scripts/db.py`.

## Relationship to claude-performance-tracker

A sibling plugin in the same marketplace, deliberately independent: separate
database, separate data directory, no shared code.

`claude-performance-tracker` answers which approach is cheapest per successful
outcome, and pays for it with explicit `/track` and `/track-done` bracketing, a
rubric, and a judge subagent. This one answers what ticket X cost, and asks
nothing of you. Install either, or both.

## Layout

```
branch-token-tracker/
├── .claude-plugin/plugin.json
├── hooks/hooks.json                 # SessionStart, Stop, SessionEnd
├── bin/btt                          # launcher: ingest | report | backfill (on PATH)
├── scripts/
│   ├── db.py                        # data dir resolution + schema init
│   ├── schema.sql                   # one table: turns
│   ├── config.py                    # branch to ticket id
│   ├── transcript.py                # turn + token-usage extraction
│   ├── ingest.py                    # hook entrypoint
│   ├── maintenance.py               # backfill: re-derive turns from transcripts
│   └── report.py                    # markdown | csv | json
├── skills/token-report/SKILL.md     # /token-report
└── tests/
```

`bin/btt` forces pyenv's `system` interpreter. Without that, a project pinning
an uninstalled version through `.python-version` would make a bare `python3`
fail before any of this code runs. Override it with `BTT_PYTHON`. The scripts
use only the standard library and run on any Python 3.9 or later.

## Tests

```bash
cd plugins/branch-token-tracker
python3 -m unittest discover -s tests
```

## Changelog

See [CHANGELOG.md](CHANGELOG.md).
