"""Persistence operations for runs, sessions and turns.

Higher-level than db.py (which only owns the connection + schema). The capture
path is idempotent: turns are rebuilt from the transcript and upserted by
turn_id, and run aggregates are recomputed from `turns`, so replaying the same
transcript never double-counts.
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timezone

import signals
import transcript as T

SOURCE = "transcript"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_run_id() -> str:
    # Session-independent on purpose: a run is not its session.
    return f"run-{uuid.uuid4().hex[:16]}"


def get_run_for_session(conn: sqlite3.Connection, session_id: str) -> str | None:
    row = conn.execute(
        "SELECT run_id FROM sessions WHERE session_id = ?", (session_id,)
    ).fetchone()
    return row[0] if row else None


def open_passive_run(
    conn: sqlite3.Connection,
    session_id: str,
    transcript_path: str | None,
    project: str | None,
) -> str:
    """Open a passive run for this session, or return the existing one."""
    existing = get_run_for_session(conn, session_id)
    if existing:
        return existing

    run_id = new_run_id()
    conn.execute(
        """INSERT INTO runs (run_id, capture_mode, project, started_at, source)
           VALUES (?, 'passive', ?, ?, ?)""",
        (run_id, project, now_iso(), SOURCE),
    )
    conn.execute(
        """INSERT OR REPLACE INTO sessions (session_id, run_id, transcript_path, started_at)
           VALUES (?, ?, ?, ?)""",
        (session_id, run_id, transcript_path, now_iso()),
    )
    conn.commit()
    return run_id


def capture_session_turns(
    conn: sqlite3.Connection, run_id: str, session_id: str, transcript_path: str,
    query_source: str | None = None, include_sidechain: bool = False,
) -> int:
    """Capture a transcript's turns, attributed to `run_id`.

    Attribution is fixed on first sight — a turn's run_id, session_id and
    query_source are never rewritten — so flipping the tracked/passive pointer
    mid-session cannot re-label earlier turns. The token envelope, however, IS
    refreshed on every pass, taking the larger of the stored and freshly parsed
    value per column.

    That distinction is the whole fix for the capture bug: a turn used to be
    frozen the first time anything captured it, which meant a turn caught
    mid-flight kept a fraction of its real tokens forever. Envelopes only ever
    grow, so a re-parse of a truncated or compacted transcript can never shrink
    one either.

    `query_source` and `include_sidechain` are accepted for compatibility and
    ignored: the parser now tags each turn itself from the record it came from.
    """
    rows = T.parse_turns(transcript_path)
    seen = {r[0] for r in conn.execute("SELECT turn_id FROM turns")}
    inserted = 0
    for t in rows:
        if t.turn_id in seen:
            conn.execute(
                """UPDATE turns SET
                       ended_at = COALESCE(?, ended_at),
                       model    = COALESCE(?, model),
                       effort   = COALESCE(?, effort),
                       agent_type = COALESCE(?, agent_type),
                       input_tokens          = MAX(input_tokens, ?),
                       output_tokens         = MAX(output_tokens, ?),
                       cache_read_tokens     = MAX(cache_read_tokens, ?),
                       cache_creation_tokens = MAX(cache_creation_tokens, ?),
                       cache_creation_1h_tokens = MAX(cache_creation_1h_tokens, ?),
                       total_tokens_agg      = MAX(total_tokens_agg, ?),
                       -- NULL means unknown, 0 means it took no time.
                       -- MAX(COALESCE(...)) would quietly turn the first
                       -- into the second on every re-capture.
                       active_ms = CASE WHEN ? > COALESCE(active_ms, 0)
                                        THEN ? ELSE active_ms END,
                       num_tool_calls        = MAX(num_tool_calls, ?)
                   WHERE turn_id = ?""",
                (t.ended_at, t.model, t.effort, t.agent_type,
                 t.input_tokens, t.output_tokens, t.cache_read_tokens,
                 t.cache_creation_tokens, t.cache_creation_1h_tokens,
                 t.total_tokens_agg, t.active_ms or 0, t.active_ms or 0,
                 t.num_tool_calls,
                 t.turn_id))
            continue
        conn.execute(
            """INSERT INTO turns
               (turn_id, run_id, session_id, seq, started_at, ended_at, model,
                effort, query_source, is_prompt, agent_type,
                input_tokens, output_tokens, cache_read_tokens,
                cache_creation_tokens, cache_creation_1h_tokens,
                total_tokens_agg, active_ms, num_tool_calls, prompt_text, source)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (t.turn_id, run_id, session_id, t.seq, t.started_at, t.ended_at,
             t.model, t.effort, t.query_source, 1 if t.is_prompt else 0,
             t.agent_type, t.input_tokens, t.output_tokens, t.cache_read_tokens,
             t.cache_creation_tokens, t.cache_creation_1h_tokens,
             t.total_tokens_agg, t.active_ms, t.num_tool_calls,
             t.prompt_text, SOURCE),
        )
        inserted += 1
    conn.commit()
    return inserted


def finalize_run(conn: sqlite3.Connection, run_id: str,
                 closed_by: str | None) -> None:
    """Aggregate a run's turns into its `runs` row, and close it.

    `closed_by=None` recomputes the aggregates without closing the run, which is
    what the backfill needs for runs that are still in flight.

    Two different time figures, because they answer different questions:
      * wall_clock_ms — elapsed calendar span, first turn to last. Includes
        every coffee break and overnight gap, so it is NOT a cost measure.
      * active_ms — the sum of the turns' capped working time. This is the one
        the reports rank on.

    `num_prompts` counts main-thread human prompts only (is_prompt), so
    Claude Code's injected records and subagent rows stay out of it.
    """
    agg = conn.execute(
        """SELECT
               COALESCE(SUM(input_tokens),0),
               COALESCE(SUM(output_tokens),0),
               COALESCE(SUM(cache_read_tokens),0),
               COALESCE(SUM(cache_creation_tokens),0),
               COALESCE(SUM(cache_creation_1h_tokens),0),
               COALESCE(SUM(total_tokens_agg),0),
               COALESCE(SUM(num_tool_calls),0),
               COALESCE(SUM(CASE WHEN is_prompt = 1 AND query_source = 'main'
                                 THEN 1 ELSE 0 END),0),
               MIN(started_at),
               MAX(ended_at),
               COALESCE(SUM(active_ms),0),
               (SELECT GROUP_CONCAT(DISTINCT model) FROM turns
                 WHERE run_id = ? AND query_source = 'main' AND model IS NOT NULL),
               (SELECT GROUP_CONCAT(DISTINCT effort) FROM turns
                 WHERE run_id = ? AND effort IS NOT NULL),
               (SELECT GROUP_CONCAT(DISTINCT agent_type) FROM turns
                 WHERE run_id = ? AND agent_type IS NOT NULL)
           FROM turns WHERE run_id = ?""",
        (run_id, run_id, run_id, run_id),
    ).fetchone()
    (inp, out, cr, cc, cc1h, agg_tok, tools, nprompts, first_start, last_end,
     active, models, efforts, agent_types) = agg

    conn.execute(
        """UPDATE runs SET
               input_tokens = ?, output_tokens = ?, cache_read_tokens = ?,
               cache_creation_tokens = ?, cache_creation_1h_tokens = ?,
               total_tokens_agg = ?, num_tool_calls = ?, num_prompts = ?,
               models = ?, effort = ?, started_at = COALESCE(?, started_at),
               ended_at = CASE WHEN ? IS NULL THEN ended_at ELSE ? END,
               wall_clock_ms = ?, active_ms = ?,
               closed_by = COALESCE(?, closed_by)
           WHERE run_id = ?""",
        (inp, out, cr, cc, cc1h, agg_tok, tools, nprompts, models, efforts,
         first_start, closed_by, last_end,
         T.duration_ms(first_start, last_end), active, closed_by, run_id),
    )

    # Deterministic envelope: approach descriptor, output, friction, context.
    env = signals.derive_run_envelope(conn, run_id)
    if env:
        # Subagent types come from the Agent tool results themselves, which is
        # more reliable than scraping tool_use blocks out of the transcript.
        subs = ",".join(sorted(set(
            (env["subagents_used"] or "").split(",") + (agent_types or "").split(",")
        ) - {""})) or None
        conn.execute(
            """UPDATE runs SET
                   permission_mode = ?, subagents_used = ?, skills_used = ?,
                   mcp_tools_used = ?, lines_added = ?, lines_removed = ?,
                   files_touched = ?, doc_words = ?, interrupts = ?,
                   re_prompts = ?, edits_without_read = ?, reasoning_loops = ?,
                   premature_stops = ?, peak_context_tokens = ?,
                   peak_context_pct = ?, compact_count = ?, clear_count = ?
               WHERE run_id = ?""",
            (env["permission_mode"], subs, env["skills_used"],
             env["mcp_tools_used"], env["lines_added"], env["lines_removed"],
             env["files_touched"], env["doc_words"], env["interrupts"],
             env["re_prompts"], env["edits_without_read"], env["reasoning_loops"],
             env["premature_stops"], env["peak_context_tokens"],
             env["peak_context_pct"], env["compact_count"],
             env["clear_count"], run_id),
        )
    conn.commit()


def sweep_stale_runs(conn: sqlite3.Connection, max_idle_ms: int = 6 * 3600 * 1000,
                     now: str | None = None) -> list:
    """Finalize passive runs that were never closed because SessionEnd never fired.

    A crash, a `kill`, or a machine restart leaves a passive run open forever
    with zero aggregates, so it is silently missing from every report — 30% of
    runs were in that state. Only runs whose last turn is older than `max_idle_ms`
    are swept, so a session running right now in another terminal is never
    touched. Finalizing is idempotent (everything is recomputed from `turns`), so
    a run swept early self-heals when its real SessionEnd arrives.
    """
    now = now or now_iso()
    stale = []
    rows = conn.execute(
        """SELECT r.run_id, MAX(t.ended_at) AS last_seen
           FROM runs r JOIN turns t ON t.run_id = r.run_id
           WHERE r.capture_mode = 'passive' AND r.ended_at IS NULL
           GROUP BY r.run_id""").fetchall()
    for row in rows:
        idle = T.duration_ms(row["last_seen"], now)
        if idle is not None and idle >= max_idle_ms:
            stale.append(row["run_id"])
    for run_id in stale:
        finalize_run(conn, run_id, closed_by="stale-sweep")
    return stale


def run_capture_mode(conn: sqlite3.Connection, run_id: str) -> str | None:
    row = conn.execute(
        "SELECT capture_mode FROM runs WHERE run_id = ?", (run_id,)
    ).fetchone()
    return row[0] if row else None


# ----- tracked runs (per-session, with pause/resume) ------------------------
#
# Attribution is per session: each session has at most one *active* tracked run
# (a row in active_tracked). A tracked run that is open in `runs` but not in
# active_tracked is PAUSED — resumable. This lets sessions track different tasks
# in parallel, and lets a task be paused in one session and resumed in another.

def get_active_tracked_run(
    conn: sqlite3.Connection, session_id: str
) -> str | None:
    """The tracked run this session is actively attaching turns to, if any."""
    if not session_id:
        return None
    row = conn.execute(
        "SELECT run_id FROM active_tracked WHERE session_id = ?", (session_id,)
    ).fetchone()
    return row[0] if row else None


def _attach(conn: sqlite3.Connection, session_id: str, run_id: str) -> None:
    """Make `run_id` the active tracked run for `session_id`.

    Clears any prior attachment for this session AND any stale attachment of
    this run to another (e.g. crashed) session, so the invariants
    (session_id unique, run_id unique) always hold.
    """
    conn.execute("DELETE FROM active_tracked WHERE session_id = ? OR run_id = ?",
                 (session_id, run_id))
    conn.execute(
        "INSERT INTO active_tracked (session_id, run_id, attached_at) "
        "VALUES (?, ?, ?)", (session_id, run_id, now_iso()))


def pause_tracked_run(
    conn: sqlite3.Connection, session_id: str
) -> str | None:
    """Detach (auto-pause) this session's active tracked run without finalizing
    it. Returns the paused run_id, or None if the session had none active."""
    run_id = get_active_tracked_run(conn, session_id)
    if run_id is None:
        return None
    conn.execute("DELETE FROM active_tracked WHERE session_id = ?", (session_id,))
    conn.commit()
    return run_id


def start_tracked_run(
    conn: sqlite3.Connection,
    session_id: str,
    label: str,
    task_type: str | None,
    size_class: str | None,
    intended_approach: str | None,
    project: str | None,
) -> tuple[str, str | None]:
    """Open a tracked run and make it this session's active run.

    If the session was already tracking another run, that run is auto-paused
    (kept open, resumable). Returns (new_run_id, auto_paused_run_id | None).
    """
    paused = get_active_tracked_run(conn, session_id)
    run_id = new_run_id()
    conn.execute(
        """INSERT INTO runs
           (run_id, capture_mode, project, started_at, task_label, task_type,
            size_class, intended_approach, source)
           VALUES (?, 'tracked', ?, ?, ?, ?, ?, ?, ?)""",
        (run_id, project, now_iso(), label, task_type, size_class,
         intended_approach, SOURCE),
    )
    _attach(conn, session_id, run_id)
    conn.commit()
    return run_id, (paused if paused != run_id else None)


def resume_tracked_run(
    conn: sqlite3.Connection, session_id: str, selector: str
) -> tuple[str | None, str | None, list]:
    """Reattach a paused tracked run to this session.

    `selector` matches an open tracked run by exact run_id or by task label.
    Any run currently active in this session is auto-paused first.

    Returns (resumed_run_id, auto_paused_run_id, ambiguous_matches):
      * resumed_run_id set on success;
      * (None, _, [])          -> no paused run matched;
      * (None, _, [rows...])   -> selector matched several — caller disambiguates.
    """
    candidates = _open_paused_matches(conn, selector)
    if not candidates:
        return None, None, []
    if len(candidates) > 1:
        return None, None, candidates
    run_id = candidates[0]["run_id"]
    paused = get_active_tracked_run(conn, session_id)
    _attach(conn, session_id, run_id)
    conn.commit()
    return run_id, (paused if paused != run_id else None), []


def _open_paused_matches(conn: sqlite3.Connection, selector: str) -> list:
    """Open tracked runs (not yet done) that are currently paused and match
    `selector` by exact run_id or by task label."""
    rows = conn.execute(
        """SELECT r.run_id, r.task_label, r.task_type, r.size_class
           FROM runs r
           WHERE r.capture_mode = 'tracked' AND r.ended_at IS NULL
             AND r.run_id NOT IN (SELECT run_id FROM active_tracked)
             AND (r.run_id = ? OR r.task_label = ?)""",
        (selector, selector),
    ).fetchall()
    return [dict(r) for r in rows]


def list_open_tracked_runs(conn: sqlite3.Connection) -> list:
    """All open (not-done) tracked runs with their state — active (and in which
    session) or paused. Powers /track-list and resume disambiguation."""
    rows = conn.execute(
        """SELECT r.run_id, r.task_label, r.task_type, r.size_class,
                  r.started_at, a.session_id AS active_session
           FROM runs r
           LEFT JOIN active_tracked a ON a.run_id = r.run_id
           WHERE r.capture_mode = 'tracked' AND r.ended_at IS NULL
           ORDER BY r.started_at""",
    ).fetchall()
    return [dict(r) for r in rows]


def finish_tracked_run(
    conn: sqlite3.Connection,
    session_id: str,
    outcome: str,
    satisfaction: int | None,
    note: str | None,
    run_id: str | None = None,
) -> str | None:
    """Finalize a tracked run with its self-reported outcome and detach it.

    Targets `run_id` when given (to close a paused run directly), else this
    session's active run. Returns the run_id, or None if nothing matched.
    """
    if run_id is None:
        run_id = get_active_tracked_run(conn, session_id)
    else:
        ok = conn.execute(
            "SELECT 1 FROM runs WHERE run_id = ? AND capture_mode = 'tracked' "
            "AND ended_at IS NULL", (run_id,)).fetchone()
        if ok is None:
            return None
    if run_id is None:
        return None
    finalize_run(conn, run_id, closed_by="track-done")
    conn.execute(
        """UPDATE runs SET outcome = ?, outcome_source = 'self_report',
                           satisfaction = ?, note = ?
           WHERE run_id = ?""",
        (outcome, satisfaction, note, run_id),
    )
    conn.execute("DELETE FROM active_tracked WHERE run_id = ?", (run_id,))
    conn.commit()
    return run_id
