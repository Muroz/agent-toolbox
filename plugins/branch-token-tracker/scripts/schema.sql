-- branch-token-tracker storage.
--
-- `turns` is the only table summed. Every number this plugin reports is a
-- GROUP BY over it at read time — there is nothing to keep in sync, nothing to
-- migrate when a new report shape is wanted, and no state machine to leave in a
-- bad state when a session dies. `sessions` and `prs` hold facts a session or a
-- branch has once; reports join them in and never add them up.
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
  is_prompt             INTEGER NOT NULL DEFAULT 1,
  -- Where the work happened: the normalized origin remote (`github.com/o/r`),
  -- which `project` (a directory basename) cannot tell apart across clones.
  repo                  TEXT,
  cc_version            TEXT,
  -- Why a turn cost what it did. NULL on rows captured before these existed:
  -- unknown, which is not the same as zero.
  api_calls             INTEGER,  -- model calls (distinct message ids)
  peak_context_tokens   INTEGER,  -- largest input + cache read + cache write
  mean_context_tokens   INTEGER,
  tool_result_chars     INTEGER,  -- tool output text fed back to the model
  idle_gaps             INTEGER,  -- gaps between model calls over 5 minutes
  idle_ms               INTEGER,  -- their full, uncapped length
  cold_cache_write_tokens INTEGER -- cache writes by the call after each gap
);

CREATE INDEX IF NOT EXISTS idx_turns_ticket ON turns(ticket);
CREATE INDEX IF NOT EXISTS idx_turns_session ON turns(session_id);
CREATE INDEX IF NOT EXISTS idx_turns_started ON turns(started_at);

-- Facts a session has once rather than per turn. Upserted by the hooks on
-- every capture; nothing here is summed, so nothing can double-count.
-- `title` and `first_prompt` stay on this machine unless an export is run with
-- `--include-text`: prompts can carry secrets or customer data.
CREATE TABLE IF NOT EXISTS sessions (
  session_id     TEXT PRIMARY KEY,
  project        TEXT,
  repo           TEXT,
  title          TEXT,   -- Claude Code's own session title (aiTitle)
  first_prompt   TEXT,   -- first human prompt, truncated to 200 characters
  cc_version     TEXT,
  entrypoint     TEXT,   -- cli | sdk-ts | vscode ...
  billing        TEXT,   -- api | subscription | NULL when unknown
  billing_source TEXT,   -- config (stated) | hint (guessed from the hook env)
  updated_at     TEXT
);

-- Pull requests per branch, filled by `btt enrich` from `gh`. Never written by
-- a hook: it needs network and auth, and a hook must stay fast and offline.
CREATE TABLE IF NOT EXISTS prs (
  repo          TEXT NOT NULL,
  number        INTEGER NOT NULL,
  branch        TEXT NOT NULL,
  state         TEXT,     -- OPEN | CLOSED | MERGED
  created_at    TEXT,
  merged_at     TEXT,
  closed_at     TEXT,
  additions     INTEGER,
  deletions     INTEGER,
  changed_files INTEGER,
  reviews       INTEGER,  -- submitted reviews of any kind
  changes_requested INTEGER, -- reviews that asked for changes: rework rounds
  reopens       INTEGER,
  url           TEXT,
  fetched_at    TEXT NOT NULL,
  PRIMARY KEY (repo, number)
);

CREATE INDEX IF NOT EXISTS idx_prs_branch ON prs(branch);

-- Schema bookkeeping so an existing database can be migrated forward; see db.py.
CREATE TABLE IF NOT EXISTS schema_meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
