"""Data-dir resolution — the skill/CLI must read the DB the hooks wrote to.

The hooks get a marketplace-suffixed ${CLAUDE_PLUGIN_DATA}
(e.g. perf-tracker-agent-toolbox); the skills/CLI run in the
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


class _TempDataRoot(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        # Pretend this temp tree is ~/.claude/plugins/data/perf-tracker
        self.canonical = self.tmp / "perf-tracker"
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


class TestDataDirResolution(_TempDataRoot):
    def test_explicit_dir_wins(self):
        d = self._make_db("explicit", 1)
        self.assertEqual(db.data_dir(str(d)), d)

    def test_env_var_wins_over_discovery(self):
        self._make_db("perf-tracker-agent-toolbox", 5)
        envd = self.tmp / "perf-tracker-inline"
        db.init_db(str(envd))
        with mock.patch.dict(os.environ, {"CLAUDE_PLUGIN_DATA": str(envd)}):
            self.assertEqual(db.data_dir(), envd)

    def test_discovers_populated_suffixed_dir(self):
        # Canonical unsuffixed dir is an empty stub; real data lives in a
        # marketplace-suffixed sibling. Discovery must pick the sibling.
        self._make_db("perf-tracker", 0)
        populated = self._make_db(
            "perf-tracker-agent-toolbox", 7)
        self.assertEqual(db.data_dir(), populated)

    def test_most_turns_wins_over_other_silos(self):
        self._make_db("perf-tracker-inline", 4)
        big = self._make_db("perf-tracker-agent-toolbox", 40)
        self.assertEqual(db.data_dir(), big)

    def test_falls_back_to_canonical_when_nothing_populated(self):
        # No sibling has turns -> canonical dir, created on demand.
        self.assertEqual(db.data_dir(), self.canonical)
        self.assertTrue(self.canonical.exists())


class TestLegacyAdoption(_TempDataRoot):
    """The 0.9.0 rename: history under the old name is copied across once."""

    def _legacy(self, suffix: str, turns: int) -> Path:
        return self._make_db("claude-performance-tracker" + suffix, turns)

    def _turns(self, d: Path) -> int:
        return db._turn_count(d / "usage.db")

    def test_new_hook_dir_adopts_legacy_history(self):
        self._legacy("-agent-toolbox", 9)
        new = self.tmp / "perf-tracker-agent-toolbox"
        self.assertEqual(db.data_dir(str(new)), new)
        self.assertEqual(self._turns(new), 9)

    def test_most_populated_legacy_dir_wins(self):
        self._legacy("-inline", 3)
        self._legacy("-agent-toolbox", 12)
        new = self.tmp / "perf-tracker-agent-toolbox"
        db.data_dir(str(new))
        self.assertEqual(self._turns(new), 12)

    def test_legacy_dir_is_left_intact(self):
        old = self._legacy("-agent-toolbox", 5)
        db.data_dir(str(self.tmp / "perf-tracker-agent-toolbox"))
        self.assertEqual(self._turns(old), 5)

    def test_existing_db_is_never_overwritten(self):
        new = self._make_db("perf-tracker-agent-toolbox", 2)
        self._legacy("-agent-toolbox", 20)
        db.data_dir(str(new))
        self.assertEqual(self._turns(new), 2)

    def test_cli_without_env_adopts_into_canonical(self):
        self._legacy("-agent-toolbox", 4)
        self.assertEqual(db.data_dir(), self.canonical)
        self.assertEqual(self._turns(self.canonical), 4)

    def test_dir_outside_plugin_data_never_adopts(self):
        self._legacy("-agent-toolbox", 6)
        elsewhere = Path(tempfile.mkdtemp()) / "perf-tracker-x"
        db.data_dir(str(elsewhere))
        self.assertFalse((elsewhere / "usage.db").exists())


if __name__ == "__main__":
    unittest.main()
