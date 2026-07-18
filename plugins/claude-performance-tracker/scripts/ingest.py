"""Hook entrypoint for claude-performance-tracker.

Invoked by every lifecycle hook with `--event <HookEventName>`. Reads the hook's
JSON payload from stdin (which includes `session_id`, `transcript_path`, `cwd`,
`permission_mode`, and event-specific fields) and updates the SQLite store.

Design: lifecycle hooks are cheap and only maintain boundaries/markers; the `Stop`
event does the heavy lifting by parsing `transcript_path` for the turn envelope.
Run finalization happens at boundary events (SessionEnd / clear / track-done).

This is a SCAFFOLD. Each handler is stubbed and tracked as a tracer-bullet issue.
A hook must never block the session: on any error we exit 0 and stay silent.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import db
import infer_outcome
import store


def _read_payload() -> dict:
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
    finally:
        conn.close()


def on_user_prompt_submit(payload: dict, data_dir: str | None) -> None:
    # Turn capture happens at Stop (when the assistant's usage is known). Nothing
    # to do here yet; /clear boundary handling is a later slice.
    pass


def _capture(payload: dict, data_dir: str | None) -> tuple[str, str] | None:
    """Shared Stop/SessionEnd path: attribute this session's new turns to the
    active run and capture them.

    This session's active tracked run (if any) takes precedence over its passive
    run, so turns produced while tracking attach to the tracked run. Attribution
    is per session, so two sessions tracking different tasks never cross over.

    Returns (run_id, session_id) — the run the turns were attributed to.
    """
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


def on_subagent_stop(payload: dict, data_dir: str | None) -> None:
    """Attribute a finished subagent's token usage to the parent run.

    The payload's transcript_path points at the subagent's own (sidechain)
    transcript. Its turns attach to whichever run the parent session is feeding
    (open tracked run, else the session's passive run), tagged query_source=subagent.
    """
    session_id = payload.get("session_id")
    transcript = _transcript(payload)
    if not session_id or not transcript:
        return
    conn = db.connect(data_dir)
    try:
        run_id = store.get_active_tracked_run(conn, session_id) \
            or store.get_run_for_session(conn, session_id)
        if run_id is None:
            run_id = store.open_passive_run(
                conn, session_id, None, _project(payload))
        store.capture_session_turns(
            conn, run_id, session_id, transcript,
            query_source="subagent", include_sidechain=True)
    finally:
        conn.close()


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
    "UserPromptSubmit": on_user_prompt_submit,
    "Stop": on_stop,
    "SubagentStop": on_subagent_stop,
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
        # Never break the user's session because tracking failed.
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
