"""Data-dir resolution — the skill/CLI must read the DB the hooks wrote to.

The hooks get a marketplace-suffixed ${CLAUDE_PLUGIN_DATA}
(e.g. claude-performance-tracker-agent-toolbox); the skills/CLI run in the
session shell with no such env var. These tests pin the discovery that keeps
both halves on the same database.

    python3 -m unittest discover -s tests
"""

import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import db  # noqa: E402


class TestDataDirResolution(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        # Pretend this temp tree is ~/.claude/plugins/data/claude-performance-tracker
        self.canonical = self.tmp / "claude-performance-tracker"
        self._patch = mock.patch.object(db, "CANONICAL_DIR", self.canonical)
        self._patch.start()
        # Ensure no env var leaks in from the real session.
        self._env = mock.patch.dict(os.environ, {}, clear=False)
        self._env.start()
        os.environ.pop("CLAUDE_PLUGIN_DATA", None)

    def tearDown(self):
        self._patch.stop()
        self._env.stop()

    def _make_db(self, name: str, turns: int) -> Path:
        d = self.tmp / name
        db.init_db(str(d))
        if turns:
            conn = sqlite3.connect(d / "usage.db")
            conn.execute(
                "INSERT INTO runs(run_id, capture_mode, started_at) "
                "VALUES('r', 'passive', '2026-07-01T00:00:00Z')")
            for i in range(turns):
                conn.execute(
                    "INSERT INTO turns(turn_id, run_id, session_id) "
                    "VALUES(?, 'r', 's')", (f"t{i}",))
            conn.commit()
            conn.close()
        return d

    def test_explicit_dir_wins(self):
        d = self._make_db("explicit", 1)
        self.assertEqual(db.data_dir(str(d)), d)

    def test_env_var_wins_over_discovery(self):
        self._make_db("claude-performance-tracker-agent-toolbox", 5)
        envd = self.tmp / "claude-performance-tracker-inline"
        db.init_db(str(envd))
        with mock.patch.dict(os.environ, {"CLAUDE_PLUGIN_DATA": str(envd)}):
            self.assertEqual(db.data_dir(), envd)

    def test_discovers_populated_suffixed_dir(self):
        # Canonical unsuffixed dir is an empty stub; real data lives in a
        # marketplace-suffixed sibling. Discovery must pick the sibling.
        self._make_db("claude-performance-tracker", 0)
        populated = self._make_db(
            "claude-performance-tracker-agent-toolbox", 7)
        self.assertEqual(db.data_dir(), populated)

    def test_most_turns_wins_over_other_silos(self):
        self._make_db("claude-performance-tracker-inline", 4)
        big = self._make_db("claude-performance-tracker-agent-toolbox", 40)
        self.assertEqual(db.data_dir(), big)

    def test_falls_back_to_canonical_when_nothing_populated(self):
        # No sibling has turns -> canonical dir, created on demand.
        self.assertEqual(db.data_dir(), self.canonical)
        self.assertTrue(self.canonical.exists())


if __name__ == "__main__":
    unittest.main()
