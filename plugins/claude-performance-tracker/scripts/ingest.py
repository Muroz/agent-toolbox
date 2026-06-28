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
    """Shared Stop/SessionEnd path: ensure a run exists and rebuild its turns.

    Returns (run_id, session_id) so the caller can decide whether to finalize.
    """
    session_id = payload.get("session_id")
    transcript = _transcript(payload)
    if not session_id or not transcript:
        return None
    conn = db.connect(data_dir)
    try:
        run_id = store.get_run_for_session(conn, session_id)
        if run_id is None:  # session predates install — open lazily
            run_id = store.open_passive_run(
                conn, session_id, transcript, _project(payload))
        store.capture_session_turns(conn, run_id, session_id, transcript)
        return run_id, session_id
    finally:
        conn.close()


def on_stop(payload: dict, data_dir: str | None) -> None:
    _capture(payload, data_dir)


def on_subagent_stop(payload: dict, data_dir: str | None) -> None:
    # Subagent token attribution is a later slice.
    pass


def on_session_end(payload: dict, data_dir: str | None) -> None:
    result = _capture(payload, data_dir)
    if result is None:
        return
    run_id, _ = result
    conn = db.connect(data_dir)
    try:
        # Only passive runs close at SessionEnd; a tracked run stays open until
        # /track-done.
        if store.run_capture_mode(conn, run_id) == "passive":
            store.finalize_run(conn, run_id, closed_by="SessionEnd")
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
