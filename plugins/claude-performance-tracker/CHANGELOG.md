# Changelog

All notable changes to claude-performance-tracker.

After any upgrade that changes how transcripts are parsed, run `cpt backfill`.
It corrects a database written by an older version in place: refilling truncated
envelopes, dropping rows the parser no longer produces, relabeling mislabeled
subagent rows, and recomputing each affected run's aggregates, signals and
inferred outcome.

## 0.7.1

- Count every `tool_use` block rather than one per message.

## 0.7.0

- Read a subagent's real token envelope from its own transcript at
  `<slug>/<session-id>/subagents/agent-<id>.jsonl`. Those logs also discover the
  agents, so capture no longer depends on a notification arriving or on the
  parser recognizing the shape it arrives in.
- Rank subagent evidence: the agent's own log beats a completed tool result,
  which beats a bare aggregate from a notification. Better evidence replaces the
  earlier figure rather than adding to it.

  The bare aggregate is roughly the non-cached tokens, about 40% of real
  weighted cost, which is why runs with backgrounded agents used to look far
  cheaper than they were.
- `cpt backfill` upgrades stored aggregate rows to the real split, clearing
  `total_tokens_agg` on any row that gains one so the agent is not counted
  twice.

## 0.6.0

- Scan all three record shapes a subagent notification can arrive on:
  `type=user`, `type=attachment` and `type=queue-operation`.

  Version 0.5.0 and earlier scanned only `type=user`, which silently dropped
  every agent whose notification came through as an attachment. In one real
  session that was two of four agents and 60% of its subagent tokens.

## 0.5.0

- Guarantee that a hook can never block the session. Ingest entrypoints always
  exit 0, and print a traceback only under `CPT_DEBUG=1`.
- Add the weighted cost model, so rankings use input-equivalent tokens rather
  than a raw sum dominated by cache reads.
- Report time two ways. `active_ms` caps individual idle gaps at 5 minutes and
  is what the rankings use. `wall_clock_ms` is the raw calendar span, which on
  real data produced 250-hour runs and is not a cost.
- Add `cpt backfill` and `cpt sweep`.
- Remove the `SubagentStop` hook. Its payload's `transcript_path` is the main
  transcript, so firing mid-turn it captured the in-flight main turn with only
  the tokens produced so far, mislabeled it `subagent`, and pinned it. Turns
  that spawned a subagent lost about 89% of their output. Subagent spend now
  comes from the `Agent` tool results instead.
- Remove the `UserPromptSubmit` hook. Capture happens at `Stop`, when usage is
  known, so this hook only ever spawned a process to do nothing.
- Exclude Claude Code's injected records from the turn boundary:
  `<task-notification>`, local-command caveats and stdout, and
  `[Request interrupted…]`. Counting them inflated `num_prompts` by about 19%.

## 0.3.0

- Add `cpt report recommend`, the actionable form of `compare`, surfaced
  automatically at `/track` time.
- Add `cpt report antipatterns`: recurring friction across runs, with the share
  landing in a bad outcome, where each clusters, and friction signals that have
  no matching rubric dimension flagged as candidates to add.
- Add `/usage-lessons` and the `lessons-synthesizer` subagent, which turn the
  run history into a durable, git-shareable playbook.
- Harden the judge with 0/1/2 calibration anchors per rubric dimension, plus an
  opt-in second-opinion pass through `/evaluate-run --verify` and
  `cpt eval reconcile`.

## 0.2.0

- Make tracked runs per session, with `/track-pause`, `/track-resume` and
  `/track-list`. A run can be paused in one session and finished in another, and
  two sessions can track different tasks in parallel. `SessionEnd` auto-pauses
  rather than finalizing, so a run survives a dead session as resumable.

## 0.1.1

- Discover the populated data directory instead of guessing the unsuffixed name.
  Claude Code suffixes `${CLAUDE_PLUGIN_DATA}` with the install source, so the
  report used to read an empty stub and print "No usage captured yet" while the
  data sat in the suffixed directory.
- Run hooks through the `bin/cpt` launcher, which forces a pyenv-independent
  interpreter.

## 0.1.0

- First release. Hooks capture every session, `/track` brackets a deliberate
  run, `/evaluate-run` scores it against a rubric, and `/usage-report` reads it
  back.
