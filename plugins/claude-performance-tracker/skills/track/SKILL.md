---
name: track
description: Open a deliberately-tracked performance run for the work you are about to do. Use when you want to measure and later compare an approach (model, plan-mode, subagent, skill) against others on a real task. Asks for a task label, type, size and intended approach.
---

# /track — open a tracked run

Begin a **tracked run** so this stretch of work is measured as one comparable unit.
Tracking is **per session**: this session's turns attach to its own tracked run, so
several sessions can track different tasks in parallel without cross-contaminating. A
tracked run overrides passive capture and stays open across `/clear` and across sessions
until `/track-done` closes it (ending a session merely *pauses* it — resume later with
`/track-resume`).

Best invoked at the **start** of a fresh context (e.g. right after `/clear` or in a new
session) so the measured envelope is clean.

## Steps

1. Gather these from the user — ask, do not guess (use AskUserQuestion for the choices):
   - **label** — short description of the task (free text).
   - **type** — one of: `bugfix`, `feature`, `refactor`, `research`, `debug`, `other`.
   - **size** — `S`, `M`, or `L` (rough expected effort).
   - **approach** — optional free text describing the intended approach
     (e.g. "plan-mode + opus-4-8, no subagents").

   **Recommender nudge** — right after the user gives **type** and **size**, and
   *before* asking for the approach, look up what has historically worked best for
   this kind of task:

   ```bash
   cpt report recommend --type <type> --size <size>
   ```

   If it returns a **confident** recommendation (not "low confidence" / "No
   self-reported successful runs"), surface it to the user as a suggested default
   approach before they answer. If it's low-confidence or empty, stay silent — never
   block or nag. (Same `cpt`-not-on-PATH fallback as step 2, resolving `report.py`.)
2. Open the run by invoking the tracker CLI. Pass this session's id so capture is scoped
   to this session:

   ```bash
   cpt track start --session-id "$CLAUDE_CODE_SESSION_ID" \
     --label "<label>" --type <type> --size <size> --approach "<approach>"
   ```

   If `cpt` is not on PATH (e.g. dev mode), resolve the bundled script and call it:

   ```bash
   TRACK=$(ls -t ~/.claude/plugins/cache/*/claude-performance-tracker/*/scripts/track.py 2>/dev/null | head -1)
   python3 "$TRACK" start --session-id "$CLAUDE_CODE_SESSION_ID" \
     --label "<label>" --type <type> --size <size> --approach "<approach>"
   ```

3. Relay the confirmation, and remind the user to run `/track-done` when finished.

If this session was already tracking another run, `/track` auto-pauses it (nothing is
lost — it stays resumable). The CLI reports the auto-paused run id when that happens.
