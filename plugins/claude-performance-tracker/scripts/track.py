"""CLI behind the /track and /track-done skills.

Invoked (via the `cpt` launcher on PATH, or directly) to open and close tracked
runs. It needs no session id: a tracked run is a global, session-independent
unit, and the Stop hook attaches turns to it via the open_run pointer.

    cpt start --label "..." --type feature --size M --approach "plan-mode, opus-4-8"
    cpt done  --outcome success --satisfaction 4 --note "..."
"""

from __future__ import annotations

import argparse
import os

import db
import store

TASK_TYPES = ["bugfix", "feature", "refactor", "research", "debug", "other"]
SIZES = ["S", "M", "L"]
OUTCOMES = ["success", "partial", "failed"]


def cmd_start(args) -> int:
    conn = db.connect(args.data_dir)
    try:
        existing = store.get_open_tracked_run(conn)
        if existing:
            print(f"A tracked run is already open ({existing}).\n"
                  f"Close it with /track-done before starting another.")
            return 0
        project = args.project or os.path.basename(os.getcwd()) or None
        run_id = store.start_tracked_run(
            conn, args.label, args.type, args.size, args.approach, project)
        print(f"▶ Tracking started — {run_id}\n"
              f"  {args.label}  [{args.type or '-'} / {args.size or '-'}]\n"
              f"  approach: {args.approach or '(unspecified)'}\n"
              f"Work the task, then close with /track-done.")
        return 0
    finally:
        conn.close()


def cmd_done(args) -> int:
    conn = db.connect(args.data_dir)
    try:
        run_id = store.finish_tracked_run(
            conn, args.outcome, args.satisfaction, args.note)
        if run_id is None:
            print("No tracked run is open. Start one with /track.")
            return 0
        print(f"■ Tracking finished — {run_id}\n"
              f"  outcome: {args.outcome}  satisfaction: {args.satisfaction}/5")
        return 0
    finally:
        conn.close()


def main() -> int:
    p = argparse.ArgumentParser(description="Open/close tracked performance runs.")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("start", help="open a tracked run")
    s.add_argument("--label", required=True)
    s.add_argument("--type", choices=TASK_TYPES, default=None)
    s.add_argument("--size", choices=SIZES, default=None)
    s.add_argument("--approach", default=None)
    s.add_argument("--project", default=None)
    s.add_argument("--data-dir", default=None)

    d = sub.add_parser("done", help="close the open tracked run")
    d.add_argument("--outcome", required=True, choices=OUTCOMES)
    d.add_argument("--satisfaction", type=int, choices=[1, 2, 3, 4, 5], required=True)
    d.add_argument("--note", default=None)
    d.add_argument("--data-dir", default=None)

    args = p.parse_args()
    db.init_db(args.data_dir)
    return cmd_start(args) if args.cmd == "start" else cmd_done(args)


if __name__ == "__main__":
    raise SystemExit(main())
