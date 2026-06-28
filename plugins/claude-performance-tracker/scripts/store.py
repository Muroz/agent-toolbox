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
    conn: sqlite3.Connection, run_id: str, session_id: str, transcript_path: str
) -> int:
    """Rebuild this session's turns from the transcript and upsert them.

    Idempotent: keyed by turn_id (the user prompt uuid).
    """
    turns = T.parse_turns(transcript_path)
    for t in turns:
        conn.execute(
            """INSERT OR REPLACE INTO turns
               (turn_id, run_id, session_id, seq, started_at, ended_at, model,
                query_source, input_tokens, output_tokens, cache_read_tokens,
                cache_creation_tokens, num_tool_calls, prompt_text, source)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (t.turn_id, run_id, session_id, t.seq, t.started_at, t.ended_at,
             t.model, t.query_source, t.input_tokens, t.output_tokens,
             t.cache_read_tokens, t.cache_creation_tokens, t.num_tool_calls,
             t.prompt_text, SOURCE),
        )
    conn.commit()
    return len(turns)


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
    conn.commit()


def run_capture_mode(conn: sqlite3.Connection, run_id: str) -> str | None:
    row = conn.execute(
        "SELECT capture_mode FROM runs WHERE run_id = ?", (run_id,)
    ).fetchone()
    return row[0] if row else None
