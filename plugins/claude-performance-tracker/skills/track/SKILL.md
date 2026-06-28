---
name: track
description: Open a deliberately-tracked performance run for the work you are about to do. Use when you want to measure and later compare an approach (model, plan-mode, subagent, skill) against others on a real task. Asks for a task label, type, size and intended approach.
---

# /track — open a tracked run

Begin a **tracked run** so this stretch of work is measured as one comparable unit.
A tracked run overrides passive capture and stays open across `/clear` and even across
sessions until `/track-done` closes it. Turns produced while it is open attach to it.

Best invoked at the **start** of a fresh context (e.g. right after `/clear` or in a new
session) so the measured envelope is clean.

## Steps

1. Gather these from the user — ask, do not guess (use AskUserQuestion for the choices):
   - **label** — short description of the task (free text).
   - **type** — one of: `bugfix`, `feature`, `refactor`, `research`, `debug`, `other`.
   - **size** — `S`, `M`, or `L` (rough expected effort).
   - **approach** — optional free text describing the intended approach
     (e.g. "plan-mode + opus-4-8, no subagents").
2. Open the run by invoking the tracker CLI:

   ```bash
   cpt start --label "<label>" --type <type> --size <size> --approach "<approach>"
   ```

   If `cpt` is not on PATH (e.g. dev mode), resolve the bundled script and call it:

   ```bash
   TRACK=$(ls -t ~/.claude/plugins/cache/*/claude-performance-tracker/*/scripts/track.py 2>/dev/null | head -1)
   python3 "$TRACK" start --label "<label>" --type <type> --size <size> --approach "<approach>"
   ```

3. Relay the confirmation, and remind the user to run `/track-done` when finished.

The CLI refuses to open a second run while one is already open — if so, tell the user to
`/track-done` first.
