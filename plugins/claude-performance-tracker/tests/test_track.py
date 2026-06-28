"""Slice 4 — tracked runs (/track + /track-done).

    python3 -m unittest discover -s tests
"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import db  # noqa: E402
import ingest  # noqa: E402
import store  # noqa: E402


def _pair(k):
    return [
        {"type": "user", "uuid": f"u{k}", "timestamp": f"2026-06-28T10:0{k}:00Z",
         "message": {"role": "user", "content": f"task {k}"}},
        {"type": "assistant", "uuid": f"a{k}x", "timestamp": f"2026-06-28T10:0{k}:05Z",
         "message": {"role": "assistant", "id": f"a{k}", "model": "claude-opus-4-8",
                     "content": [{"type": "text", "text": "ok"}],
                     "usage": {"input_tokens": 10 * k, "output_tokens": 20 * k,
                               "cache_read_input_tokens": 0,
                               "cache_creation_input_tokens": 0}}},
    ]


def _write(path: Path, n_turns: int) -> None:
    rows = []
    for k in range(1, n_turns + 1):
        rows += _pair(k)
    with open(path, "w") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")


class TestTrackedRuns(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.tpath = Path(self.tmp) / "t.jsonl"
        self.payload = {"session_id": "sess-A", "transcript_path": str(self.tpath),
                        "cwd": "/home/me/proj"}

    def _conn(self):
        return db.connect(self.tmp)

    def _scenario(self):
        """turn 1 passive, then /track, then turn 2 (tracked), then SessionEnd."""
        _write(self.tpath, 1)
        ingest.on_session_start(self.payload, self.tmp)
        ingest.on_stop(self.payload, self.tmp)              # turn 1 -> passive
        conn = self._conn()
        tracked = store.start_tracked_run(conn, "exp", "feature", "M", "plan", None)
        conn.close()
        _write(self.tpath, 2)
        ingest.on_stop(self.payload, self.tmp)              # turn 2 -> tracked
        ingest.on_session_end(self.payload, self.tmp)       # passive closes; tracked open
        return tracked

    def test_start_sets_open_run(self):
        db.init_db(self.tmp)
        conn = self._conn()
        run_id = store.start_tracked_run(conn, "x", "bugfix", "S", None, None)
        self.assertEqual(store.get_open_tracked_run(conn), run_id)
        self.assertEqual(
            conn.execute("SELECT capture_mode FROM runs WHERE run_id=?",
                         (run_id,)).fetchone()[0], "tracked")

    def test_turns_attribute_by_pointer_state(self):
        tracked = self._scenario()
        conn = self._conn()
        passive = store.get_run_for_session(conn, "sess-A")
        u1_run = conn.execute("SELECT run_id FROM turns WHERE turn_id='u1'").fetchone()[0]
        u2_run = conn.execute("SELECT run_id FROM turns WHERE turn_id='u2'").fetchone()[0]
        self.assertEqual(u1_run, passive)   # pre-/track turn stays passive
        self.assertEqual(u2_run, tracked)   # during-tracking turn attaches to tracked

    def test_tracked_survives_session_end(self):
        tracked = self._scenario()
        conn = self._conn()
        # tracked run still open after SessionEnd
        self.assertEqual(store.get_open_tracked_run(conn), tracked)
        self.assertIsNone(
            conn.execute("SELECT closed_by FROM runs WHERE run_id=?",
                         (tracked,)).fetchone()[0])
        # the passive run, however, was closed at SessionEnd
        passive = store.get_run_for_session(conn, "sess-A")
        self.assertEqual(
            conn.execute("SELECT closed_by FROM runs WHERE run_id=?",
                         (passive,)).fetchone()[0], "SessionEnd")

    def test_done_records_outcome_and_clears_pointer(self):
        tracked = self._scenario()
        conn = self._conn()
        returned = store.finish_tracked_run(conn, "success", 4, "great")
        self.assertEqual(returned, tracked)
        row = conn.execute(
            "SELECT outcome, outcome_source, satisfaction, note, closed_by, "
            "input_tokens, output_tokens FROM runs WHERE run_id=?",
            (tracked,)).fetchone()
        self.assertEqual(
            tuple(row), ("success", "self_report", 4, "great", "track-done", 20, 40))
        self.assertIsNone(store.get_open_tracked_run(conn))

    def test_done_with_no_open_run_returns_none(self):
        db.init_db(self.tmp)
        conn = self._conn()
        self.assertIsNone(store.finish_tracked_run(conn, "success", 5, None))


class TestTrackCli(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def _cpt(self, *args):
        return subprocess.run(
            [sys.executable, str(SCRIPTS / "track.py"), *args, "--data-dir", self.tmp],
            capture_output=True, text=True)

    def test_done_without_open_run_message(self):
        p = self._cpt("done", "--outcome", "success", "--satisfaction", "3")
        self.assertEqual(p.returncode, 0)
        self.assertIn("No tracked run is open", p.stdout)

    def test_start_then_double_start_guard(self):
        self.assertIn("Tracking started", self._cpt("start", "--label", "a").stdout)
        self.assertIn("already open", self._cpt("start", "--label", "b").stdout)


if __name__ == "__main__":
    unittest.main()
