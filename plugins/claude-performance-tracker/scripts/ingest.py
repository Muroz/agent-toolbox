"""Hook entrypoint for claude-performance-tracker.

Invoked by every lifecycle hook with `--event <HookEventName>`. Reads the hook's
JSON payload from stdin (which includes `session_id`, `transcript_path`, `cwd`,
`permission_mode`, and event-specific fields) and updates the SQLite store.

Design: lifecycle hooks are cheap and only maintain boundaries/markers; the `Stop`
event does the heavy lifting by parsing `transcript_path` for the turn envelope.
Run finalization happens at boundary events (SessionEnd / stale sweep / track-done).

There is deliberately no `SubagentStop` hook. It used to exist, and it was
actively destructive: its payload's `transcript_path` is the *main* transcript,
so firing mid-turn it captured the in-flight main turn with only the tokens
produced so far, mislabelled it `subagent`, and pinned it — after which the real
`Stop` skipped it as already-seen. Turns that spawned a subagent lost ~89% of
their output tokens. Subagent spend is read from the `Agent` tool results in the
main transcript instead (see transcript.py), which is both complete and correct.

There is also no `UserPromptSubmit` hook: turn capture happens at `Stop`, when
the usage is actually known, so that hook spawned a Python process per prompt to
do nothing.

A hook must never block the session: on any error we exit 0 and stay silent. Set
$CPT_DEBUG=1 to print the traceback to stderr instead of swallowing it — without
it, a broken plugin looks exactly like a working one.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import traceback

import db
import infer_outcome
import store


def _read_payload() -> dict:
    # Hooks always pipe their JSON in. Run by hand from a terminal there is
    # nothing to read, and blocking on an interactive stdin would look like a
    # hang rather than a mistake.
    if sys.stdin is None or sys.stdin.isatty():
        return {}
    try:
        raw = sys.stdin.read()
        return json.loads(raw) if raw.strip() else {}
    except Exception:
        return {}


def _project(payload: dict) -> str | None:
    cwd = payload.get("cwd")
    return os.path.basename(cwd.rstrip("/")) if cwd else None


def _transcript(payload: dict) -> str | None:
    path = payload.get("transcript_path")
    return path if path and os.path.exists(path) else None


def on_session_start(payload: dict, data_dir: str | None) -> None:
    db.init_db(data_dir)
    session_id = payload.get("session_id")
    if not session_id:
        return
    conn = db.connect(data_dir)
    try:
        store.open_passive_run(
            conn, session_id, payload.get("transcript_path"), _project(payload))
        # Close out runs abandoned by a crash or a killed terminal, which would
        # otherwise sit open forever with zero aggregates and never be reported.
        store.sweep_stale_runs(conn)
    finally:
        conn.close()


def _capture(payload: dict, data_dir: str | None) -> tuple[str, str] | None:
    """Shared Stop/SessionEnd path: attribute this session's new turns to the
    active run and capture them.

    This session's active tracked run (if any) takes precedence over its passive
    run, so turns produced while tracking attach to the tracked run. Attribution
    is per session, so two sessions tracking different tasks never cross over.

    Returns (run_id, session_id) — the run the turns were attributed to.
    """
    # SessionStart is not guaranteed to have run first: a plugin installed
    # mid-session sees its very first event at Stop. Without this the schema is
    # missing and every capture fails — silently, since hooks swallow errors.
    db.init_db(data_dir)
    session_id = payload.get("session_id")
    transcript = _transcript(payload)
    if not session_id or not transcript:
        return None
    conn = db.connect(data_dir)
    try:
        # Always ensure the session has its own passive run (so SessionEnd can
        # close it regardless of any tracked run).
        passive = store.get_run_for_session(conn, session_id)
        if passive is None:
            passive = store.open_passive_run(
                conn, session_id, transcript, _project(payload))

        run_id = store.get_active_tracked_run(conn, session_id) or passive
        store.capture_session_turns(conn, run_id, session_id, transcript)
        return run_id, session_id
    finally:
        conn.close()


def on_stop(payload: dict, data_dir: str | None) -> None:
    _capture(payload, data_dir)


def on_session_end(payload: dict, data_dir: str | None) -> None:
    result = _capture(payload, data_dir)
    if result is None:
        return
    _, session_id = result
    conn = db.connect(data_dir)
    try:
        # Auto-pause (detach, don't finalize) any tracked run this session was
        # driving, so it becomes resumable in a later session rather than being
        # stranded as "active" in a session that no longer exists. Only
        # /track-done ever finalizes a tracked run.
        store.pause_tracked_run(conn, session_id)
        # Close the session's own passive run.
        passive = store.get_run_for_session(conn, session_id)
        if passive and store.run_capture_mode(conn, passive) == "passive":
            store.finalize_run(conn, passive, closed_by="SessionEnd")
            infer_outcome.infer_and_store(conn, passive)
    finally:
        conn.close()


HANDLERS = {
    "SessionStart": on_session_start,
    "Stop": on_stop,
    "SessionEnd": on_session_end,
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--event", required=True, choices=sorted(HANDLERS))
    parser.add_argument("--data-dir", default=None)
    args = parser.parse_args()

    handler = HANDLERS[args.event]
    try:
        handler(_read_payload(), args.data_dir)
    except Exception:
        # Never break the user's session because tracking failed — but do not
        # make a broken plugin indistinguishable from a working one either.
        if os.environ.get("CPT_DEBUG"):
            traceback.print_exc(file=sys.stderr)
        return 0
    return 0

if __name__ == "__main__":
    sys.exit(main())
