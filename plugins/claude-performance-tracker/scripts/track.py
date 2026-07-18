"""CLI behind the /track, /track-done, /track-pause, /track-resume, /track-list skills.

Tracking is per session: each session (identified by --session-id, which the
skills pass from $CLAUDE_CODE_SESSION_ID) attaches its turns to at most one
tracked run at a time. A run can be paused (detached, kept open) and resumed —
possibly in a different session — so tasks can be juggled and tracked in
parallel across sessions.

    cpt track start  --session-id S --label "..." --type feature --size M --approach "..."
    cpt track pause  --session-id S
    cpt track resume --session-id S --run "<run-id-or-label>"
    cpt track done   --session-id S [--run <id>] --outcome success --satisfaction 4
    cpt track list
"""

from __future__ import annotations

import argparse
import os

import db
import store

TASK_TYPES = ["bugfix", "feature", "refactor", "research", "debug", "other"]
SIZES = ["S", "M", "L"]
OUTCOMES = ["success", "partial", "failed"]


def _require_session(args) -> str | None:
    sid = (args.session_id or "").strip()
    if not sid:
        print("No session id. This needs $CLAUDE_CODE_SESSION_ID — your Claude "
              "Code may be too old for per-session tracking.")
        return None
    return sid


def _fmt(run) -> str:
    return (f"{run['run_id']}  {run.get('task_label') or '(no label)'}  "
            f"[{run.get('task_type') or '-'} / {run.get('size_class') or '-'}]")


def cmd_start(args) -> int:
    sid = _require_session(args)
    if sid is None:
        return 0
    conn = db.connect(args.data_dir)
    try:
        project = args.project or os.path.basename(os.getcwd()) or None
        run_id, paused = store.start_tracked_run(
            conn, sid, args.label, args.type, args.size, args.approach, project)
        msg = (f"▶ Tracking started — {run_id}\n"
               f"  {args.label}  [{args.type or '-'} / {args.size or '-'}]\n"
               f"  approach: {args.approach or '(unspecified)'}")
        if paused:
            msg += f"\n  (auto-paused your previous run {paused} — resume it later)"
        msg += "\nWork the task, then close with /track-done."
        print(msg)
        return 0
    finally:
        conn.close()


def cmd_pause(args) -> int:
    sid = _require_session(args)
    if sid is None:
        return 0
    conn = db.connect(args.data_dir)
    try:
        run_id = store.pause_tracked_run(conn, sid)
        if run_id is None:
            print("This session has no active tracked run to pause.")
            return 0
        print(f"⏸ Paused — {run_id}\n"
              f"  Resume it with /track-resume (here or in another session).")
        return 0
    finally:
        conn.close()


def cmd_resume(args) -> int:
    sid = _require_session(args)
    if sid is None:
        return 0
    conn = db.connect(args.data_dir)
    try:
        run_id, paused, ambiguous = store.resume_tracked_run(conn, sid, args.run)
        if run_id is None and ambiguous:
            lines = "\n".join("  " + _fmt(r) for r in ambiguous)
            print("Several paused runs match that label — resume by run id:\n"
                  + lines)
            return 0
        if run_id is None:
            print(f"No paused tracked run matches '{args.run}'. "
                  f"See open runs with /track-list.")
            return 0
        msg = f"▶ Resumed — {run_id}"
        if paused:
            msg += f"\n  (auto-paused your previous run {paused})"
        msg += "\nContinue the task, then close with /track-done."
        print(msg)
        return 0
    finally:
        conn.close()


def cmd_done(args) -> int:
    sid = _require_session(args)
    if sid is None:
        return 0
    conn = db.connect(args.data_dir)
    try:
        run_id = store.finish_tracked_run(
            conn, sid, args.outcome, args.satisfaction, args.note, args.run)
        if run_id is None:
            if args.run:
                print(f"No open tracked run '{args.run}' to close.")
            else:
                print("This session has no active tracked run. Resume one with "
                      "/track-resume, or see /track-list.")
            return 0
        print(f"■ Tracking finished — {run_id}\n"
              f"  outcome: {args.outcome}  satisfaction: {args.satisfaction}/5")
        return 0
    finally:
        conn.close()


def cmd_list(args) -> int:
    conn = db.connect(args.data_dir)
    try:
        runs = store.list_open_tracked_runs(conn)
        if not runs:
            print("No open tracked runs. Start one with /track.")
            return 0
        print("Open tracked runs:")
        for r in runs:
            state = (f"active in session {r['active_session'][:8]}…"
                     if r["active_session"] else "paused")
            print(f"  {_fmt(r)}  — {state}")
        return 0
    finally:
        conn.close()


def main() -> int:
    p = argparse.ArgumentParser(description="Open/close/pause/resume tracked runs.")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("start", help="open a tracked run for this session")
    s.add_argument("--session-id", default=None)
    s.add_argument("--label", required=True)
    s.add_argument("--type", choices=TASK_TYPES, default=None)
    s.add_argument("--size", choices=SIZES, default=None)
    s.add_argument("--approach", default=None)
    s.add_argument("--project", default=None)
    s.add_argument("--data-dir", default=None)

    pa = sub.add_parser("pause", help="detach this session's active tracked run")
    pa.add_argument("--session-id", default=None)
    pa.add_argument("--data-dir", default=None)

    r = sub.add_parser("resume", help="reattach a paused run to this session")
    r.add_argument("--session-id", default=None)
    r.add_argument("--run", required=True, help="run id or task label")
    r.add_argument("--data-dir", default=None)

    d = sub.add_parser("done", help="close a tracked run with its outcome")
    d.add_argument("--session-id", default=None)
    d.add_argument("--run", default=None, help="close this run id (else the active one)")
    d.add_argument("--outcome", required=True, choices=OUTCOMES)
    d.add_argument("--satisfaction", type=int, choices=[1, 2, 3, 4, 5], required=True)
    d.add_argument("--note", default=None)
    d.add_argument("--data-dir", default=None)

    ls = sub.add_parser("list", help="show open (active/paused) tracked runs")
    ls.add_argument("--data-dir", default=None)

    args = p.parse_args()
    db.init_db(args.data_dir)
    return {
        "start": cmd_start, "pause": cmd_pause, "resume": cmd_resume,
        "done": cmd_done, "list": cmd_list,
    }[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
