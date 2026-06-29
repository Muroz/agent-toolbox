---
name: usage-report
description: Report on your tracked Claude usage — overview, approach comparison, model-degradation trend, or a single run's scorecard. Use to understand cost (tokens/time/prompts) per successful outcome and to compare how different approaches fare.
---

# /usage-report — read the data

Render reports from the local store. All numbers are computed at read time from raw rows.

## Views

- `overview` — totals + per-project + per-model + time-series (tokens, prompts, wall-clock).
- `compare` — for a `{task_type × size}` bucket, each approach's median tokens / time /
  prompts **per successful outcome**. Refuses to rank a bucket with too few samples
  ("insufficient data, n=N") rather than crown a false winner. Choose the approach
  dimension with `--by model|mode|subagent|skill|effort` (default `model`), and the
  ranking threshold with `--min N`. Only self-reported successful tracked runs are ranked;
  inferred-success runs are flagged, never blended in.
- `degradation` — efficiency/quality metrics over time, split by model, so "is the model
  getting worse" is a trend, not a vibe.
- `run <id>` — full scorecard for one run plus its judge verdict.

## How to run

Run `cpt report [view] [args]` and present the markdown tables it produces (default view
is `overview`):

```bash
cpt report                       # overview
cpt report compare --by model    # approach comparison
```

Fallback if `cpt` is not on PATH:

```bash
REPORT=$(ls -t ~/.claude/plugins/cache/*/claude-performance-tracker/*/scripts/report.py 2>/dev/null | head -1)
python3 "$REPORT" [view] [args]
```

> Scaffold: report queries are tracer-bullet issues, one per view.
