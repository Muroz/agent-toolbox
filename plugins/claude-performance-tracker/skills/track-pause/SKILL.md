---
name: track-pause
description: Pause the tracked performance run this session is driving, without finalizing it. Use when you need to set the current task aside to work on something else, then resume it later (here or in another session). Keeps the run open and resumable.
---

# /track-pause — pause the current tracked run

Detach this session from its active tracked run **without** finalizing it. The run stays
open and resumable — turns produced after pausing no longer attach to it (they fall back to
passive capture). Use this to juggle multiple in-progress tasks.

Note: ending a session already auto-pauses its active tracked run, so you only need this to
pause mid-session while continuing to work on something else.

## Steps

1. Pause this session's active run:

   ```bash
   cpt track pause --session-id "$CLAUDE_CODE_SESSION_ID"
   ```

   Fallback if `cpt` is not on PATH:

   ```bash
   TRACK=$(ls -t ~/.claude/plugins/cache/*/claude-performance-tracker/*/scripts/track.py 2>/dev/null | head -1)
   python3 "$TRACK" pause --session-id "$CLAUDE_CODE_SESSION_ID"
   ```

2. Relay the confirmation (it reports the paused run id). Remind the user they can bring it
   back with `/track-resume` and see all open runs with `/track-list`.

If the CLI says there is no active tracked run, tell the user there is nothing to pause.
