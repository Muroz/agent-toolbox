---
name: track-list
description: List the open tracked performance runs and their state — which is active (and in which session) and which are paused. Use to see what can be resumed or closed when juggling multiple tracked tasks across sessions.
---

# /track-list — show open tracked runs

Show every tracked run that is still open (not yet closed with `/track-done`), labelled as
**active** (and in which session) or **paused**. Useful for deciding what to `/track-resume`
or `/track-done` when several tasks are in flight across sessions.

## Steps

1. List the open runs:

   ```bash
   cpt track list
   ```

   Fallback if `cpt` is not on PATH:

   ```bash
   TRACK=$(ls -t ~/.claude/plugins/cache/*/claude-performance-tracker/*/scripts/track.py 2>/dev/null | head -1)
   python3 "$TRACK" list
   ```

2. Present the list. If a run is paused, offer to `/track-resume` it; if it's the one this
   session is actively tracking, remind the user they can `/track-done` it.
