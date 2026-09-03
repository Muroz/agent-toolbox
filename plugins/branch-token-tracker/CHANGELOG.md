# Changelog

All notable changes to branch-token-tracker.

After any upgrade that changes how transcripts are parsed, run `btt backfill`.
It re-derives turns from the transcripts still on disk, so spend an older parser
could not see is recovered rather than lost.

## 0.6.1

- Count every `tool_use` block rather than one per message.

## 0.6.0

- Read a subagent's real token envelope from its own transcript at
  `<slug>/<session-id>/subagents/agent-<id>.jsonl`. Those logs also discover the
  agents, so capture no longer depends on a notification arriving or on the
  parser recognizing the shape it arrives in.
- Rank subagent evidence: the agent's own log beats a completed tool result,
  which beats a bare aggregate from a notification. Better evidence replaces the
  earlier figure rather than adding to it.

  Before this release the bare aggregate was the only subagent evidence, so
  agent-heavy tickets understated their cost badly. Measured against the logs,
  the aggregate is roughly the non-cached tokens, about 40% of the real weighted
  cost. In one session four agents reported 239,102 tokens against a true
  weighted cost of 624,230.
- `btt backfill` upgrades stored aggregate rows to the real split, clearing
  `total_tokens_agg` on any row that gains one so the agent is not counted
  twice.

## 0.5.0

- Add `--since` and `--until`, each accepting a relative window, an absolute
  date or an absolute datetime.
- Add `--by day|week|month` to group spend by local calendar period.

## 0.4.0

- Add `btt backfill` to repair a store from the transcripts on disk.
- Scan all three record shapes a subagent notification can arrive on:
  `type=user`, `type=attachment` and `type=queue-operation`.

  Earlier versions scanned only `type=user`, which silently dropped every agent
  whose notification came through as an attachment. In one real session that was
  two of four agents and 60% of its subagent tokens. Every store written before
  this release understates subagent spend.

## 0.3.0

- Guarantee that a hook can never block the session. Ingest entrypoints always
  exit 0.
- Add the weighted cost model and subagent attribution.

  Subagent spend previously relied on sidechain records, which do not exist in
  real transcripts, so in practice it went unbilled to the ticket entirely.
- Rewrite `current.json` on every `Stop` rather than only at `SessionEnd`. A
  statusline reading it used to spend each session showing the previous
  session's numbers.

## 0.1.0

- First release. Three hooks capture token spend per turn and key it to the
  task-tracker id in the git branch name.
