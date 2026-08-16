---
name: token-report
description: Report token spend grouped by the task-tracker id in your git branch names. Use to answer "how many tokens did ticket X cost", to see spend per ticket over a window, or to export those totals as CSV/JSON for a spreadsheet or the real tracker.
---

# /token-report

Reports what `branch-token-tracker` has captured. Capture is automatic — the plugin's hooks
record every turn against the ticket id in the branch that was checked out at the time, so
there is nothing to start or stop.

## Steps

1. Work out what the user is asking for:
   - **no argument** → all tickets, biggest spender first
   - **a ticket id** (`PROJ-412`, `#883`) → drill into that ticket's sessions and branches
   - **a window** ("this month", "last week") → `--since 30d` / `--since 7d`
     (only `<n>d` and `<n>h` parse; anything else is reported as ignored, not silently dropped)
   - **"export"/"csv"/"json"/"for a spreadsheet"** → `--format csv` or `--format json`

2. Run it:

   ```bash
   btt report                                  # every ticket
   btt report PROJ-412                         # one ticket's sessions
   btt report --since 30d                      # windowed
   btt report --project my-repo                # one repo only
   btt report --format csv > tokens.csv        # export
   ```

3. Present the table as-is — it is already markdown. Add one line of interpretation: which
   ticket dominates, and whether cache-read (usually the bulk) or output tokens drive it.

## Ticket ids come from the branch name

Extraction is regex-driven and configurable. Defaults match `PROJ-412`-style ids and `#883`
issue refs; anything unmatched (e.g. work on `main`) rolls up under `unassigned`.

To customize, write `.branch-tokens.json` at the repo root (or `~/.claude/branch-tokens.json`
for a global default):

```json
{
  "patterns": ["(?P<id>[A-Z][A-Z0-9]+-\\d+)"],
  "fallback": "unassigned",
  "uppercase": true
}
```

Patterns are tried in order; the first match wins, using the `id` named group when present.
`$BTT_PATTERN` overrides everything for a one-off run.

If the user's ids aren't being picked up, check the branch name against the pattern before
assuming capture is broken — `btt report` shows what actually got extracted.

## Fallback if `btt` is not on PATH

Resolve the bundled script directly:

```bash
ls -t ~/.claude/plugins/cache/*/branch-token-tracker/*/scripts/report.py | head -1
```

and call it with `python3`, passing the same flags.
