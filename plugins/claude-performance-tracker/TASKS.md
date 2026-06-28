# claude-performance-tracker — implementation tasks

Tracer-bullet vertical slices. Each is independently grabbable and verifiable end-to-end.
**1 → 2 → 3** is the walking skeleton (capture → store → report); **4–10** deepen one
layer at a time and can largely proceed in parallel once **2** lands. All AFK.

Design reference: [README.md](README.md). Contracts already in place: `scripts/schema.sql`,
`scripts/rubric.yaml`, `hooks/hooks.json`, manifests, stubbed scripts, skills, agent.

---

## 1. Installable plugin + DB bootstrap

**Type:** AFK

### What to build
Make the plugin installable from the local `claude-toolbox` marketplace and have a fresh
install initialize its storage. On `SessionStart`, the plugin creates the SQLite database
in its persistent data directory and applies the schema idempotently. This proves the
plugin loads, hooks fire, and the schema applies on a real machine — nothing about usage
data yet.

### Acceptance criteria
- [ ] `claude plugin marketplace add ~/Coding/claude-toolbox` then
      `claude plugin install claude-performance-tracker@claude-toolbox` succeeds.
- [ ] Starting a session creates `usage.db` in the plugin data dir with all tables present.
- [ ] Re-initialization is idempotent (no error on existing DB).
- [ ] A hook failure never blocks or delays the session (errors are swallowed, exit 0).

### Blocked by
None — can start immediately.

---

## 2. Passive turn capture

**Type:** AFK

### What to build
Passively record work with zero user effort. `SessionStart` opens a passive run with a
session-independent `run_id`. `Stop` parses the turn from `transcript_path` and writes a
`turns` row with the token envelope (input/output/cache) and timing. `SessionEnd`
finalizes the run: aggregate its turns into the `runs` row and close it.

### Acceptance criteria
- [ ] A real session produces one `runs` row and one `turns` row per assistant turn.
- [ ] Token counts and timestamps match the session transcript.
- [ ] `runs` totals equal the sum of their `turns`.
- [ ] `run_id` is independent of `session_id` (stored, not derived).
- [ ] `source='transcript'` on all rows.

### Blocked by
- Slice 1.

---

## 3. `/usage-report overview`

**Type:** AFK

### What to build
Read the captured data back. The `overview` view of `/usage-report` queries `runs`/`turns`
and prints a markdown report: overall totals plus per-model and per-project breakdowns and
a simple time-series (tokens, prompts, wall-clock). Numbers are computed at read time from
raw rows. This closes the thin capture → store → report tracer bullet.

### Acceptance criteria
- [ ] `/usage-report` (default) and `/usage-report overview` print correct markdown tables.
- [ ] Totals reconcile with the `runs`/`turns` rows.
- [ ] Empty DB renders a friendly "no data yet" message, not an error.

### Blocked by
- Slice 2.

---

## 4. Tracked runs (`/track`, `/track-done`)

**Type:** AFK

### What to build
Let the user deliberately bracket an experiment. `/track` collects task tags
(`task_label`, `task_type`, `size_class`, optional intended approach), creates a
`capture_mode='tracked'` run, and sets the `open_run` pointer. `/track-done` finds the
open run, collects a mandatory self-reported `outcome` + `satisfaction` (+ note),
finalizes it, and clears the pointer. A tracked run overrides passive capture for its span.

### Acceptance criteria
- [ ] `/track` creates a tracked run and sets `open_run`; turns during it attach to it.
- [ ] `/track-done` records `outcome`, `satisfaction`, `outcome_source='self_report'`,
      `closed_by='track-done'`, and clears `open_run`.
- [ ] `/track-done` with no open run reports a clear message.
- [ ] A tracked run is not force-closed by `SessionEnd` (stays open until `/track-done`).

### Blocked by
- Slice 2.

---

## 5. Full deterministic envelope

**Type:** AFK

### What to build
Populate the rest of the scorecard from the transcript at finalization: approach descriptor
(`models`, `effort`, `permission_mode`, `subagents_used`, `skills_used`, `mcp_tools_used`),
counts (`num_tool_calls`), tangible output (`lines_added/removed`, `files_touched`,
`doc_words`), friction signals (`interrupts`, `re_prompts`, `edits_without_read`,
`reasoning_loops`, `premature_stops`), and context-pressure (`peak_context_pct`,
`compact_count`, `clear_count`).

### Acceptance criteria
- [ ] All listed `runs` fields are populated for new runs and match the transcript.
- [ ] LOC counts distinguish added vs removed; doc output measured for `.md`/doc edits.
- [ ] Friction signals are derived deterministically and documented (definition per signal).
- [ ] Approach descriptor correctly reflects multi-model / mixed-mode runs.

### Blocked by
- Slice 2.

---

## 6. `/usage-report compare`

**Type:** AFK

### What to build
The headline comparison. The `compare` view groups runs by `{task_type × size}` bucket and,
within each bucket, reports each approach's median tokens / time / prompts **per successful
outcome**. When a bucket has fewer than the minimum samples, it prints "insufficient data,
n=N" instead of ranking — never a false winner.

### Acceptance criteria
- [ ] `compare` produces a per-bucket table ranked on cost-per-success.
- [ ] Buckets below the sample threshold show the small-sample guard, not a ranking.
- [ ] Only `outcome='success'` runs feed cost-per-success; inferred outcomes are flagged.
- [ ] Approaches are distinguished by the descriptor fields from slice 5.

### Blocked by
- Slice 4, Slice 5.

---

## 7. Subagent attribution

**Type:** AFK

### What to build
Account for subagent cost correctly. `SubagentStop` (and/or transcript derivation)
attributes subagent token usage to the owning run, tagged `query_source='subagent'`, so an
approach that fans out to subagents shows its true total cost.

### Acceptance criteria
- [ ] Subagent token usage is attributed to the parent run, not lost.
- [ ] `turns`/aggregates distinguish `query_source` main vs subagent.
- [ ] A run that used subagents reports higher total tokens than its main-only turns.

### Blocked by
- Slice 2.

---

## 8. Inferred outcome for passive runs

**Type:** AFK

### What to build
Give passive runs a usable outcome without self-report. At `SessionEnd`, `infer_outcome`
maps observable signals (late interrupts, re-prompts, sentiment, clean close, topic change)
to `success / partial / failed / unknown`, stores the result with `outcome_source='inferred'`
and the signals that produced it (`inferred_signals` JSON). `unknown` is the honest fallback.

### Acceptance criteria
- [ ] Passive runs receive an inferred outcome with `outcome_source='inferred'`.
- [ ] The signals behind each inference are stored and auditable.
- [ ] Ambiguous runs get `unknown` and are excluded from cost-per-success rankings.
- [ ] Inferred labels are never blended with self-reported labels in headline reports.

### Blocked by
- Slice 2, Slice 5.

---

## 9. Qualitative scoring (`/evaluate-run` + `usage-evaluator`)

**Type:** AFK

### What to build
The LLM-judge layer. `/evaluate-run [id|recent]` dispatches the `usage-evaluator` subagent
(Haiku, low effort) over a run's transcript. It scores the `agent_behavior` rubric once per
run and `prompt_quality` once per user prompt, then persists a `judge_verdicts` row plus
long-form `scores` rows (one per dimension), each stamped with the current `rubric_version`.
Never runs in the hot path.

### Acceptance criteria
- [ ] `/evaluate-run <id>` and a batch mode over recent un-judged runs both work.
- [ ] `agent_behavior` scored per run; `prompt_quality` scored per prompt (per `turn_id`).
- [ ] Scores stored in `scores` (EAV) with `rubric_version`; verdict in `judge_verdicts`.
- [ ] Adding a new dimension to `rubric.yaml` requires no schema change.

### Blocked by
- Slice 2.

---

## 10. `/usage-report degradation` + `run <id>`

**Type:** AFK

### What to build
Drill-down and trend views. `degradation` plots efficiency/quality metrics over time split
by model, so "is the model getting worse" is a trend line. `run <id>` prints one run's full
scorecard including its judge verdict and per-prompt quality.

### Acceptance criteria
- [ ] `degradation` shows per-model time trends for the key metrics.
- [ ] `run <id>` shows the full Q7 scorecard + judge verdict for that run.
- [ ] Both join cleanly against `scores` so prompt-quality can be correlated with cost/outcome.

### Blocked by
- Slice 5, Slice 9.
