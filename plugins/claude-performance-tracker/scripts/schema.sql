-- claude-performance-tracker storage schema.
--
-- Design rules baked in here:
--   * Raw facts only. No pre-aggregated report numbers — every derived metric
--     (cost-per-success, bucket medians, trends) is computed at report time.
--   * `run_id` is session-independent so a run can own turns across sessions.
--   * `source` columns ('transcript' now, 'otel' later) let a future OTEL
--     receiver write the same tables without a migration.
--   * Qualitative scores use a long/EAV shape so new rubric dimensions are
--     rows, never schema changes. Verdicts carry `rubric_version`.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- One row per run = the scorecard. capture_mode: 'passive' | 'tracked'.
CREATE TABLE IF NOT EXISTS runs (
    run_id          TEXT PRIMARY KEY,
    capture_mode    TEXT NOT NULL CHECK (capture_mode IN ('passive', 'tracked')),
    project         TEXT,
    started_at      TEXT NOT NULL,
    ended_at        TEXT,
    closed_by       TEXT,          -- 'SessionEnd' | 'clear' | 'track-done' | NULL (open)

    -- task tags (tracked runs; nullable for passive)
    task_label        TEXT,
    task_type         TEXT,        -- bugfix | feature | refactor | research | debug | ...
    size_class        TEXT,        -- S | M | L
    intended_approach TEXT,        -- free-text approach the user set at /track

    -- approach descriptor (auto-derived; comma/JSON encoded where multi-valued)
    models          TEXT,
    effort          TEXT,
    permission_mode TEXT,          -- plan | acceptEdits | auto | default
    subagents_used  TEXT,
    skills_used     TEXT,
    mcp_tools_used  TEXT,

    -- cost envelope (tokens + time + counts; no USD)
    input_tokens          INTEGER DEFAULT 0,
    output_tokens         INTEGER DEFAULT 0,
    cache_read_tokens     INTEGER DEFAULT 0,
    cache_creation_tokens INTEGER DEFAULT 0,
    cache_creation_1h_tokens INTEGER DEFAULT 0,
    total_tokens_agg      INTEGER DEFAULT 0,
    wall_clock_ms         INTEGER,   -- elapsed calendar span, first to last turn
    active_ms             INTEGER,   -- working time: sum of the turns' active_ms
    num_prompts           INTEGER DEFAULT 0,
    num_tool_calls        INTEGER DEFAULT 0,

    -- tangible output (descriptive, never a target)
    lines_added     INTEGER DEFAULT 0,
    lines_removed   INTEGER DEFAULT 0,
    files_touched   INTEGER DEFAULT 0,
    doc_words       INTEGER DEFAULT 0,

    -- friction signals
    interrupts          INTEGER DEFAULT 0,
    re_prompts          INTEGER DEFAULT 0,
    edits_without_read  INTEGER DEFAULT 0,
    reasoning_loops     INTEGER DEFAULT 0,
    premature_stops     INTEGER DEFAULT 0,

    -- context-window pressure. The raw token peak is the honest figure; the
    -- percentage depends on a window size the transcript never states, so it is
    -- derived at read time against a documented assumption (see report.py).
    peak_context_tokens INTEGER,
    peak_context_pct    REAL,
    compact_count       INTEGER DEFAULT 0,
    clear_count         INTEGER DEFAULT 0,

    -- outcome
    outcome         TEXT,          -- success | partial | failed | unknown
    outcome_source  TEXT,          -- self_report | inferred
    satisfaction    INTEGER,       -- 1..5 (self-report only)
    note            TEXT,
    inferred_signals TEXT,         -- JSON: signals that produced an inferred outcome

    source          TEXT NOT NULL DEFAULT 'transcript'
);

-- One row per turn (per prompt.id). Carries both session_id and run_id so a
-- run can span sessions.
CREATE TABLE IF NOT EXISTS turns (
    turn_id         TEXT PRIMARY KEY,   -- prompt.id when available
    run_id          TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    session_id      TEXT NOT NULL,
    seq             INTEGER,
    started_at      TEXT,
    ended_at        TEXT,

    model           TEXT,
    effort          TEXT,
    query_source    TEXT,           -- main | subagent | auxiliary

    input_tokens          INTEGER DEFAULT 0,
    output_tokens         INTEGER DEFAULT 0,
    cache_read_tokens     INTEGER DEFAULT 0,
    cache_creation_tokens INTEGER DEFAULT 0,
    -- the 1h-TTL slice of cache_creation_tokens; the remainder is 5m TTL. Kept
    -- apart because they bill at 2x and 1.25x input respectively (see cost.py).
    cache_creation_1h_tokens INTEGER DEFAULT 0,
    -- a backgrounded subagent that only ever reported an aggregate total, with
    -- no per-class split. Deliberately excluded from weighted cost.
    total_tokens_agg      INTEGER DEFAULT 0,
    -- working time: the turn's span with each idle gap capped (transcript.py).
    active_ms             INTEGER,
    num_tool_calls        INTEGER DEFAULT 0,

    -- False for rows that consume tokens but are not a human prompt: subagent
    -- rows, and assistant work with no preceding prompt. `num_prompts` counts
    -- is_prompt rows only, so Claude Code's injected `<task-notification>`
    -- records can never inflate it again.
    is_prompt       INTEGER NOT NULL DEFAULT 1,
    agent_type      TEXT,           -- subagent_type, for subagent rows

    prompt_text     TEXT,           -- retained for prompt-quality scoring
    source          TEXT NOT NULL DEFAULT 'transcript'
);

CREATE INDEX IF NOT EXISTS idx_turns_run ON turns(run_id);
CREATE INDEX IF NOT EXISTS idx_turns_session ON turns(session_id);

-- Long-form qualitative scores (EAV). subject_type ties a score to a run or a
-- single turn (prompt). New rubric dimensions are new rows, never new columns.
CREATE TABLE IF NOT EXISTS scores (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    subject_type    TEXT NOT NULL CHECK (subject_type IN ('run', 'prompt')),
    subject_id      TEXT NOT NULL,  -- run_id or turn_id
    dimension       TEXT NOT NULL,  -- e.g. ownership_dodging, clarity, specificity
    score           REAL,
    rationale       TEXT,
    rubric_version  TEXT NOT NULL,
    created_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_scores_subject ON scores(subject_type, subject_id);
CREATE INDEX IF NOT EXISTS idx_scores_dimension ON scores(dimension);

-- One row per judge pass over a run (provenance for the EAV scores above).
CREATE TABLE IF NOT EXISTS judge_verdicts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    model           TEXT,
    rubric_version  TEXT NOT NULL,
    overall_grade   TEXT,
    notes           TEXT,
    created_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_verdicts_run ON judge_verdicts(run_id);

-- Per-session pointer to the tracked run each session is *actively* attaching
-- turns to. This replaces the old singleton `open_run`, so several sessions can
-- track different tasks in parallel and a task can be paused/resumed:
--   * session_id PK  -> a session attaches to at most one tracked run at a time.
--   * run_id UNIQUE  -> a tracked run is active in at most one session at a time.
-- A tracked run that is open (runs.ended_at IS NULL, no outcome) but has NO row
-- here is PAUSED: it receives no turns and is resumable with /track-resume.
-- Only /track-done finalizes a run; SessionEnd merely detaches (auto-pauses) it.
CREATE TABLE IF NOT EXISTS active_tracked (
    session_id      TEXT PRIMARY KEY,
    run_id          TEXT NOT NULL UNIQUE REFERENCES runs(run_id) ON DELETE CASCADE,
    attached_at     TEXT NOT NULL
);

-- Maps a session to the run that owns its turns. A passive run owns exactly one
-- session for now; a tracked run may own several (cross-session, future). Keeping
-- this mapping separate is why `run_id` need not equal `session_id`.
CREATE TABLE IF NOT EXISTS sessions (
    session_id      TEXT PRIMARY KEY,
    run_id          TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    transcript_path TEXT,
    started_at      TEXT
);

CREATE INDEX IF NOT EXISTS idx_sessions_run ON sessions(run_id);


-- Schema bookkeeping, so an existing database can be migrated forward instead of
-- silently keeping an old column set. `CREATE TABLE IF NOT EXISTS` alone never
-- adds a column to a table that already exists, which is how the 0.2.0 -> 0.3.0
-- change stranded the old `open_run` table. See db.py `_migrate`.
CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
