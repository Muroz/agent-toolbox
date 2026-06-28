---
name: track-done
description: Close the currently-open tracked performance run and record its outcome. Use when the task you bracketed with /track is finished (or abandoned). Asks for outcome and a 1-5 satisfaction score.
---

# /track-done — close a tracked run

Finalize the open tracked run with **ground-truth outcome** — the signal that makes the
measured cost interpretable.

## What this does

1. Find the open tracked run via the `open_run` pointer (error clearly if none is open).
2. Ask the user (mandatory):
   - `outcome` — success / partial / failed.
   - `satisfaction` — 1..5.
   - `note` — optional.
3. Finalize the run: aggregate its `turns` into the `runs` row, set
   `outcome_source='self_report'`, `closed_by='track-done'`, `ended_at`, and clear the
   `open_run` pointer.

> Scaffold: wire to `scripts/` finalization. Tracked-run finalization is a tracer-bullet
> issue.
