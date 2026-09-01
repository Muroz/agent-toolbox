-- branch-token-tracker storage.
--
-- One table on purpose. Every number this plugin reports is a GROUP BY over
-- `turns` at read time — there is nothing to keep in sync, nothing to migrate
-- when a new report shape is wanted, and no state machine to leave in a bad
-- state when a session dies.
--
-- `turn_id` is the transcript's user-message uuid, so re-reading a transcript
-- after every Stop hook is naturally idempotent: rows that already exist are
-- skipped, and a turn's ticket attribution is fixed the first time it is seen.

PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS turns (
  turn_id               TEXT PRIMARY KEY,
  session_id            TEXT NOT NULL,
  project               TEXT,          -- basename(cwd)
  branch                TEXT,          -- branch at capture time; NULL outside a repo
  ticket                TEXT NOT NULL, -- id extracted from `branch`, or the fallback
  model                 TEXT,
  started_at            TEXT,
  ended_at              TEXT,
  input_tokens          INTEGER NOT NULL DEFAULT 0,
  output_tokens         INTEGER NOT NULL DEFAULT 0,
  cache_read_tokens     INTEGER NOT NULL DEFAULT 0,
  cache_creation_tokens INTEGER NOT NULL DEFAULT 0,
  -- 1h-TTL slice of cache_creation_tokens (bills at 2x input vs 1.25x for 5m).
  cache_creation_1h_tokens INTEGER NOT NULL DEFAULT 0,
  -- a backgrounded subagent that only reported an aggregate, with no split.
  total_tokens_agg      INTEGER NOT NULL DEFAULT 0,
  num_tool_calls        INTEGER NOT NULL DEFAULT 0,
  active_ms             INTEGER,
  -- 'main' or 'subagent'. A subagent's spend is charged to the same ticket, but
  -- keeping it separable is what makes "where did this ticket's tokens go"
  -- answerable.
  query_source          TEXT NOT NULL DEFAULT 'main',
  agent_type            TEXT,
  is_prompt             INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_turns_ticket ON turns(ticket);
CREATE INDEX IF NOT EXISTS idx_turns_session ON turns(session_id);
CREATE INDEX IF NOT EXISTS idx_turns_started ON turns(started_at);

-- Schema bookkeeping so an existing database can be migrated forward; see db.py.
CREATE TABLE IF NOT EXISTS schema_meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
