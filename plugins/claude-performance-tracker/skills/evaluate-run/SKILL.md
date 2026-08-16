---
name: evaluate-run
description: Score the qualitative side of one or more runs using the usage-evaluator subagent — agent-behavior rubric plus per-prompt quality. Use to evaluate recent runs or a specific run id. Runs out of the hot path (opt-in / batched).
---

# /evaluate-run — qualitative scoring

Score one or more runs against the rubric in `scripts/rubric.yaml` using the
`usage-evaluator` subagent (Haiku, low effort), and persist the results. Never run this in
the hot path — it is deliberate / batched.

## Steps

1. **Resolve targets.**
   - If the user gave a `run_id`, use it.
   - Otherwise list recent un-judged runs and pick from them:
     ```bash
     cpt eval list-unjudged --limit 10
     ```

2. **For each target run**, get its context (transcripts + per-turn prompts + rubric):
   ```bash
   cpt eval context --run-id <run_id>
   ```

3. **Dispatch the `usage-evaluator` subagent** (via the Agent tool) for that run, passing
   the context from step 2. It reads the transcript(s) and `rubric.yaml`, scores the
   `agent_behavior` dimensions once for the run and the `prompt_quality` dimensions once
   per `turn_id`, and returns a JSON verdict (see the agent's contract).

4. **Persist** the returned JSON (write it to a temp file, then):
   ```bash
   cpt eval persist --run-id <run_id> --json-file /tmp/verdict.json
   ```
   (or pipe the JSON to `cpt eval persist --run-id <run_id>` on stdin.)

5. Summarize what was scored (overall grade + notable rationales). View later with
   `cpt report run <run_id>`.

## Optional: second-opinion verification (`--verify`)

If the user asks to verify a run (or invokes `/evaluate-run --verify`), run a **second**
`usage-evaluator` pass on the same run and check the two agree. Single-pass is the default —
this costs a second judge call, so it's opt-in.

- Dispatch the `usage-evaluator` subagent a second time (steps 2–4 again) for the run. Both
  verdicts persist — `judge_verdicts` keeps one row per pass and the EAV `scores` accumulate,
  so no dedup is needed.
  **Pass the `run_id` explicitly.** Step 1's `list-unjudged` excludes runs that already have a
  verdict, so a verify target can never be picked from that list.
- Then reconcile the passes:
  ```bash
  cpt eval reconcile --run-id <run_id>            # newest verdict's rubric version
  cpt eval reconcile --run-id <run_id> --rubric-version 1   # pin an older rubric
  ```
  It reports any dimension whose scores differ by **more than 1 point** across passes *under
  one rubric version* — a rubric bump recalibrates the scale, so cross-version scores are not
  comparable and are never compared. Surface those disagreements — they mark where the rubric
  is ambiguous or the run is genuinely borderline. `cpt report run <run_id>` also shows a
  "Judge agreement" note once >1 pass exists under the rendered verdict's rubric.

Fallback if `cpt` is not on PATH: resolve `scripts/evaluate.py` via
`ls -t ~/.claude/plugins/cache/*/claude-performance-tracker/*/scripts/evaluate.py | head -1`
and call it with `python3`.
