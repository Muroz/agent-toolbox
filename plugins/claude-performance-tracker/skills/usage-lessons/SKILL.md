---
name: usage-lessons
description: Synthesize a durable, git-shareable usage playbook from all your tracked runs — what approaches work, recurring friction, weak prompt habits and drift. Uses the lessons-synthesizer subagent. Runs out of the hot path.
---

# /usage-lessons — synthesize a durable playbook

Mine the whole run history into a short **lessons playbook** you can re-read (and paste into
`CLAUDE.md`) so the tracker's measurement actually changes how you work next time. This is the
compounding-loop payoff: `/track` and `/evaluate-run` record the data; this turns it into
guidance. Deliberate / batched — never run it in the hot path.

## Steps

1. **Gather the evidence pack** (deterministic — all numbers computed at read time):
   ```bash
   cpt insights context
   ```
   If `cpt` is not on PATH:
   `INS=$(ls -t ~/.claude/plugins/cache/*/claude-performance-tracker/*/scripts/insights.py 2>/dev/null | head -1)`
   then `python3 "$INS" context`.

2. **Dispatch the `lessons-synthesizer` subagent** (via the Agent tool), passing the JSON from
   step 1. It returns the playbook as Markdown — grounded strictly in those numbers, with no
   attribution. If the pack is essentially empty, it says so; relay that and stop.

3. **Write the playbook to the default file.** Resolve the path (the data dir isn't in the
   session env) and write the returned Markdown there with the Write tool:
   ```bash
   cpt insights lessons-path      # prints <data-dir>/lessons.md
   ```
   This file survives plugin updates. Show the user the path and a short summary.

4. **(Opt-in) Share it via git.** Only if the user asks to feed it into a version-controlled
   file (for example, the project's `CLAUDE.md`), inject it as a **delimited, idempotent
   block** so re-running replaces just that block and never clobbers surrounding content:

   ```
   <!-- cpt-lessons:start -->
   …playbook markdown…
   <!-- cpt-lessons:end -->
   ```

   Read the target file first: if the markers already exist, replace everything between them
   (Edit); otherwise append the whole block at the end. Never touch anything outside the
   markers. Ask which file before writing into the user's repo.
