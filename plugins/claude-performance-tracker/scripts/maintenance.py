"""Repair and housekeeping passes over an existing store.

Two commands, both safe to re-run:

  cpt backfill   Re-derive every session's turns from its transcript, replacing
                 what is stored. This is how a database written by a buggy
                 version gets corrected in place — turns whose envelope was
                 truncated are refilled, rows the parser no longer produces
                 (Claude Code's injected `<task-notification>` prompts, and main
                 turns the old SubagentStop hook mislabelled `subagent`) are
                 dropped, and every affected run's aggregates, signals and
                 inferred outcome are recomputed from the corrected turns.

  cpt sweep      Finalize passive runs abandoned by a crash or a killed
                 terminal, which otherwise sit open forever with no aggregates.

Backfill only touches sessions whose transcript still exists on disk; a session
whose transcript has been deleted keeps whatever was captured at the time.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3

import db
import infer_outcome
import store
import transcript as T


def _session_rows(conn: sqlite3.Connection) -> list:
    return conn.execute(
        "SELECT session_id, run_id, transcript_path FROM sessions").fetchall()


def rebuild_session(conn: sqlite3.Connection, session_id: str,
                    default_run: str, transcript_path: str) -> dict:
    """Replace a session's stored turns with what its transcript actually says.

    Run attribution is preserved per turn where it is already known, and carried
    forward from the nearest preceding known turn otherwise — so a session whose
    turns were split between a passive and a tracked run stays split correctly.
    """
    parsed = T.parse_turns(transcript_path)
    parsed_ids = {t.turn_id for t in parsed}

    stored = conn.execute(
        "SELECT turn_id, run_id, seq FROM turns WHERE session_id = ? "
        "ORDER BY seq, started_at", (session_id,)).fetchall()
    owner = {r["turn_id"]: r["run_id"] for r in stored}
    touched = {r["run_id"] for r in stored}

    dropped = [tid for tid in owner if tid not in parsed_ids]
    for tid in dropped:
        conn.execute("DELETE FROM turns WHERE turn_id = ?", (tid,))

    carried = default_run
    repaired = 0
    added = 0
    for t in parsed:
        run_id = owner.get(t.turn_id, carried)
        carried = run_id
        touched.add(run_id)
        exists = t.turn_id in owner
        conn.execute("DELETE FROM turns WHERE turn_id = ?", (t.turn_id,))
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
             t.prompt_text, store.SOURCE))
        repaired += 1 if exists else 0
        added += 0 if exists else 1
    conn.commit()
    return {"dropped": len(dropped), "repaired": repaired, "added": added,
            "runs": touched}


def salvage_unrebuildable(conn: sqlite3.Connection) -> dict:
    """Correct what can be corrected on rows whose transcript is gone.

    A session with no transcript on disk cannot be re-derived, but two of the
    old bugs left evidence in the row itself:

      * A `subagent` row whose turn_id is not `agent:`-prefixed is a main turn
        the old SubagentStop hook mislabelled — the parser only ever mints
        `agent:<id>` for real subagent rows.
      * A row whose prompt_text starts with one of Claude Code's injected
        prefixes was never a human prompt, so it must not count as one.

    Both are cheap, reversible-by-re-backfill corrections that stop these rows
    skewing prompt counts and the query-source split forever.
    """
    relabelled = conn.execute(
        "UPDATE turns SET query_source = 'main' "
        "WHERE query_source = 'subagent' AND turn_id NOT LIKE 'agent:%'").rowcount
    marked = 0
    for prefix in T.SYNTHETIC_PREFIXES:
        marked += conn.execute(
            "UPDATE turns SET is_prompt = 0 "
            "WHERE is_prompt = 1 AND prompt_text LIKE ?", (prefix + "%",)).rowcount
    marked += conn.execute(
        "UPDATE turns SET is_prompt = 0 "
        "WHERE is_prompt = 1 AND turn_id LIKE 'agent:%'").rowcount
    conn.commit()
    return {"relabelled_main": relabelled, "marked_not_prompt": marked}


def backfill(conn: sqlite3.Connection, verbose: bool = False) -> dict:
    stats = {"sessions": 0, "skipped_no_transcript": 0, "dropped": 0,
             "repaired": 0, "added": 0, "runs_recomputed": 0, "reinferred": {}}
    touched: set = set()
    for row in _session_rows(conn):
        path = row["transcript_path"]
        if not path or not os.path.exists(path):
            stats["skipped_no_transcript"] += 1
            continue
        res = rebuild_session(conn, row["session_id"], row["run_id"], path)
        stats["sessions"] += 1
        for k in ("dropped", "repaired", "added"):
            stats[k] += res[k]
        touched |= {r for r in res["runs"] if r}
        if verbose:
            print(f"  {row['session_id'][:8]} dropped={res['dropped']} "
                  f"repaired={res['repaired']} added={res['added']}")

    stats["salvaged"] = salvage_unrebuildable(conn)
    # Salvage can change is_prompt / query_source on runs the rebuild never
    # touched, so those runs need recomputing too.
    for row in conn.execute(
            "SELECT DISTINCT run_id FROM turns WHERE run_id IS NOT NULL"):
        touched.add(row[0])

    for run_id in sorted(touched):
        r = conn.execute(
            "SELECT ended_at, closed_by, capture_mode, outcome_source "
            "FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if r is None:
            continue
        # Keep an open run open; only recompute what its turns now say.
        store.finalize_run(conn, run_id,
                           closed_by=r["closed_by"] if r["ended_at"] else None)
        stats["runs_recomputed"] += 1
        if r["capture_mode"] == "passive" and r["outcome_source"] != "self_report":
            # The old inference leaned on friction detectors that never fired,
            # so every inferred label predates working evidence.
            conn.execute(
                "UPDATE runs SET outcome = NULL, outcome_source = NULL "
                "WHERE run_id = ?", (run_id,))
            label = infer_outcome.infer_and_store(conn, run_id)
            if label:
                stats["reinferred"][label] = stats["reinferred"].get(label, 0) + 1
    conn.commit()
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("backfill", help="re-derive stored turns from transcripts")
    b.add_argument("--data-dir", default=None)
    b.add_argument("--verbose", action="store_true")
    w = sub.add_parser("sweep", help="finalize abandoned open runs")
    w.add_argument("--data-dir", default=None)
    w.add_argument("--max-idle-hours", type=float, default=6.0)
    args = parser.parse_args()

    db.init_db(args.data_dir)
    conn = db.connect(args.data_dir)
    try:
        if args.cmd == "backfill":
            print(json.dumps(backfill(conn, args.verbose), indent=2, default=str))
        else:
            swept = store.sweep_stale_runs(
                conn, max_idle_ms=int(args.max_idle_hours * 3600 * 1000))
            print(json.dumps({"swept": swept, "count": len(swept)}, indent=2))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
