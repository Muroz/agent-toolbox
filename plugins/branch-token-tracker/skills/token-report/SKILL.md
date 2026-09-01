---
name: token-report
description: Report token spend grouped by the task-tracker id in your git branch names. Use to answer "how many tokens did ticket X cost", to see spend in a date range or per day/week/month, or to export those totals as CSV/JSON for a spreadsheet or the real tracker.
---

# /token-report

Reports what `branch-token-tracker` has captured. Capture is automatic — the plugin's hooks
record every turn against the ticket id in the branch that was checked out at the time, so
there is nothing to start or stop.

## Steps

1. Work out what the user is asking for:
   - **no argument** → all tickets, biggest spender first
   - **a ticket id** (`PROJ-412`, `#883`) → drill into that ticket's sessions and branches
   - **a rolling window** ("last week", "past month") → `--since 7d` / `--since 30d`
   - **a date range** ("in August", "between the 1st and the 15th", "since Aug 1") →
     `--since 2026-08-01 --until 2026-08-15`. Both bounds also take `<n>d`/`<n>h` or a
     `2026-08-01T09:30` datetime. **Absolute values are local dates**, and a bare `--until`
     date includes that whole day — so "the 1st to the 15th" is exactly those two flags.
     Anything unparsable is reported as ignored, never silently dropped.
   - **spend over time** ("per day", "how has it trended", "week by week") → `--by day`,
     `--by week` (Monday-start) or `--by month`. Combine with a ticket to get that ticket
     day by day, and with `--since`/`--until` to bound it.
   - **"export"/"csv"/"json"/"for a spreadsheet"** → `--format csv` or `--format json`
     (the `period` column comes through, so a time series exports directly)

2. Run it:

   ```bash
   btt report                                  # every ticket
   btt report PROJ-412                         # one ticket's sessions
   btt report --since 30d                      # rolling window
   btt report --since 2026-08-01 --until 2026-08-15   # a closed date range
   btt report --by week                        # spend per week
   btt report PROJ-412 --by day                # one ticket, day by day
   btt report --project my-repo                # one repo only
   btt report --format csv > tokens.csv        # export
   ```

   Convert the user's words to dates yourself — "in August" is
   `--since 2026-08-01 --until 2026-08-31`, not a guessed `--since 31d`. Today's date is in
   your context; use it rather than asking.

3. Present the table as-is — it is already markdown. Add one line of interpretation: which
   ticket dominates, and whether cache-read (usually the bulk) or output tokens drive it.
   For a `--by` report, say what the trend is rather than restating the rows.

   Rank on **weighted** tokens, not the raw total: raw is ~95% cache reads, which bill at a
   tenth of input, so it ranks by session length rather than cost. Subagent spend is included
   in weighted, read from each agent's own transcript. The exception is an agent whose log is
   gone, which leaves only a bare total — it shows in raw and is left out of weighted, since a
   single number cannot be split across classes that bill at 1x, 5x and 0.1x.

   Timestamps are stored in UTC but every bound and bucket is local, so the days in the
   output are the user's own calendar days.

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
