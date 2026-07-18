---
name: track-done
description: Close the currently-open tracked performance run and record its outcome. Use when the task you bracketed with /track is finished (or abandoned). Asks for outcome and a 1-5 satisfaction score.
---

# /track-done — close a tracked run

Finalize a tracked run with **ground-truth outcome** — the signal that makes the measured
cost interpretable. Outcome and satisfaction are mandatory. By default this closes the run
**this session** is actively tracking; pass `--run <id>` to close a specific paused run
without resuming it.

## Steps

1. Gather from the user — ask, do not guess (use AskUserQuestion):
   - **outcome** — `success`, `partial`, or `failed`.
   - **satisfaction** — an integer 1–5.
   - **note** — optional free text.
2. Close the run (scoped to this session):

   ```bash
   cpt track done --session-id "$CLAUDE_CODE_SESSION_ID" \
     --outcome <outcome> --satisfaction <1-5> --note "<note>"
   ```

   Fallback if `cpt` is not on PATH:

   ```bash
   TRACK=$(ls -t ~/.claude/plugins/cache/*/claude-performance-tracker/*/scripts/track.py 2>/dev/null | head -1)
   python3 "$TRACK" done --session-id "$CLAUDE_CODE_SESSION_ID" \
     --outcome <outcome> --satisfaction <1-5> --note "<note>"
   ```

3. Relay the confirmation.

If the CLI reports this session has no active tracked run, the task may be paused — show
the user `/track-list` and offer to `/track-resume` it first, or close it directly by id
with `--run <id>`.
