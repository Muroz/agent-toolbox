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
    query_source: str = "main", include_sidechain: bool = False,
) -> int:
    """Insert not-yet-seen turns from a transcript, attributed to `run_id`.

    Insert-only (never rewrite): a turn's run attribution is fixed when it is
    first captured — based on whichever run was active at that Stop — so flipping
    the tracked/passive pointer mid-session never re-labels earlier turns. Idempotent
    because turn_id (the user prompt uuid) is the primary key.

    `query_source` tags the turns ('main' or 'subagent'); `include_sidechain`
    is set when parsing a subagent's own transcript.
    """
    seen = {r[0] for r in conn.execute("SELECT turn_id FROM turns")}
    inserted = 0
    for t in T.parse_turns(transcript_path, include_sidechain=include_sidechain):
        if t.turn_id in seen:
            continue
        conn.execute(
            """INSERT INTO turns
               (turn_id, run_id, session_id, seq, started_at, ended_at, model,
                query_source, input_tokens, output_tokens, cache_read_tokens,
                cache_creation_tokens, num_tool_calls, prompt_text, source)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (t.turn_id, run_id, session_id, t.seq, t.started_at, t.ended_at,
             t.model, query_source, t.input_tokens, t.output_tokens,
             t.cache_read_tokens, t.cache_creation_tokens, t.num_tool_calls,
             t.prompt_text, SOURCE),
        )
        inserted += 1
    conn.commit()
    return inserted


def finalize_run(conn: sqlite3.Connection, run_id: str, closed_by: str) -> None:
    """Aggregate a run's turns into its `runs` row and close it.

    Run totals are the sum of its turns; wall-clock spans first turn start to
    last turn end.
    """
    agg = conn.execute(
        """SELECT
               COALESCE(SUM(input_tokens),0),
               COALESCE(SUM(output_tokens),0),
               COALESCE(SUM(cache_read_tokens),0),
               COALESCE(SUM(cache_creation_tokens),0),
               COALESCE(SUM(num_tool_calls),0),
               COUNT(*),
               MIN(started_at),
               MAX(ended_at),
               GROUP_CONCAT(DISTINCT model)
           FROM turns WHERE run_id = ?""",
        (run_id,),
    ).fetchone()
    (inp, out, cr, cc, tools, nprompts, first_start, last_end, models) = agg

    conn.execute(
        """UPDATE runs SET
               input_tokens = ?, output_tokens = ?, cache_read_tokens = ?,
               cache_creation_tokens = ?, num_tool_calls = ?, num_prompts = ?,
               models = ?, started_at = COALESCE(?, started_at), ended_at = ?,
               wall_clock_ms = ?, closed_by = ?
           WHERE run_id = ?""",
        (inp, out, cr, cc, tools, nprompts, models, first_start, last_end,
         T.duration_ms(first_start, last_end), closed_by, run_id),
    )

    # Deterministic envelope: approach descriptor, output, friction, context.
    env = signals.derive_run_envelope(conn, run_id)
    if env:
        conn.execute(
            """UPDATE runs SET
                   permission_mode = ?, subagents_used = ?, skills_used = ?,
                   mcp_tools_used = ?, lines_added = ?, lines_removed = ?,
                   files_touched = ?, doc_words = ?, interrupts = ?,
                   re_prompts = ?, edits_without_read = ?, reasoning_loops = ?,
                   premature_stops = ?, peak_context_pct = ?, compact_count = ?,
                   clear_count = ?
               WHERE run_id = ?""",
            (env["permission_mode"], env["subagents_used"], env["skills_used"],
             env["mcp_tools_used"], env["lines_added"], env["lines_removed"],
             env["files_touched"], env["doc_words"], env["interrupts"],
             env["re_prompts"], env["edits_without_read"], env["reasoning_loops"],
             env["premature_stops"], env["peak_context_pct"], env["compact_count"],
             env["clear_count"], run_id),
        )
    conn.commit()


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
