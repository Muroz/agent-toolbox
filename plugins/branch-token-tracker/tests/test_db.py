"""Data-dir resolution — the skill/CLI must read the DB the hooks wrote to.

The hooks get a marketplace-suffixed ${CLAUDE_PLUGIN_DATA}
(e.g. branch-token-tracker-agent-toolbox); `/token-report` runs in the session
shell with no such env var. Getting this wrong ships a report that cheerfully
prints "no turns captured yet" next to a full database, so it is pinned here.

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
        # Pretend this temp tree is ~/.claude/plugins/data/branch-token-tracker
        self.canonical = self.tmp / "branch-token-tracker"
        self._patch = mock.patch.object(db, "CANONICAL_DIR", self.canonical)
        self._patch.start()
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
            conn = sqlite3.connect(d / db.DB_NAME)
            for i in range(turns):
                conn.execute(
                    "INSERT INTO turns(turn_id, session_id, ticket) "
                    "VALUES(?, 's', 'PROJ-1')", (f"t{i}",))
            conn.commit()
            conn.close()
        return d

    def test_explicit_dir_wins(self):
        d = self._make_db("explicit", 1)
        self.assertEqual(db.data_dir(str(d)), d)

    def test_env_var_wins_over_discovery(self):
        self._make_db("branch-token-tracker-agent-toolbox", 5)
        envd = self.tmp / "branch-token-tracker-inline"
        db.init_db(str(envd))
        with mock.patch.dict(os.environ, {"CLAUDE_PLUGIN_DATA": str(envd)}):
            self.assertEqual(db.data_dir(), envd)

    def test_discovers_populated_suffixed_dir(self):
        # The unsuffixed dir is an empty stub; the real data lives in the
        # marketplace-suffixed sibling. Discovery must pick the sibling.
        self._make_db("branch-token-tracker", 0)
        populated = self._make_db("branch-token-tracker-agent-toolbox", 7)
        self.assertEqual(db.data_dir(), populated)

    def test_most_turns_wins_over_other_silos(self):
        self._make_db("branch-token-tracker-inline", 4)
        big = self._make_db("branch-token-tracker-agent-toolbox", 40)
        self.assertEqual(db.data_dir(), big)

    def test_falls_back_to_canonical_when_nothing_populated(self):
        self.assertEqual(db.data_dir(), self.canonical)
        self.assertTrue(self.canonical.exists())

    def test_a_sibling_that_is_not_a_database_is_ignored(self):
        junk = self.tmp / "branch-token-tracker-broken"
        junk.mkdir(parents=True)
        (junk / db.DB_NAME).write_text("this is not sqlite")
        populated = self._make_db("branch-token-tracker-agent-toolbox", 3)
        self.assertEqual(db.data_dir(), populated)

    def test_the_other_plugins_data_dir_is_never_picked_up(self):
        # claude-performance-tracker lives in the same parent directory and also
        # has a `turns` table — the glob must not stray into it.
        other = self.tmp / "claude-performance-tracker-agent-toolbox"
        other.mkdir(parents=True)
        conn = sqlite3.connect(other / db.DB_NAME)
        conn.execute("CREATE TABLE turns (turn_id TEXT)")
        for i in range(99):
            conn.execute("INSERT INTO turns VALUES (?)", (f"t{i}",))
        conn.commit()
        conn.close()
        mine = self._make_db("branch-token-tracker-agent-toolbox", 2)
        self.assertEqual(db.data_dir(), mine)


if __name__ == "__main__":
    unittest.main()
