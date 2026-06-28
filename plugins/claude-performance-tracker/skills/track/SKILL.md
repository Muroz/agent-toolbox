---
name: track
description: Open a deliberately-tracked performance run for the work you are about to do. Use when you want to measure and later compare an approach (model, plan-mode, subagent, skill) against others on a real task. Asks for a task label, type, size and intended approach.
---

# /track — open a tracked run

Begin a **tracked run** so this stretch of work is measured as one comparable unit.
A tracked run overrides passive capture and stays open across `/clear` and even across
sessions until `/track-done` closes it.

## What this does

1. Collect the run's tags (ask the user, do not guess):
   - `task_label` — short description of the task.
   - `task_type` — one of: bugfix, feature, refactor, research, debug (or a new one).
   - `size_class` — S / M / L (rough expected effort).
   - `intended_approach` — optional free text (e.g. "plan-mode + opus-4-8, no subagents").
2. Create a `runs` row with `capture_mode='tracked'` and a fresh, session-independent
   `run_id`; set the `open_run` pointer.
3. Confirm the run is open and that `/track-done` should be used to close it.

> Scaffold: wire this to `scripts/` (an entrypoint that inserts the tracked run and sets
> the open_run pointer). Tracked-run creation is tracked as a tracer-bullet issue.
