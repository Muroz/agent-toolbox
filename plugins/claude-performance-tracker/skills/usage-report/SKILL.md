---
name: usage-report
description: Report on your tracked Claude usage — overview, approach comparison, model-degradation trend or a single run's scorecard. Use to understand cost (tokens/time/prompts) per successful outcome and to compare how different approaches fare.
---

# /usage-report — read the data

Render reports from the local store. Every number is computed at read time from raw rows.

## Views

- `overview` — totals + per-project + per-model + per-subagent + time-series. Reports
  **weighted** tokens (input-equivalent: output 5x, cache write 1.25-2x, cache read 0.1x)
  alongside the raw classes, plus active time (idle gaps capped) and elapsed span.
- `compare` — for a `{task_type × size}` bucket, each approach's median **weighted** tokens /
  active time / prompts **per successful outcome**. Refuses to rank a bucket with too few samples
  ("insufficient data, n=N") rather than crown a false winner. Choose the approach
  dimension with `--by model|mode|subagent|skill|effort` (default `model`), and the
  ranking threshold with `--min N`. The ranking uses only self-reported successful tracked
  runs, and flags inferred-success runs rather than blending them in.
- `recommend` — the actionable form of `compare`: "for this kind of task, use approach Z."
  With `--type <t> --size <s>` it drills into one bucket and names the cheapest-per-success
  approach (flagging low confidence when under-sampled); with no filter it lists the best
  approach per bucket. Same `--by` dimensions as `compare`.
- `antipatterns` — recurring friction across runs (interrupts, re-prompts, blind edits,
  reasoning loops, premature stops), worst first, with the share landing in a bad outcome
  and where each clusters. The report flags signals with **no** matching rubric dimension as
  candidates to add to `rubric.yaml`. Also lists the weakest prompt-quality habits. Scope
  the window with `--since 30d` (or `12h`).
- `degradation` — efficiency/quality metrics over time, split by model, so "is the model
  getting worse" is a trend, not a vibe.
- `run <id>` — full scorecard for one run plus its judge verdict.

## How to run

Run `cpt report [view] [args]` and present the markdown tables it produces (default view
is `overview`):

```bash
cpt report                                    # overview
cpt report compare --by model                 # approach comparison
cpt report recommend --type refactor --size L # actionable "use approach Z"
cpt report antipatterns --since 30d           # recurring friction + rubric candidates
```

Fallback if `cpt` is not on PATH:

```bash
REPORT=$(ls -t ~/.claude/plugins/cache/*/claude-performance-tracker/*/scripts/report.py 2>/dev/null | head -1)
python3 "$REPORT" [view] [args]
```

> Scaffold: report queries are tracer-bullet issues, one per view.


## Repairing the store

If numbers look wrong — or after upgrading from a version before 0.4.0 — run `cpt backfill`
to re-derive every session's turns from its transcript, then re-read the report. It repairs
truncated envelopes, drops rows the parser no longer produces and recomputes each affected
run's aggregates, signals and inferred outcome. Safe to re-run.
