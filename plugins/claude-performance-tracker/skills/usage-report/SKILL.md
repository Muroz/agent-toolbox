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
  ("insufficient data, n=N") rather than crown a false winner.
- `degradation` — efficiency/quality metrics over time, split by model, so "is the model
  getting worse" is a trend, not a vibe.
- `run <id>` — full scorecard for one run plus its judge verdict.

## How to run

Invoke `scripts/report.py <view> [args] --data-dir "$CLAUDE_PLUGIN_DATA"` and present the
markdown tables it produces. Default to `overview` when no view is given.

> Scaffold: report queries are tracer-bullet issues, one per view.
