# branch-token-tracker

Attribute token spend to the task-tracker id in your git branch name.

You are already bracketing your work: a branch called `feature/PROJ-412-add-login` says
exactly which ticket the next few hours belong to. This plugin reads that, counts the tokens
every turn costs, and rolls them up per ticket. No `/start`, no `/stop`, no rubric, no judge.

```
$ btt report
# Tokens by ticket

**3 ticket(s) · 12,481,903 tokens total**

| ticket | turns | sessions | input | output | cache read | cache create | weighted | est. USD | raw total | elapsed | last seen |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| PROJ-412 | 41 | 5 | 612 | 214,880 | 8,904,221 | 411,002 | 9,530,715 | 6h 12m | 2026-08-08T14:22 |
| #883 | 12 | 2 | 98 | 61,004 | 2,401,880 | 96,441 | 2,559,423 | 1h 48m | 2026-08-07T18:05 |
| unassigned | 4 | 1 | 22 | 18,330 | 361,003 | 12,410 | 391,765 | 22m 4s | 2026-08-05T09:11 |
```

## Install

```bash
/plugin marketplace add /path/to/agent-toolbox
/plugin install branch-token-tracker@agent-toolbox
```

Capture starts immediately. `/token-report` (or `btt report`) reads it back.

## How it works

Three hooks, one table, no state machine.

| hook | what it does |
| --- | --- |
| `SessionStart` | initializes the DB and prints the current ticket's running total into session context |
| `Stop` | resolves the branch **now**, inserts any turns not yet stored |
| `SessionEnd` | final capture, then writes `current.json` and echoes a session summary |

The branch is resolved at every `Stop`, not once per session. Switch branches mid-session and
the turns before the switch stay on the old ticket while everything after lands on the new one
— which is why `Stop` is used at all rather than capturing only at `SessionEnd`.

Turns are keyed by the transcript's user-message uuid and inserted only if absent, so
re-reading the transcript after every turn is idempotent and a turn's ticket is pinned to the
branch it actually ran on.

Everything reported is a `GROUP BY` at read time. There is nothing pre-aggregated to fall out
of sync, and no in-flight state for a killed session to corrupt.

### What counts as a turn

One user prompt plus the assistant messages answering it. Tool results are `type=user` records
too, and are excluded — counting them would inflate the turn count several-fold. Assistant
records are deduped by `message.id` (last wins), because the same message id repeats as it
streams and each copy carries the cumulative usage.

Subagent tokens are included: they are spent on the ticket like any other. They are read
from the `Agent` tool's `toolUseResult` and stored as their own rows (`query_source =
'subagent'`, keyed `agent:<agentId>`). This used to rely on sidechain records, which do not
exist in real transcripts — so in practice subagent spend went unbilled to the ticket
entirely.

### Weighted vs raw tokens

The headline figure is **weighted** tokens: input-equivalent units, since cache reads bill at
0.1x input, cache writes at 1.25x (5m) / 2x (1h) and output at 5x. A raw sum of the four
classes is ~95% cache reads, so ranking tickets by it ranks them by how long their sessions
were rather than by what they cost. The raw total is still shown, but it is not a cost.

### Live totals for a statusline

`current.json` in the plugin's data dir is rewritten on **every** `Stop` (not only at
`SessionEnd`, which meant a statusline spent each session showing the previous one's
numbers), and carries a `updated_at` stamped at write time.

## Configuring the ticket pattern

Ticket extraction is the entire customization surface. Config lookup, first hit wins:

1. `$BTT_CONFIG` — explicit file path
2. `.branch-tokens.json` — walking up from the session's cwd
3. `~/.claude/branch-tokens.json` — user-level default
4. built-in defaults

```json
{
  "patterns": ["(?P<id>[A-Z][A-Z0-9]+-\\d+)", "(?P<id>#\\d+)"],
  "fallback": "unassigned",
  "uppercase": true
}
```

Patterns are tried in order against the full branch name; the first that matches wins, taking
the `id` named group when present and the whole match otherwise. `pattern` (a bare string) is
accepted as a synonym for a one-element `patterns`. `$BTT_PATTERN` overrides all of it for a
one-off run.

| branch | ticket |
| --- | --- |
| `feature/PROJ-412-add-login` | `PROJ-412` |
| `ENG-1234` | `ENG-1234` |
| `fix/#883-null-deref` | `#883` |
| `main` | `unassigned` |

Nothing here can raise: a malformed regex, an unreadable config, a detached HEAD, or a cwd
outside a repo all degrade to the fallback. This code runs inside a session hook, so failing
loudly would mean interrupting real work.

### Worked example: ClickUp ids

The default patterns assume a tracker whose ids end in digits. ClickUp's do not —
`CU-86e31q7e3` is base36 — and the failure is not a clean miss. `[A-Z][A-Z0-9]+-\d+` matches
the *prefix* of such an id and stops at the first letter, so a branch called
`Add-target-count-CU-86e31q7e3` is silently filed under `CU-86`. Two different tasks can
truncate to the same id and have their costs merged into one row, which looks like a real
total rather than a bug. Repo-level `.branch-tokens.json`:

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

| branch | default | with the config above |
| --- | --- | --- |
| `Add-target-count-CU-86e31q7e3` | `CU-86` | `CU-86e31q7e3` |
| `dz/CU-8695abc12/wip` | `CU-8695` | `CU-8695abc12` |
| `PROJ-412-still-jira` | `PROJ-412` | `PROJ-412` |
| `fix-issue-#883` | `#883` | `#883` |

Three things that are easy to get wrong here, all of which apply to any custom scheme:

- **Order matters.** The ClickUp pattern has to come *first*. First match wins, and the JIRA
  pattern happily matches `CU-86` out of the same branch — put it second and you get the
  truncated id back.
- **`patterns` replaces the defaults, it does not extend them.** The layered config is a dict
  update, which is why JIRA and `#883` are repeated above. Drop them only if the repo has no
  other scheme.
- **`uppercase` applies to the extracted id, not to the branch.** Matching is always
  case-sensitive against the raw branch name. `true` would store `CU-86E31Q7E3`, which is not
  what you paste back into ClickUp — hence `false` here. The tradeoff is that a branch written
  `cu-86e31q7e3` then falls to the fallback. If branch casing varies, match either and
  normalise instead:

  ```json
  "patterns": ["(?P<id>(?i:cu)-[0-9a-z]+)", "(?P<id>[A-Z][A-Z0-9]+-\\d+)", "(?P<id>#\\d+)"],
  "uppercase": true
  ```

Try a pattern before committing it — `$BTT_PATTERN` beats every config file:

```bash
BTT_PATTERN='(?P<id>CU-[0-9a-z]+)' btt report
```

## Reporting

```bash
btt report                             # every ticket, biggest first
btt report PROJ-412                    # the sessions and branches behind one ticket
btt report --since 30d                 # windowed (<n>d / <n>h)
btt report --project my-repo           # one repo only
btt report --format csv > tokens.csv   # or --format json
```

An unparsable `--since` is reported as ignored rather than silently widening the query to all
time.

`<data-dir>/current.json` holds the latest session's totals for a statusline or a script:

```json
{
  "ticket": "PROJ-412",
  "branch": "feature/PROJ-412-add-login",
  "session_tokens": 1840221,
  "ticket_tokens": 9530715,
  "ticket_sessions": 5
}
```

SessionEnd hook stdout is not surfaced in the transcript, so this file — not the echoed
summary — is what makes the live total reachable. The SessionStart line is the part you see
in-session.

## Where the data lives

`~/.claude/plugins/data/branch-token-tracker*/tokens.db`.

Claude Code hands hooks a `${CLAUDE_PLUGIN_DATA}` suffixed with the install source
(`…-agent-toolbox` for a marketplace install, `…-inline` for `--plugin-dir`), and the shell a
skill runs in does not inherit that variable. So the read side scans the sibling directories
and picks the populated one rather than guessing the unsuffixed name — see
`_discover_populated_dir` in `scripts/db.py`.

## Relationship to claude-performance-tracker

Sibling plugin in the same marketplace, deliberately independent — separate database, separate
data dir, no shared code. `claude-performance-tracker` answers "which *approach* is cheapest
per successful outcome", and pays for it with explicit `/track` … `/track-done` bracketing, a
rubric, and a judge subagent. This one answers "what did ticket X cost" and asks nothing of
you. Install either, or both.

## Layout

```
branch-token-tracker/
├── .claude-plugin/plugin.json
├── hooks/hooks.json                 # SessionStart, Stop, SessionEnd
├── bin/btt                          # launcher/multiplexer: ingest | report (also on PATH)
├── scripts/
│   ├── db.py                        # data dir resolution + schema init
│   ├── schema.sql                   # one table: turns
│   ├── config.py                    # branch -> ticket id
│   ├── transcript.py                # turn + token-usage extraction
│   ├── ingest.py                    # hook entrypoint
│   └── report.py                    # markdown | csv | json
├── skills/token-report/SKILL.md     # /token-report
└── tests/
```

`bin/btt` forces pyenv's `system` interpreter, because a project pinning an uninstalled version
via `.python-version` would otherwise make a bare `python3` fail before any of this code runs.
Override with `BTT_PYTHON`. The scripts are stdlib-only and run on any Python 3.9+.

## Tests

```bash
cd plugins/branch-token-tracker
python3 -m unittest discover -s tests
```


## ⚠ Uninstall deletes your captured data

`claude plugin uninstall` removes `${CLAUDE_PLUGIN_DATA}` — including `tokens.db` and every
ticket total in it. Back it up outside that directory first:

```bash
cp ~/.claude/plugins/data/branch-token-tracker-*/tokens.db ~/tokens.db.bak
```

Prefer `claude plugin update` (with a version bump) over uninstall/reinstall.
