"""Repair passes over an existing store. Safe to re-run.

  btt backfill   Re-read every captured session's transcript and store what the
                 current parser finds. This is how a database written by an
                 older, buggier parser gets corrected in place.

The case this exists for: async subagents report their spend in a
`<task-notification>`, and the parser used to scan only `type=user` records for
one. Agents whose notification arrived as an `attachment` were dropped entirely
— in the session that exposed it, two of four agents and 60% of the subagent
tokens. Those numbers are still sitting in the transcript, so they are
recoverable; nothing here invents a figure it cannot read.

Attribution rule for a newly-found turn: it inherits the ticket and branch of
the most recent *already-stored* turn in the same session that started no later
than it did. A session can span several branches — the whole point of resolving
the branch at every Stop — so re-resolving the branch now, from a repo that has
since moved on, would silently re-file historical spend under today's ticket.
The stored attribution is the contemporaneous record and it is left alone.

Backfill only touches sessions whose transcript still exists on disk. A session
whose transcript has been deleted keeps whatever was captured at the time.
"""

from __future__ import annotations

import argparse
import glob
import os
import sqlite3

import db
import transcript

PROJECTS_DIR = os.path.expanduser("~/.claude/projects")


def find_transcript(session_id: str) -> str | None:
    """A session's transcript is named by its id, under some project dir."""
    hits = glob.glob(os.path.join(PROJECTS_DIR, "*", f"{session_id}.jsonl"))
    return hits[0] if hits else None


def _attribution(conn: sqlite3.Connection, session_id: str) -> list:
    """Stored (started_at, ticket, branch, project) for a session, in order."""
    return conn.execute(
        "SELECT started_at, ticket, branch, project FROM turns "
        "WHERE session_id = ? ORDER BY started_at", (session_id,)).fetchall()


def _attribute(rows: list, started_at: str | None) -> tuple:
    """The attribution in force when `started_at` happened.

    Falls back to the session's first stored turn, which covers a subagent that
    somehow predates every main turn we kept.
    """
    if not rows:
        return (None, None, None)
    best = rows[0]
    for r in rows:
        if r[0] and started_at and r[0] <= started_at:
            best = r
        elif r[0] and started_at and r[0] > started_at:
            break
    return (best[1], best[2], best[3])


def backfill(conn: sqlite3.Connection, verbose: bool = False) -> dict:
    stats = {"sessions": 0, "skipped_no_transcript": 0, "added": 0, "updated": 0}
    sessions = [r[0] for r in conn.execute(
        "SELECT DISTINCT session_id FROM turns ORDER BY session_id")]

    for session_id in sessions:
        path = find_transcript(session_id)
        if not path:
            stats["skipped_no_transcript"] += 1
            if verbose:
                print(f"  {session_id[:8]} no transcript on disk, left as-is")
            continue
        stats["sessions"] += 1

        stored = _attribution(conn, session_id)
        seen = {r[0]: r for r in conn.execute(
            "SELECT turn_id, total_tokens_agg, output_tokens FROM turns "
            "WHERE session_id = ?", (session_id,))}
        added = updated = 0

        for t in transcript.parse_turns(path):
            if t.turn_id in seen:
                # Counts only ever grow: a turn captured mid-flight would
                # otherwise keep a fraction of its real tokens forever.
                before = conn.execute(
                    "SELECT total_tokens_agg, output_tokens, num_tool_calls "
                    "FROM turns WHERE turn_id = ?", (t.turn_id,)).fetchone()
                conn.execute(
                    """UPDATE turns SET
                           ended_at = COALESCE(?, ended_at),
                           model    = COALESCE(?, model),
                           agent_type = COALESCE(?, agent_type),
                           input_tokens          = MAX(input_tokens, ?),
                           output_tokens         = MAX(output_tokens, ?),
                           cache_read_tokens     = MAX(cache_read_tokens, ?),
                           cache_creation_tokens = MAX(cache_creation_tokens, ?),
                           cache_creation_1h_tokens = MAX(cache_creation_1h_tokens, ?),
                           total_tokens_agg      = MAX(total_tokens_agg, ?),
                           -- NULL means unknown, 0 means it took no time.
                           -- MAX(COALESCE(...)) would quietly turn the
                           -- first into the second on every re-run.
                           active_ms = CASE WHEN ? > COALESCE(active_ms, 0)
                                            THEN ? ELSE active_ms END,
                           num_tool_calls        = MAX(num_tool_calls, ?)
                       WHERE turn_id = ?""",
                    (t.ended_at, t.model, t.agent_type, t.input_tokens,
                     t.output_tokens, t.cache_read_tokens,
                     t.cache_creation_tokens, t.cache_creation_1h_tokens,
                     t.total_tokens_agg, t.active_ms or 0, t.active_ms or 0, t.num_tool_calls,
                     t.turn_id))
                after = conn.execute(
                    "SELECT total_tokens_agg, output_tokens, num_tool_calls "
                    "FROM turns WHERE turn_id = ?", (t.turn_id,)).fetchone()
                if tuple(before) != tuple(after):
                    updated += 1
                continue

            ticket, branch, project = _attribute(stored, t.started_at)
            conn.execute(
                """INSERT INTO turns
                   (turn_id, session_id, project, branch, ticket, model,
                    started_at, ended_at, input_tokens, output_tokens,
                    cache_read_tokens, cache_creation_tokens,
                    cache_creation_1h_tokens, total_tokens_agg, num_tool_calls,
                    active_ms, query_source, agent_type, is_prompt)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (t.turn_id, session_id, project, branch, ticket, t.model,
                 t.started_at, t.ended_at, t.input_tokens, t.output_tokens,
                 t.cache_read_tokens, t.cache_creation_tokens,
                 t.cache_creation_1h_tokens, t.total_tokens_agg,
                 t.num_tool_calls, t.active_ms, t.query_source, t.agent_type,
                 1 if t.is_prompt else 0))
            added += 1

        stats["added"] += added
        stats["updated"] += updated
        if verbose:
            print(f"  {session_id[:8]} added={added} updated={updated}")

    conn.commit()
    return stats


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(description="Repair the token store.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    bf = sub.add_parser("backfill", help="re-derive turns from transcripts")
    bf.add_argument("--data-dir", default=None)
    bf.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    db.init_db(args.data_dir)
    conn = db.connect(args.data_dir)
    try:
        stats = backfill(conn, verbose=not args.quiet)
    finally:
        conn.close()
    print(f"[branch-token-tracker] backfill: {stats['sessions']} session(s) "
          f"re-read, {stats['added']} turn(s) added, "
          f"{stats['updated']} refreshed, "
          f"{stats['skipped_no_transcript']} skipped (no transcript).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
