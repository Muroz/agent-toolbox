# claude-performance-tracker

A Claude Code plugin to **qualify good agent usage** and **compare approaches** on a
*cost-per-successful-outcome* basis. Cost is measured in **tokens + time + prompts** — not
dollars. The goal is not accounting or forensics; it is to turn "am I using agents well?"
and "which approach is better for this kind of task?" into measured, repeatable answers.

> Status: **scaffold**. Contracts (schema, rubric, hook wiring, plugin/marketplace
> manifests) are in place; implementation is tracked as tracer-bullet issues.

## How it works

Two write paths feed one local SQLite store; reports are computed from raw facts at read time.

- **Passive capture** (always on) — hooks log every session/turn. Outcome is *inferred*
  (audit-logged, flagged `outcome_source=inferred`, `unknown` as honest fallback).
- **Tracked runs** (deliberate) — `/track` brackets an experiment; you give a mandatory
  self-reported outcome + satisfaction at `/track-done`.

### Capture spine (hybrid, file-leaning)

Transcript-parse now → optional OTEL receiver later. Both write the **same normalized
schema** behind a `source` column, so the OTEL upgrade is additive, not a migration.

### Hooks

| Hook | Role |
|------|------|
| `SessionStart` | open/resume a run |
| `UserPromptSubmit` | advance the turn, mark `/clear` boundaries |
| `Stop` | **workhorse** — per-turn envelope from the transcript into `turns` |
| `SubagentStop` | attribute subagent token usage to the run |
| `SessionEnd` | finalize the current passive run |

Run **finalization** happens at boundaries (`SessionEnd` / `/clear` / `/track-done`); a
tracked run overrides passive and may span boundaries. `run_id` is **session-independent**
(stored in the DB), so cross-session task tracking is additive later.

### Three scoring layers

1. **Deterministic metrics** — always on (tokens, time, prompts, tool-calls, output LOC,
   friction signals, context-window pressure).
2. **Self-report** — mandatory on tracked runs (`outcome`, `satisfaction`).
3. **`usage-evaluator` subagent** — opt-in / batched (Haiku, low effort). Scores an
   agent-behaviour rubric **and** per-prompt quality. Scores are stored in long form
   (EAV) against a versioned `rubric.yaml`, so new dimensions never require a migration.

### Comparison

Bucketed by `{task_type × size}`, ranked on cost-per-**success**, with a small-sample
guard that refuses to crown false winners.

## Storage

SQLite at `${CLAUDE_PLUGIN_DATA}/usage.db` — `runs`, `turns`, `scores` (EAV),
`judge_verdicts`, `open_run` (session-independent pointer). Raw facts only; all derived
and comparison numbers are computed at report time.

## Skills

| Skill | Purpose |
|-------|---------|
| `/track` | Open a tracked run (task label, type, size, intended approach). |
| `/track-done` | Close a tracked run with outcome + satisfaction. |
| `/usage-report` | Overview · approach-comparison · degradation-watch · run-scorecard. |
| `/evaluate-run` | Run the `usage-evaluator` subagent over recent or specified runs. |

## Deferred (foundations laid)

OTEL receiver · scheduled digest · live statusline · real-time prompt coaching ·
cross-session resume · richer report exporters (JSON/CSV/HTML/dashboard).
