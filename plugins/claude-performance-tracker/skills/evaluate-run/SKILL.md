---
name: evaluate-run
description: Score the qualitative side of one or more runs using the usage-evaluator subagent — agent-behavior rubric plus per-prompt quality. Use to evaluate recent runs or a specific run id. Runs out of the hot path (opt-in / batched).
---

# /evaluate-run — qualitative scoring

Invoke the `usage-evaluator` subagent over a run (or a batch of recent un-judged runs) to
score the behavioral rubric and per-prompt quality defined in `scripts/rubric.yaml`.

## What this does

1. Resolve the target runs: a given `run_id`, or the most recent runs without a
   `judge_verdicts` row.
2. For each run, dispatch the `usage-evaluator` subagent (Haiku, low effort) with the
   run's transcript slice.
3. Persist results: one `judge_verdicts` row + long-form `scores` rows (one per
   dimension, stamped with the current `rubric_version`).

Never run this on every turn — it is deliberate/batched so it stays cheap.

> Scaffold: dispatch + persistence are tracer-bullet issues.
