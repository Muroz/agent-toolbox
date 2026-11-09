# Changelog

All notable changes to branch-token-tracker.

After any upgrade that changes how transcripts are parsed, run `btt backfill`.
It re-derives turns from the transcripts still on disk, so spend an older parser
could not see is recovered rather than lost.

## 0.8.0

- `btt report --unassigned sessions` shows work off any ticket as one row per
  session, named by its title, instead of a single `unassigned` row. It also
  works with `--by <period> --split ticket`. Exports keep `unassigned` in the
  `ticket` column and add `session_id`. They leave out session titles unless
  you pass `--include-text`.
- `btt ticket [branch ...]` prints the ticket each branch name files under,
  using the config that applies in the current directory. Combine it with
  `BTT_PATTERN` to try a pattern. The README used to suggest
  `BTT_PATTERN=... btt report` for that, which shows nothing new: reports read
  the tickets stored at capture.
- `btt purge-unassigned` prints what it would delete from the fallback ticket.
  With `--yes` it backs up the database, then deletes those turns and any
  session rows left with no turns. Schema 4 adds a `purged_turns` table, and
  the hooks and `btt backfill` never re-insert a purged turn.

## 0.7.2

- Every hook now times out after 30 seconds rather than the 600-second default.

## 0.7.1

- A local command such as `/clear` no longer counts as a session's first
  prompt. Only a prompt the model answered does, and a slash command reads as
  typed, `/name args`, rather than as its wrapper tags. Run `btt backfill` to
  correct stored sessions.
- A drilldown shows an untitled session's first prompt, in quotes, in place of
  its title. Claude Code titles only interactive sessions, so Agent SDK runs and
  Claude Desktop sessions used to show a dash.

## 0.7.0

- Record why a turn cost what it did: model calls, peak and mean context per
  call, the size of tool output fed back, gaps of over 5 minutes between calls
  and the cache writes the call after each gap made. The ticket table shows
  them as `cost shape` and `idle`, and shows `active` time.
- Record each session's title, first prompt, repo (the `origin` remote, with any
  credentials stripped), Claude Code version and billing mode in a new
  `sessions` table. Drilldowns show the title and repo, so
  `btt report unassigned` says what untracked work was.
- Price every turn at its own model's rates. Opus 5.5 and Fable 5.1 read cache
  at well under 0.1x input, so the old single-rate estimate overstated them.
  Add the 5.5 and 5.1 models, and correct Sonnet 5 from $3 to $2 input. A `*`
  marks a dollar figure that leaves out spend on an unpriced model. The report
  shows each model's share in a `models` column.
- Add `btt enrich`, which stores PR state, size, reviews and reopens per branch
  from `gh`. The ticket table gains a `PRs` column.
- Add `--split ticket` to `--by`, with PR open and merge markers.
- CSV and JSON rows carry `person`, `btt_version`, `schema_version`, the
  window and `generated_at`. Titles and prompts appear in them only with
  `--include-text`.

Run `btt backfill` after upgrading to fill the new fields for sessions whose
transcripts are still on disk. Older turns read as unknown, not zero. The full
steps, including the new config keys, are in
[docs/upgrading-to-0.7.0.md](docs/upgrading-to-0.7.0.md).

## 0.6.2

- `btt report --by` now fills `active` from each turn's stored working time.
  It used to show the span from a bucket's first timestamp to its last, so a
  turn left open overnight read as a 24-hour day. CSV and JSON exports gain an
  `active_ms` field.

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
