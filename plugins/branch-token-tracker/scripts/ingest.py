"""Hook entrypoint: capture this session's turns against the branch's ticket.

    btt ingest --event SessionStart|Stop|SessionEnd [--data-dir DIR]

Payload arrives as JSON on stdin (session_id, transcript_path, cwd).

The branch is resolved *per capture*, not once per session, which is the whole
reason a Stop hook is used rather than only SessionEnd: switch branches halfway
through a session and the turns before the switch stay on the old ticket while
everything after lands on the new one.

Nothing in here may raise. A hook that fails is a hook that interrupts real
work, so `main` swallows everything and exits 0.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config  # noqa: E402
import db  # noqa: E402
import transcript  # noqa: E402

EVENTS = ("SessionStart", "Stop", "SessionEnd")


def _payload() -> dict:
    try:
        raw = sys.stdin.read()
    except OSError:
        return {}
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _project(cwd: str | None) -> str | None:
    return os.path.basename(cwd.rstrip("/")) if cwd else None


def current_branch(cwd: str | None) -> str | None:
    """The checked-out branch, or None outside a repo / on a detached HEAD.

    Short timeout and a swallowed failure: a slow or broken git must not stall
    the end of every turn.
    """
    if not cwd or not os.path.isdir(cwd):
        return None
    try:
        p = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, timeout=2)
    except (OSError, subprocess.SubprocessError):
        return None
    if p.returncode != 0:
        return None
    branch = p.stdout.strip()
    return None if not branch or branch == "HEAD" else branch


def capture(conn, session_id: str, transcript_path: str, *, project: str | None,
            branch: str | None, ticket: str) -> int:
    """Insert turns not already stored. Returns the number written.

    Insert-only by `turn_id`, so re-reading the whole transcript after every
    Stop is cheap and a turn's ticket is pinned to the branch it ran on.
    """
    if not transcript_path or not os.path.exists(transcript_path):
        return 0
    seen = {r[0] for r in conn.execute("SELECT turn_id FROM turns")}
    written = 0
    for t in transcript.parse_turns(transcript_path):
        if t.turn_id in seen:
            continue
        conn.execute(
            """INSERT INTO turns
               (turn_id, session_id, project, branch, ticket, model,
                started_at, ended_at, input_tokens, output_tokens,
                cache_read_tokens, cache_creation_tokens, num_tool_calls)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (t.turn_id, session_id, project, branch, ticket, t.model,
             t.started_at, t.ended_at, t.input_tokens, t.output_tokens,
             t.cache_read_tokens, t.cache_creation_tokens, t.num_tool_calls))
        written += 1
    conn.commit()
    return written


def _totals(conn, where: str, params: tuple) -> dict:
    row = conn.execute(
        f"""SELECT COUNT(*), COALESCE(SUM(input_tokens),0),
                   COALESCE(SUM(output_tokens),0),
                   COALESCE(SUM(cache_read_tokens),0),
                   COALESCE(SUM(cache_creation_tokens),0)
            FROM turns WHERE {where}""", params).fetchone()
    return {"turns": row[0], "input_tokens": row[1], "output_tokens": row[2],
            "cache_read_tokens": row[3], "cache_creation_tokens": row[4],
            "total_tokens": row[1] + row[2] + row[3] + row[4]}


def write_current(data_dir, payload: dict) -> None:
    """Drop the current ticket's totals where a statusline or script can read it.

    SessionEnd hook stdout is not surfaced anywhere the user sees, so this file
    — not the echo below it — is what makes the live total actually reachable.
    """
    try:
        (Path(data_dir) / "current.json").write_text(
            json.dumps(payload, indent=2) + "\n")
    except OSError:
        pass


def run(event: str, data_dir: str | None) -> None:
    payload = _payload()
    session_id = payload.get("session_id") or "unknown"
    cwd = payload.get("cwd")
    transcript_path = payload.get("transcript_path") or ""
    project = _project(cwd)
    branch = current_branch(cwd)
    ticket = config.ticket_for(branch, cwd=cwd)

    db.init_db(data_dir)
    conn = db.connect(data_dir)
    try:
        if event != "SessionStart":
            capture(conn, session_id, transcript_path, project=project,
                    branch=branch, ticket=ticket)

        totals = _totals(conn, "ticket = ?", (ticket,))
        sessions = conn.execute(
            "SELECT COUNT(DISTINCT session_id) FROM turns WHERE ticket = ?",
            (ticket,)).fetchone()[0]

        if event == "SessionStart":
            # SessionStart stdout is added to the session context, so this is
            # the one place a running total can greet the user unprompted.
            if totals["turns"]:
                print(f"[branch-token-tracker] {ticket} so far: "
                      f"{totals['total_tokens']:,} tokens across {sessions} "
                      f"session(s), {totals['turns']} turn(s).")
        elif event == "SessionEnd":
            session_totals = _totals(conn, "session_id = ?", (session_id,))
            write_current(db.data_dir(data_dir), {
                "ticket": ticket, "branch": branch, "project": project,
                "session_id": session_id,
                "session_tokens": session_totals["total_tokens"],
                "session_turns": session_totals["turns"],
                "ticket_tokens": totals["total_tokens"],
                "ticket_turns": totals["turns"],
                "ticket_sessions": sessions,
                "updated_at": payload.get("timestamp"),
            })
            print(f"[branch-token-tracker] {ticket}: "
                  f"+{session_totals['total_tokens']:,} this session, "
                  f"{totals['total_tokens']:,} total.")
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Capture turns per branch ticket.")
    parser.add_argument("--event", required=True, choices=EVENTS)
    parser.add_argument("--data-dir", default=None)
    args = parser.parse_args()
    try:
        run(args.event, args.data_dir)
    except Exception:  # noqa: BLE001 — a hook must never break the session
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
