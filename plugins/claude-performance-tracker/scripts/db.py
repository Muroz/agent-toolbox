"""Database location and initialization for claude-performance-tracker.

The SQLite file lives in the plugin's persistent data directory
(${CLAUDE_PLUGIN_DATA}), which survives plugin updates and is cleaned up on
uninstall. Hook scripts receive that path; everything else resolves it the same way.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def data_dir(explicit: str | None = None) -> Path:
    """Resolve the writable data directory.

    Order: explicit --data-dir arg, then $CLAUDE_PLUGIN_DATA, then a local
    fallback under the user's home for ad-hoc/manual runs.
    """
    candidate = explicit or os.environ.get("CLAUDE_PLUGIN_DATA")
    if not candidate:
        # Canonical installed plugin data dir. Both the hooks (which pass
        # ${CLAUDE_PLUGIN_DATA}) and skill-invoked scripts (which have no such
        # env var) resolve to the same DB here.
        candidate = str(
            Path.home() / ".claude" / "plugins" / "data" / "claude-performance-tracker")
    path = Path(candidate)
    path.mkdir(parents=True, exist_ok=True)
    return path


def db_path(explicit_dir: str | None = None) -> Path:
    return data_dir(explicit_dir) / "usage.db"


def connect(explicit_dir: str | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path(explicit_dir))
    conn.row_factory = sqlite3.Row
    return conn


def init_db(explicit_dir: str | None = None) -> None:
    """Idempotently create the schema. Safe to call on every SessionStart."""
    conn = connect(explicit_dir)
    try:
        conn.executescript(SCHEMA_PATH.read_text())
        conn.commit()
    finally:
        conn.close()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Initialize the usage database.")
    parser.add_argument("--data-dir", default=None)
    args = parser.parse_args()
    init_db(args.data_dir)
    print(f"initialized {db_path(args.data_dir)}")
