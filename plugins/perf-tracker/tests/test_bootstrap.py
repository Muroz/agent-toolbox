"""Slice 1 — installable plugin + DB bootstrap.

Dependency-free (stdlib unittest) so it runs anywhere with just python3:

    python3 -m unittest discover -s tests
"""

import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import db  # noqa: E402  (after sys.path injection)

EXPECTED_TABLES = {"runs", "turns", "scores", "judge_verdicts", "active_tracked"}


def tables(database: Path) -> set:
    conn = sqlite3.connect(database)
    try:
        return {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()


class TestDbInit(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_init_creates_all_tables(self):
        db.init_db(self.tmp)
        self.assertTrue(EXPECTED_TABLES.issubset(tables(db.db_path(self.tmp))))

    def test_init_is_idempotent(self):
        db.init_db(self.tmp)
        db.init_db(self.tmp)  # must not raise
        self.assertTrue(EXPECTED_TABLES.issubset(tables(db.db_path(self.tmp))))

    def test_explicit_dir_wins(self):
        self.assertEqual(db.data_dir(self.tmp), Path(self.tmp))

    def test_env_var_fallback(self):
        env_dir = tempfile.mkdtemp()
        old = os.environ.get("CLAUDE_PLUGIN_DATA")
        os.environ["CLAUDE_PLUGIN_DATA"] = env_dir
        try:
            self.assertEqual(db.data_dir(None), Path(env_dir))
        finally:
            if old is None:
                del os.environ["CLAUDE_PLUGIN_DATA"]
            else:
                os.environ["CLAUDE_PLUGIN_DATA"] = old


class TestSessionStartHook(unittest.TestCase):
    """The hook contract: invoked as a subprocess, it bootstraps the DB and
    must never fail in a way that blocks the session (always exit 0)."""

    def _run(self, stdin: str, data_dir: str) -> int:
        return subprocess.run(
            [sys.executable, str(SCRIPTS / "ingest.py"),
             "--event", "SessionStart", "--data-dir", data_dir],
            input=stdin, text=True, capture_output=True,
        ).returncode

    def test_hook_bootstraps_db(self):
        tmp = tempfile.mkdtemp()
        rc = self._run(
            '{"session_id":"s1","hook_event_name":"SessionStart"}', tmp)
        self.assertEqual(rc, 0)
        self.assertTrue(db.db_path(tmp).exists())
        self.assertTrue(EXPECTED_TABLES.issubset(tables(db.db_path(tmp))))

    def test_hook_survives_garbage_stdin(self):
        tmp = tempfile.mkdtemp()
        self.assertEqual(self._run("not json at all", tmp), 0)

    def test_hook_survives_empty_stdin(self):
        tmp = tempfile.mkdtemp()
        self.assertEqual(self._run("", tmp), 0)


if __name__ == "__main__":
    unittest.main()
