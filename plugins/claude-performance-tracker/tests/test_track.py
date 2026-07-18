"""Per-session tracked runs with pause/resume (/track, /track-done,
/track-pause, /track-resume, /track-list).

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


def _write(path: Path, uuids) -> None:
    """Write a transcript containing exactly the prompts named in `uuids`."""
    rows = []
    for k in uuids:
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
        _write(self.tpath, [1])
        ingest.on_session_start(self.payload, self.tmp)
        ingest.on_stop(self.payload, self.tmp)              # turn 1 -> passive
        conn = self._conn()
        tracked, paused = store.start_tracked_run(
            conn, "sess-A", "exp", "feature", "M", "plan", None)
        conn.close()
        _write(self.tpath, [1, 2])
        ingest.on_stop(self.payload, self.tmp)              # turn 2 -> tracked
        ingest.on_session_end(self.payload, self.tmp)       # passive closes; tracked paused
        return tracked

    def test_start_sets_active_run(self):
        db.init_db(self.tmp)
        conn = self._conn()
        run_id, paused = store.start_tracked_run(
            conn, "sess-A", "x", "bugfix", "S", None, None)
        self.assertIsNone(paused)
        self.assertEqual(store.get_active_tracked_run(conn, "sess-A"), run_id)
        self.assertEqual(
            conn.execute("SELECT capture_mode FROM runs WHERE run_id=?",
                         (run_id,)).fetchone()[0], "tracked")

    def test_turns_attribute_by_active_state(self):
        tracked = self._scenario()
        conn = self._conn()
        passive = store.get_run_for_session(conn, "sess-A")
        u1_run = conn.execute("SELECT run_id FROM turns WHERE turn_id='u1'").fetchone()[0]
        u2_run = conn.execute("SELECT run_id FROM turns WHERE turn_id='u2'").fetchone()[0]
        self.assertEqual(u1_run, passive)   # pre-/track turn stays passive
        self.assertEqual(u2_run, tracked)   # during-tracking turn attaches to tracked

    def test_session_end_pauses_not_finalizes_tracked(self):
        tracked = self._scenario()
        conn = self._conn()
        # tracked run still open (not finalized) after SessionEnd...
        self.assertIsNone(
            conn.execute("SELECT closed_by FROM runs WHERE run_id=?",
                         (tracked,)).fetchone()[0])
        # ...but detached (paused): no session actively points at it.
        self.assertIsNone(store.get_active_tracked_run(conn, "sess-A"))
        self.assertEqual([r["run_id"] for r in store.list_open_tracked_runs(conn)],
                         [tracked])
        # the passive run, however, was closed at SessionEnd
        passive = store.get_run_for_session(conn, "sess-A")
        self.assertEqual(
            conn.execute("SELECT closed_by FROM runs WHERE run_id=?",
                         (passive,)).fetchone()[0], "SessionEnd")

    def test_done_records_outcome_and_detaches(self):
        tracked = self._scenario()
        conn = self._conn()
        # resume then finish (mirrors the real /track-resume ... /track-done flow)
        store.resume_tracked_run(conn, "sess-A", tracked)
        returned = store.finish_tracked_run(conn, "sess-A", "success", 4, "great")
        self.assertEqual(returned, tracked)
        row = conn.execute(
            "SELECT outcome, outcome_source, satisfaction, note, closed_by, "
            "input_tokens, output_tokens FROM runs WHERE run_id=?",
            (tracked,)).fetchone()
        self.assertEqual(
            tuple(row), ("success", "self_report", 4, "great", "track-done", 20, 40))
        self.assertIsNone(store.get_active_tracked_run(conn, "sess-A"))
        self.assertEqual(store.list_open_tracked_runs(conn), [])

    def test_done_with_no_active_run_returns_none(self):
        db.init_db(self.tmp)
        conn = self._conn()
        self.assertIsNone(
            store.finish_tracked_run(conn, "sess-A", "success", 5, None))


class TestParallelSessions(unittest.TestCase):
    """Case 1: two sessions tracking different tasks at the same time."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        db.init_db(self.tmp)

    def _sess(self, sid, uuids):
        tpath = Path(self.tmp) / f"{sid}.jsonl"
        _write(tpath, uuids)
        return {"session_id": sid, "transcript_path": str(tpath),
                "cwd": f"/home/me/{sid}"}

    def test_two_sessions_track_different_runs(self):
        pa = self._sess("A", [1])
        pb = self._sess("B", [2])
        ingest.on_session_start(pa, self.tmp)
        ingest.on_session_start(pb, self.tmp)

        conn = db.connect(self.tmp)
        run_a, _ = store.start_tracked_run(conn, "A", "task-A", "feature", "M", None, None)
        run_b, _ = store.start_tracked_run(conn, "B", "task-B", "bugfix", "S", None, None)
        conn.close()
        self.assertNotEqual(run_a, run_b)

        ingest.on_stop(pa, self.tmp)
        ingest.on_stop(pb, self.tmp)

        conn = db.connect(self.tmp)
        # each session's turn landed on its OWN tracked run — no cross-contamination
        self.assertEqual(
            conn.execute("SELECT run_id FROM turns WHERE turn_id='u1'").fetchone()[0],
            run_a)
        self.assertEqual(
            conn.execute("SELECT run_id FROM turns WHERE turn_id='u2'").fetchone()[0],
            run_b)
        self.assertEqual(store.get_active_tracked_run(conn, "A"), run_a)
        self.assertEqual(store.get_active_tracked_run(conn, "B"), run_b)
        conn.close()


class TestPauseResumeAcrossSessions(unittest.TestCase):
    """Case 2: track A in session 1, do & finish B in session 2, resume A in
    session 3."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        db.init_db(self.tmp)

    def _sess(self, sid, uuids):
        tpath = Path(self.tmp) / f"{sid}.jsonl"
        _write(tpath, uuids)
        return {"session_id": sid, "transcript_path": str(tpath),
                "cwd": "/home/me/proj"}

    def test_full_lifecycle(self):
        # --- Session 1: track A, one turn, then session ends (A auto-pauses) ---
        s1 = self._sess("S1", [1])
        ingest.on_session_start(s1, self.tmp)
        conn = db.connect(self.tmp)
        run_a, _ = store.start_tracked_run(conn, "S1", "task A", "feature", "L", None, None)
        conn.close()
        ingest.on_stop(s1, self.tmp)           # u1 -> A
        ingest.on_session_end(s1, self.tmp)    # A paused, not finalized

        conn = db.connect(self.tmp)
        self.assertIsNone(store.get_active_tracked_run(conn, "S1"))
        self.assertEqual([r["run_id"] for r in store.list_open_tracked_runs(conn)],
                         [run_a])  # A is open + paused
        conn.close()

        # --- Session 2: track B, finish it. A must stay untouched. ---
        s2 = self._sess("S2", [2])
        ingest.on_session_start(s2, self.tmp)
        conn = db.connect(self.tmp)
        run_b, paused_by_b = store.start_tracked_run(
            conn, "S2", "task B", "bugfix", "S", None, None)
        conn.close()
        self.assertNotEqual(run_a, run_b)
        self.assertIsNone(paused_by_b)         # different session, nothing auto-paused
        ingest.on_stop(s2, self.tmp)           # u2 -> B
        conn = db.connect(self.tmp)
        done_b = store.finish_tracked_run(conn, "S2", "success", 5, None)
        self.assertEqual(done_b, run_b)
        # A is still open+paused; B is done
        self.assertEqual([r["run_id"] for r in store.list_open_tracked_runs(conn)],
                         [run_a])
        conn.close()
        ingest.on_session_end(s2, self.tmp)

        # --- Session 3: resume A, add a turn, finish A ---
        s3 = self._sess("S3", [3])
        ingest.on_session_start(s3, self.tmp)
        conn = db.connect(self.tmp)
        resumed, paused, ambiguous = store.resume_tracked_run(conn, "S3", "task A")
        conn.close()
        self.assertEqual(resumed, run_a)
        self.assertIsNone(paused)
        self.assertEqual(ambiguous, [])
        ingest.on_stop(s3, self.tmp)           # u3 -> A (resumed)
        conn = db.connect(self.tmp)
        done_a = store.finish_tracked_run(conn, "S3", "success", 4, "wrapped up")
        self.assertEqual(done_a, run_a)

        # A owns turns from session 1 AND session 3 (cross-session run)
        a_turns = {r[0] for r in conn.execute(
            "SELECT turn_id FROM turns WHERE run_id=?", (run_a,))}
        self.assertEqual(a_turns, {"u1", "u3"})
        b_turns = {r[0] for r in conn.execute(
            "SELECT turn_id FROM turns WHERE run_id=?", (run_b,))}
        self.assertEqual(b_turns, {"u2"})
        self.assertEqual(store.list_open_tracked_runs(conn), [])  # all closed
        conn.close()

    def test_resume_ambiguous_label_lists_candidates(self):
        conn = db.connect(self.tmp)
        r1, _ = store.start_tracked_run(conn, "X", "dup", "feature", "M", None, None)
        store.pause_tracked_run(conn, "X")
        r2, _ = store.start_tracked_run(conn, "Y", "dup", "feature", "M", None, None)
        store.pause_tracked_run(conn, "Y")
        run_id, paused, ambiguous = store.resume_tracked_run(conn, "Z", "dup")
        self.assertIsNone(run_id)
        self.assertEqual({c["run_id"] for c in ambiguous}, {r1, r2})
        # resuming by exact run id disambiguates
        got, _, _ = store.resume_tracked_run(conn, "Z", r1)
        self.assertEqual(got, r1)
        conn.close()


class TestTrackCli(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def _cpt(self, *args):
        return subprocess.run(
            [sys.executable, str(SCRIPTS / "track.py"), *args, "--data-dir", self.tmp],
            capture_output=True, text=True)

    def test_done_without_active_run_message(self):
        p = self._cpt("done", "--session-id", "S", "--outcome", "success",
                      "--satisfaction", "3")
        self.assertEqual(p.returncode, 0)
        self.assertIn("no active tracked run", p.stdout.lower())

    def test_start_missing_session_id_is_explained(self):
        p = self._cpt("start", "--session-id", "", "--label", "a")
        self.assertIn("session id", p.stdout.lower())

    def test_start_pause_resume_done_cli_flow(self):
        self.assertIn("Tracking started",
                      self._cpt("start", "--session-id", "S", "--label", "job").stdout)
        self.assertIn("Paused", self._cpt("pause", "--session-id", "S").stdout)
        self.assertIn("paused", self._cpt("list").stdout)
        self.assertIn("Resumed",
                      self._cpt("resume", "--session-id", "S", "--run", "job").stdout)
        self.assertIn("finished",
                      self._cpt("done", "--session-id", "S", "--outcome", "success",
                                "--satisfaction", "5").stdout)

    def test_start_twice_auto_pauses_previous(self):
        self._cpt("start", "--session-id", "S", "--label", "first")
        out = self._cpt("start", "--session-id", "S", "--label", "second").stdout
        self.assertIn("auto-paused", out)


if __name__ == "__main__":
    unittest.main()
