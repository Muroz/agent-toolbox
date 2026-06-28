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
import sys

import db


def _read_payload() -> dict:
    try:
        raw = sys.stdin.read()
        return json.loads(raw) if raw.strip() else {}
    except Exception:
        return {}


def on_session_start(payload: dict, data_dir: str | None) -> None:
    db.init_db(data_dir)
    # TODO(issue): open or resume a passive run for this session.


def on_user_prompt_submit(payload: dict, data_dir: str | None) -> None:
    # TODO(issue): advance the turn; detect /clear and other boundary markers.
    pass


def on_stop(payload: dict, data_dir: str | None) -> None:
    # TODO(issue): parse transcript_path; derive this turn's envelope into `turns`.
    pass


def on_subagent_stop(payload: dict, data_dir: str | None) -> None:
    # TODO(issue): attribute subagent token usage (query_source=subagent) to the run.
    pass


def on_session_end(payload: dict, data_dir: str | None) -> None:
    # TODO(issue): finalize the current passive run (aggregate turns -> runs row,
    # infer outcome). A tracked run stays open until /track-done.
    pass


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
