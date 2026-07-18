---
name: track-resume
description: Resume a previously paused tracked performance run and attach it to this session. Use to pick a task back up — including in a different session from where it started — so its remaining work is measured on the same run. Identifies the run by id or task label.
---

# /track-resume — resume a paused tracked run

Reattach a paused tracked run to **this** session so subsequent turns continue accumulating
on it. This is how a task started (and paused, or left open at session end) in one session
is continued in another.

## Steps

1. Identify which run to resume. If the user didn't name one, list the open runs first so
   they (and you) can see the ids and labels:

   ```bash
   cpt track list
   ```

2. Resume by run id or task label (`--run` accepts either):

   ```bash
   cpt track resume --session-id "$CLAUDE_CODE_SESSION_ID" --run "<run-id-or-label>"
   ```

   Fallback if `cpt` is not on PATH:

   ```bash
   TRACK=$(ls -t ~/.claude/plugins/cache/*/claude-performance-tracker/*/scripts/track.py 2>/dev/null | head -1)
   python3 "$TRACK" resume --session-id "$CLAUDE_CODE_SESSION_ID" --run "<run-id-or-label>"
   ```

3. Relay the confirmation. If this session was already tracking something else, resume
   auto-pauses it (the CLI reports which). Remind the user to `/track-done` when finished.

If the CLI reports the label matched several runs, it prints their ids — re-run with the
exact run id. If nothing matched, show `/track-list` so the user can pick a valid one.
