"""Database location and initialization for branch-token-tracker.

The SQLite file lives in the plugin's persistent data directory
(${CLAUDE_PLUGIN_DATA}), which survives plugin updates and is cleaned up on
uninstall. Hook scripts receive that path in their env. Skill/CLI invocations
run in the session shell, which does NOT inherit ${CLAUDE_PLUGIN_DATA}, and the
path Claude Code hands the hooks is install-source-suffixed (e.g.
`…-agent-toolbox` for a marketplace install, `…-inline` for --plugin-dir), so a
plain unsuffixed guess reads an empty database while the real one sits next to
it. `_discover_populated_dir` is how the read side finds the file the hooks
actually wrote to.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

SCHEMA_PATH = Path(__file__).with_name("schema.sql")

CANONICAL_DIR = (
    Path.home() / ".claude" / "plugins" / "data" / "branch-token-tracker")

DB_NAME = "tokens.db"


def _turn_count(dbfile: Path) -> int:
    """Number of turns in a DB file, or 0 if it can't be read as one."""
    try:
        conn = sqlite3.connect(f"file:{dbfile}?mode=ro", uri=True)
    except sqlite3.Error:
        return 0
    try:
        return conn.execute("SELECT COUNT(*) FROM turns").fetchone()[0]
    except sqlite3.Error:
        return 0
    finally:
        conn.close()


def _discover_populated_dir() -> Path | None:
    """Find the data dir the hooks actually wrote to.

    Scans the sibling `branch-token-tracker*` dirs and picks the populated one —
    most turns wins, newest mtime breaks ties. Returns None if none hold data.
    """
    base = CANONICAL_DIR.parent
    best: Path | None = None
    best_key = (0, 0.0)  # (turn_count, mtime); turn_count 0 never wins
    for dbfile in base.glob(f"branch-token-tracker*/{DB_NAME}"):
        n = _turn_count(dbfile)
        if n == 0:
            continue
        try:
            mtime = dbfile.stat().st_mtime
        except OSError:
            continue
        if (n, mtime) > best_key:
            best_key, best = (n, mtime), dbfile.parent
    return best


def data_dir(explicit: str | None = None) -> Path:
    """Resolve the writable data directory.

    Order: explicit --data-dir arg, then $CLAUDE_PLUGIN_DATA (set for hooks),
    then — for skill/CLI invocations that have no env var — the populated
    sibling dir the hooks wrote to, falling back to the canonical unsuffixed
    dir when nothing is populated yet (fresh install / tests).
    """
    candidate = explicit or os.environ.get("CLAUDE_PLUGIN_DATA")
    if candidate:
        path = Path(candidate)
    else:
        path = _discover_populated_dir() or CANONICAL_DIR
    path.mkdir(parents=True, exist_ok=True)
    return path


def db_path(explicit_dir: str | None = None) -> Path:
    return data_dir(explicit_dir) / DB_NAME


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

    parser = argparse.ArgumentParser(description="Initialize the token database.")
    parser.add_argument("--data-dir", default=None)
    args = parser.parse_args()
    init_db(args.data_dir)
    print(f"initialized {db_path(args.data_dir)}")
