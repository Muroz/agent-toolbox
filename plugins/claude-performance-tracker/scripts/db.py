"""Database location and initialization for claude-performance-tracker.

The SQLite file lives in the plugin's persistent data directory
(${CLAUDE_PLUGIN_DATA}), which survives plugin updates and is cleaned up on
uninstall. Hook scripts receive that path in their env. Skill/CLI invocations
run in the session shell, which does NOT inherit ${CLAUDE_PLUGIN_DATA}, and the
path Claude Code hands the hooks is install-source-suffixed (e.g.
`…-agent-toolbox`), so a plain unsuffixed guess misses the data — see
`_discover_populated_dir` for how the read side finds the DB the hooks wrote to.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


CANONICAL_DIR = (
    Path.home() / ".claude" / "plugins" / "data" / "claude-performance-tracker")


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

    Claude Code hands the hooks a ${CLAUDE_PLUGIN_DATA} that is *suffixed* with
    the install source — e.g. `claude-performance-tracker-agent-toolbox` for a
    marketplace install, `…-inline` for `--plugin-dir` dev mode. So the plain
    unsuffixed name is almost never where the captured data lives. When we have
    no env var to go on (the skills/CLI run in the session shell, which doesn't
    inherit it), scan the sibling dirs and pick the populated one — most turns
    wins, newest mtime breaks ties. Returns None if none hold any turns.
    """
    base = CANONICAL_DIR.parent
    best: Path | None = None
    best_key = (0, 0.0)  # (turn_count, mtime); turn_count 0 never wins
    for dbfile in base.glob("claude-performance-tracker*/usage.db"):
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
    return data_dir(explicit_dir) / "usage.db"


def connect(explicit_dir: str | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path(explicit_dir))
    conn.row_factory = sqlite3.Row
    return conn


SCHEMA_VERSION = "4"

# Columns added after a database may already have been created. `CREATE TABLE IF
# NOT EXISTS` is a no-op on an existing table, so without this list an upgrade
# silently keeps the old column set and every hook write fails — invisibly, since
# hooks swallow their exceptions. Append here whenever schema.sql gains a column.
ADDED_COLUMNS = (
    ("turns", "cache_creation_1h_tokens", "INTEGER DEFAULT 0"),
    ("turns", "total_tokens_agg", "INTEGER DEFAULT 0"),
    ("turns", "active_ms", "INTEGER"),
    ("turns", "is_prompt", "INTEGER NOT NULL DEFAULT 1"),
    ("turns", "agent_type", "TEXT"),
    ("runs", "cache_creation_1h_tokens", "INTEGER DEFAULT 0"),
    ("runs", "total_tokens_agg", "INTEGER DEFAULT 0"),
    ("runs", "active_ms", "INTEGER"),
    ("runs", "peak_context_tokens", "INTEGER"),
)

# Tables superseded by a later design that must not be left behind holding data
# nobody reads. `open_run` was the pre-0.3.0 singleton tracked-run pointer,
# replaced by the per-session `active_tracked`.
DROPPED_TABLES = ("open_run",)


def _columns(conn: sqlite3.Connection, table: str) -> set:
    try:
        return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return set()


def _migrate(conn: sqlite3.Connection) -> list[str]:
    """Bring an existing database up to the current schema. Returns what changed."""
    applied = []
    for table, column, decl in ADDED_COLUMNS:
        cols = _columns(conn, table)
        if not cols or column in cols:
            continue
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
            applied.append(f"+{table}.{column}")
        except sqlite3.Error:
            pass
    for table in DROPPED_TABLES:
        if _columns(conn, table):
            try:
                conn.execute(f"DROP TABLE {table}")
                applied.append(f"-{table}")
            except sqlite3.Error:
                pass
    conn.execute(
        "INSERT INTO schema_meta (key, value) VALUES ('version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (SCHEMA_VERSION,))
    conn.commit()
    return applied


def init_db(explicit_dir: str | None = None) -> list[str]:
    """Idempotently create AND migrate the schema. Safe on every SessionStart."""
    conn = connect(explicit_dir)
    try:
        conn.executescript(SCHEMA_PATH.read_text())
        conn.commit()
        return _migrate(conn)
    finally:
        conn.close()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Initialize the usage database.")
    parser.add_argument("--data-dir", default=None)
    args = parser.parse_args()
    applied = init_db(args.data_dir)
    print(f"initialized {db_path(args.data_dir)}")
    if applied:
        print("migrated: " + ", ".join(applied))
