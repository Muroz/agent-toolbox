"""Hook entrypoint: capture this session's turns against the branch's ticket.

    btt ingest --event SessionStart|Stop|SessionEnd [--data-dir DIR]

Payload arrives as JSON on stdin (session_id, transcript_path, cwd).

The branch is resolved *per capture*, not once per session, which is the whole
reason a Stop hook is used rather than only SessionEnd: switch branches halfway
through a session and the turns before the switch stay on the old ticket while
everything after lands on the new one.

Nothing in here may raise. A hook that fails is a hook that interrupts real
work, so `main` swallows everything and exits 0. Set $BTT_DEBUG=1 to print the
traceback to stderr instead — without it, a plugin that has silently stopped
capturing looks exactly like one that is working.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config  # noqa: E402
import cost  # noqa: E402
import db  # noqa: E402
import transcript  # noqa: E402

EVENTS = ("SessionStart", "Stop", "SessionEnd")

# Events once registered in hooks.json and since retired. Claude Code snapshots
# hooks.json at session start while the scripts are read live off disk, so a
# session older than the edit keeps firing a retired hook at the current build.
# Retired events must stay listed here and be no-ops forever.
#
# RULE: never delete an event name — move it from EVENTS to RETIRED.
RETIRED: tuple[str, ...] = ()


def _payload() -> dict:
    # Hooks always pipe their JSON in. Run by hand from a terminal there is
    # nothing to read, and blocking on an interactive stdin would look like a
    # hang rather than a mistake.
    if sys.stdin is None or sys.stdin.isatty():
        return {}
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
            # The ticket attribution stays pinned to the branch the turn first
            # ran on, but the envelope is refreshed: a turn captured while the
            # assistant was still working would otherwise keep a fraction of its
            # real tokens forever. Counts only ever grow.
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
                       -- MAX(COALESCE(...)) would quietly turn the first
                       -- into the second on every re-run.
                       active_ms = CASE WHEN ? > COALESCE(active_ms, 0)
                                        THEN ? ELSE active_ms END,
                       num_tool_calls        = MAX(num_tool_calls, ?)
                   WHERE turn_id = ?""",
                (t.ended_at, t.model, t.agent_type, t.input_tokens,
                 t.output_tokens, t.cache_read_tokens, t.cache_creation_tokens,
                 t.cache_creation_1h_tokens, t.total_tokens_agg,
                 t.active_ms or 0, t.active_ms or 0, t.num_tool_calls, t.turn_id))
            continue
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
             t.cache_creation_1h_tokens, t.total_tokens_agg, t.num_tool_calls,
             t.active_ms, t.query_source, t.agent_type,
             1 if t.is_prompt else 0))
        written += 1
    conn.commit()
    return written


def _totals(conn, where: str, params: tuple) -> dict:
    row = conn.execute(
        f"""SELECT COUNT(*), COALESCE(SUM(input_tokens),0),
                   COALESCE(SUM(output_tokens),0),
                   COALESCE(SUM(cache_read_tokens),0),
                   COALESCE(SUM(cache_creation_tokens),0),
                   COALESCE(SUM(total_tokens_agg),0),
                   COALESCE(SUM({cost.WEIGHTED_SQL}),0)
            FROM turns WHERE {where}""", params).fetchone()
    return {"turns": row[0], "input_tokens": row[1], "output_tokens": row[2],
            "cache_read_tokens": row[3], "cache_creation_tokens": row[4],
            "total_tokens": row[1] + row[2] + row[3] + row[4] + row[5],
            "weighted_tokens": row[6]}


def write_current(data_dir, payload: dict) -> None:
    """Drop the current ticket's totals where a statusline or script can read it.

    Hook stdout is not surfaced anywhere the user sees, so this file — not the
    echo below it — is what makes the live total actually reachable. It is
    written on every Stop, not only at SessionEnd: a statusline refreshed once
    per session would spend the whole session showing the previous one's totals.
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

        if event in ("Stop", "SessionEnd"):
            session_totals = _totals(conn, "session_id = ?", (session_id,))
            write_current(db.data_dir(data_dir), {
                "ticket": ticket, "branch": branch, "project": project,
                "session_id": session_id,
                "session_tokens": session_totals["total_tokens"],
                "session_weighted": round(session_totals["weighted_tokens"]),
                "session_turns": session_totals["turns"],
                "ticket_tokens": totals["total_tokens"],
                "ticket_weighted": round(totals["weighted_tokens"]),
                "ticket_turns": totals["turns"],
                "ticket_sessions": sessions,
                # The hook payload carries no timestamp, so stamping it here is
                # the only way this field ever means anything.
                "updated_at": datetime.now(timezone.utc).isoformat(),
            })

        if event == "SessionStart":
            # SessionStart stdout is added to the session context, so this is
            # the one place a running total can greet the user unprompted.
            if totals["turns"]:
                print(f"[branch-token-tracker] {ticket} so far: "
                      f"{round(totals['weighted_tokens']):,} weighted tokens "
                      f"({totals['total_tokens']:,} raw) across {sessions} "
                      f"session(s), {totals['turns']} turn(s).")
        elif event == "SessionEnd":
            session_totals = _totals(conn, "session_id = ?", (session_id,))
            print(f"[branch-token-tracker] {ticket}: "
                  f"+{round(session_totals['weighted_tokens']):,} weighted "
                  f"tokens this session, "
                  f"{round(totals['weighted_tokens']):,} total.")
    finally:
        conn.close()


def main() -> int:
    """Parse, dispatch, and — whatever happens — exit 0.

    A hook's exit code is a control channel, not a diagnostic: on
    `UserPromptSubmit` an exit of 2 *blocks the user's prompt* and shows them
    our stderr. Claude Code snapshots hooks.json at session start, so a session
    older than an edit here keeps firing events this build no longer registers;
    rejecting those with argparse's exit-2 is what bricks the session. So the
    parser lives inside the try (argparse raises SystemExit, not Exception),
    unknown events are ignored rather than rejected, and the catch is
    BaseException.

    RULE: never delete a name from EVENTS — retiring it to a no-op is free,
    and an unknown event is a stale-config no-op, never an error.

    Set $BTT_DEBUG=1 to print what went wrong to stderr. Silence is the default
    because stderr on a blocking hook event is user-facing.
    """
    try:
        parser = argparse.ArgumentParser(
            description="Capture turns per branch ticket.", allow_abbrev=False)
        parser.add_argument("--event", default=None)
        parser.add_argument("--data-dir", default=None)
        args, extra = parser.parse_known_args()

        if args.event not in EVENTS:
            if os.environ.get("BTT_DEBUG"):
                print(f"btt ingest: ignoring event {args.event!r} "
                      f"(known: {', '.join(EVENTS)})", file=sys.stderr)
            return 0
        if extra and os.environ.get("BTT_DEBUG"):
            print(f"btt ingest: ignoring extra args {extra!r}", file=sys.stderr)

        run(args.event, args.data_dir)
    except BaseException:  # noqa: BLE001 — see the docstring; exit 0 is the contract
        if os.environ.get("BTT_DEBUG"):
            traceback.print_exc(file=sys.stderr)
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
