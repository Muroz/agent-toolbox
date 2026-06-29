---
name: track-done
description: Close the currently-open tracked performance run and record its outcome. Use when the task you bracketed with /track is finished (or abandoned). Asks for outcome and a 1-5 satisfaction score.
---

# /track-done — close a tracked run

Finalize the open tracked run with **ground-truth outcome** — the signal that makes the
measured cost interpretable. Outcome and satisfaction are mandatory.

## Steps

1. Gather from the user — ask, do not guess (use AskUserQuestion):
   - **outcome** — `success`, `partial`, or `failed`.
   - **satisfaction** — an integer 1–5.
   - **note** — optional free text.
2. Close the run:

   ```bash
   cpt track done --outcome <outcome> --satisfaction <1-5> --note "<note>"
   ```

   Fallback if `cpt` is not on PATH:

   ```bash
   TRACK=$(ls -t ~/.claude/plugins/cache/*/claude-performance-tracker/*/scripts/track.py 2>/dev/null | head -1)
   python3 "$TRACK" done --outcome <outcome> --satisfaction <1-5> --note "<note>"
   ```

3. Relay the confirmation.

If the CLI reports no tracked run is open, tell the user there is nothing to close and that
they can start one with `/track`.
