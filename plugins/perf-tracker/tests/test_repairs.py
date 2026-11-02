"""Regression cover for the audit fixes.

Each test here pins a behaviour that was previously wrong in a way no test
caught, because the old tests encoded the same mistaken assumption as the code.

    python3 -m unittest discover -s tests
"""

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import cost  # noqa: E402
import db  # noqa: E402
import ingest  # noqa: E402
import maintenance  # noqa: E402
import signals  # noqa: E402
import store  # noqa: E402
import transcript as T  # noqa: E402


def _write(path, rows):
    with open(path, "w") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")


def _asst(uuid, mid, out, ts, **usage):
    u = {"input_tokens": 10, "output_tokens": out, "cache_read_input_tokens": 0,
         "cache_creation_input_tokens": 0}
    u.update(usage)
    return {"type": "assistant", "uuid": uuid, "timestamp": ts, "effort": "high",
            "message": {"role": "assistant", "id": mid, "model": "claude-opus-5",
                        "content": [{"type": "text", "text": "ok"}], "usage": u}}


class TestSyntheticPrompts(unittest.TestCase):
    """Claude Code injects `type=user` records no human typed. They inflated
    num_prompts by ~19% and were scored as prompt quality."""

    def test_injected_records_are_not_prompts(self):
        for prefix in ("<task-notification>x</task-notification>",
                       "<local-command-caveat>x</local-command-caveat>",
                       "<local-command-stdout>x</local-command-stdout>",
                       "<system-reminder>x</system-reminder>",
                       "[Request interrupted by user for tool use]"):
            self.assertTrue(T.is_synthetic_prompt(prefix), prefix)

    def test_real_prompts_are_not_synthetic(self):
        for text in ("fix the bug", "<command-name>/clear</command-name>",
                     "  do the thing"):
            self.assertFalse(T.is_synthetic_prompt(text), text)

    def test_injected_record_folds_into_the_turn_in_progress(self):
        tmp = tempfile.mkdtemp()
        p = Path(tmp) / "t.jsonl"
        _write(p, [
            {"type": "user", "uuid": "u1", "timestamp": "2026-06-28T10:00:00Z",
             "message": {"role": "user", "content": "do it"}},
            _asst("a1x", "a1", 100, "2026-06-28T10:00:02Z"),
            {"type": "user", "uuid": "n1", "timestamp": "2026-06-28T10:00:10Z",
             "message": {"role": "user", "content":
                         "<task-notification><task-id>z</task-id>"
                         "</task-notification>"}},
            _asst("a2x", "a2", 400, "2026-06-28T10:00:20Z"),
        ])
        turns = T.parse_turns(str(p))
        self.assertEqual(len(turns), 1)
        # The work the notification triggered belongs to the prompt that
        # started it, so no tokens are lost and no prompt is invented.
        self.assertEqual(turns[0].output_tokens, 500)


class TestEnvelopeRefresh(unittest.TestCase):
    """A turn captured mid-flight used to be frozen forever."""

    def test_capture_refreshes_a_truncated_envelope(self):
        tmp = tempfile.mkdtemp()
        p = Path(tmp) / "t.jsonl"
        rows = [{"type": "user", "uuid": "u1",
                 "timestamp": "2026-06-28T10:00:00Z",
                 "message": {"role": "user", "content": "go"}},
                _asst("a1x", "a1", 100, "2026-06-28T10:00:02Z")]
        _write(p, rows)
        pl = {"session_id": "S", "transcript_path": str(p), "cwd": "/x/proj"}
        ingest.on_session_start(pl, tmp)
        ingest.on_stop(pl, tmp)

        # The turn continues after that first capture.
        rows.append(_asst("a2x", "a2", 900, "2026-06-28T10:01:00Z"))
        _write(p, rows)
        ingest.on_stop(pl, tmp)

        conn = db.connect(tmp)
        got = conn.execute(
            "SELECT output_tokens FROM turns WHERE turn_id='u1'").fetchone()[0]
        self.assertEqual(got, 1000)

    def test_envelope_never_shrinks(self):
        tmp = tempfile.mkdtemp()
        p = Path(tmp) / "t.jsonl"
        full = [{"type": "user", "uuid": "u1",
                 "timestamp": "2026-06-28T10:00:00Z",
                 "message": {"role": "user", "content": "go"}},
                _asst("a1x", "a1", 1000, "2026-06-28T10:00:02Z")]
        _write(p, full)
        pl = {"session_id": "S", "transcript_path": str(p), "cwd": "/x/proj"}
        ingest.on_session_start(pl, tmp)
        ingest.on_stop(pl, tmp)
        # A compacted / truncated transcript must not erase what we know.
        _write(p, full[:1] + [_asst("a1x", "a1", 5, "2026-06-28T10:00:02Z")])
        ingest.on_stop(pl, tmp)
        conn = db.connect(tmp)
        self.assertEqual(conn.execute(
            "SELECT output_tokens FROM turns WHERE turn_id='u1'").fetchone()[0],
            1000)


class TestActiveTime(unittest.TestCase):
    """Elapsed span produced 250-hour "runs"; active time caps idle gaps."""

    def test_idle_gap_is_capped(self):
        stamps = ["2026-06-28T10:00:00Z", "2026-06-28T10:00:30Z",
                  "2026-06-29T10:00:00Z"]          # a ~24h gap
        active = T._active_ms(stamps)
        self.assertEqual(active, 30_000 + T.IDLE_GAP_MS)

    def test_single_stamp_is_zero_not_none(self):
        self.assertEqual(T._active_ms(["2026-06-28T10:00:00Z"]), 0)


class TestCostWeighting(unittest.TestCase):
    """Raw summed tokens are dominated by cache reads, which bill at 0.1x."""

    def test_weights_match_published_multipliers(self):
        self.assertEqual(cost.weighted(input_tokens=1000), 1000)
        self.assertEqual(cost.weighted(output_tokens=1000), 5000)
        self.assertEqual(cost.weighted(cache_read_tokens=1000), 100)
        self.assertEqual(
            cost.weighted(cache_creation_tokens=1000,
                          cache_creation_1h_tokens=1000), 2000)
        self.assertEqual(cost.weighted(cache_creation_tokens=1000), 1250)

    def test_sql_and_python_agree(self):
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE t (input_tokens INT, output_tokens INT,"
                     " cache_read_tokens INT, cache_creation_tokens INT,"
                     " cache_creation_1h_tokens INT)")
        conn.execute("INSERT INTO t VALUES (70, 19144, 2420336, 106960, 40000)")
        got = conn.execute(f"SELECT {cost.WEIGHTED_SQL} FROM t").fetchone()[0]
        self.assertAlmostEqual(
            got, cost.weighted(70, 19144, 2420336, 106960, 40000), places=3)

    def test_cache_reuse_is_not_punished(self):
        """The ranking inversion this fixes: a cache-heavy run really is cheap."""
        cache_heavy = cost.weighted(input_tokens=100, output_tokens=1000,
                                    cache_read_tokens=500_000)
        cache_cold = cost.weighted(input_tokens=200_000, output_tokens=1000)
        self.assertLess(cache_heavy, cache_cold)

    def test_usd_is_none_for_unknown_models(self):
        self.assertIsNone(cost.usd(1_000_000, "some-future-model"))
        self.assertAlmostEqual(cost.usd(1_000_000, "claude-opus-5"), 5.0)
        # The Claude Code context-window suffix must not defeat the lookup.
        self.assertAlmostEqual(cost.usd(1_000_000, "claude-opus-5[1m]"), 5.0)


class TestMigration(unittest.TestCase):
    """`CREATE TABLE IF NOT EXISTS` never adds a column to an existing table."""

    def test_missing_columns_are_added_to_an_old_database(self):
        tmp = tempfile.mkdtemp()
        db.init_db(tmp)
        conn = db.connect(tmp)
        conn.execute("ALTER TABLE turns DROP COLUMN active_ms")
        conn.commit()
        conn.close()
        applied = db.init_db(tmp)
        self.assertIn("+turns.active_ms", applied)
        conn = db.connect(tmp)
        self.assertIn("active_ms", db._columns(conn, "turns"))

    def test_superseded_table_is_dropped(self):
        tmp = tempfile.mkdtemp()
        db.init_db(tmp)
        conn = db.connect(tmp)
        conn.execute("CREATE TABLE open_run (run_id TEXT)")
        conn.commit()
        conn.close()
        applied = db.init_db(tmp)
        self.assertIn("-open_run", applied)

    def test_version_is_recorded(self):
        tmp = tempfile.mkdtemp()
        db.init_db(tmp)
        conn = db.connect(tmp)
        v = conn.execute(
            "SELECT value FROM schema_meta WHERE key='version'").fetchone()[0]
        self.assertEqual(v, db.SCHEMA_VERSION)


class TestStaleSweep(unittest.TestCase):
    """30% of runs sat open forever because SessionEnd never fired."""

    def _open_run(self, tmp, last_ts):
        p = Path(tmp) / "t.jsonl"
        _write(p, [{"type": "user", "uuid": "u1", "timestamp": last_ts,
                    "message": {"role": "user", "content": "go"}},
                   _asst("a1x", "a1", 100, last_ts)])
        pl = {"session_id": "S", "transcript_path": str(p), "cwd": "/x/proj"}
        ingest.on_session_start(pl, tmp)
        ingest.on_stop(pl, tmp)
        return db.connect(tmp)

    def test_abandoned_run_is_finalized(self):
        tmp = tempfile.mkdtemp()
        conn = self._open_run(tmp, "2020-01-01T00:00:00Z")
        self.assertEqual(len(store.sweep_stale_runs(conn)), 1)
        row = conn.execute(
            "SELECT ended_at, closed_by, output_tokens FROM runs").fetchone()
        self.assertIsNotNone(row["ended_at"])
        self.assertEqual(row["closed_by"], "stale-sweep")
        self.assertEqual(row["output_tokens"], 100)

    def test_a_live_run_is_left_alone(self):
        tmp = tempfile.mkdtemp()
        conn = self._open_run(tmp, "2026-06-28T10:00:00Z")
        self.assertEqual(
            store.sweep_stale_runs(conn, now="2026-06-28T10:05:00Z"), [])


class TestClearSignal(unittest.TestCase):
    """`/clear` gets no assistant reply, so it never became a turn — and the
    envelope then filtered its bundle out, pinning clear_count at zero."""

    def test_unanswered_prompt_signals_are_carried_forward(self):
        tmp = tempfile.mkdtemp()
        p = Path(tmp) / "t.jsonl"
        _write(p, [
            {"type": "user", "uuid": "u1", "timestamp": "2026-06-28T10:00:00Z",
             "message": {"role": "user", "content": "do it"}},
            _asst("a1x", "a1", 100, "2026-06-28T10:00:02Z"),
            {"type": "user", "uuid": "u2", "timestamp": "2026-06-28T10:01:00Z",
             "message": {"role": "user", "content":
                         "<command-name>/clear</command-name>"}},
        ])
        pl = {"session_id": "S", "transcript_path": str(p), "cwd": "/x/proj"}
        ingest.on_session_start(pl, tmp)
        ingest.on_stop(pl, tmp)
        ingest.on_session_end(pl, tmp)
        conn = db.connect(tmp)
        self.assertEqual(
            conn.execute("SELECT clear_count FROM runs").fetchone()[0], 1)


class TestInterruptSignal(unittest.TestCase):
    def test_real_marker_is_detected(self):
        tmp = tempfile.mkdtemp()
        p = Path(tmp) / "t.jsonl"
        _write(p, [
            {"type": "user", "uuid": "u1", "timestamp": "2026-06-28T10:00:00Z",
             "message": {"role": "user", "content": "go"}},
            _asst("a1x", "a1", 100, "2026-06-28T10:00:02Z"),
            {"type": "user", "uuid": "i1", "timestamp": "2026-06-28T10:00:05Z",
             "message": {"role": "user",
                         "content": "[Request interrupted by user for tool use]"}},
        ])
        env = signals.aggregate(list(signals.extract_bundles(str(p)).values()))
        self.assertEqual(env["interrupts"], 1)

    def test_the_old_nonexistent_shape_is_not_relied_on(self):
        """`toolUseResult.interrupted` appears in no real transcript."""
        tmp = tempfile.mkdtemp()
        p = Path(tmp) / "t.jsonl"
        _write(p, [
            {"type": "user", "uuid": "u1", "timestamp": "2026-06-28T10:00:00Z",
             "message": {"role": "user", "content": "go"}},
            _asst("a1x", "a1", 100, "2026-06-28T10:00:02Z"),
            {"type": "user", "uuid": "r1", "timestamp": "2026-06-28T10:00:05Z",
             "toolUseResult": {"interrupted": True},
             "message": {"role": "user", "content": [{"type": "tool_result",
                                                      "content": ""}]}},
        ])
        env = signals.aggregate(list(signals.extract_bundles(str(p)).values()))
        self.assertEqual(env["interrupts"], 0)


class TestContextWindow(unittest.TestCase):
    def test_window_is_configurable_and_never_exceeds_100pct(self):
        import os
        self.assertEqual(signals.context_window(150_000), 200_000)
        self.assertEqual(signals.context_window(400_000), 1_000_000)
        os.environ["CPT_CONTEXT_WINDOW"] = "1000000"
        try:
            self.assertEqual(signals.context_window(150_000), 1_000_000)
        finally:
            del os.environ["CPT_CONTEXT_WINDOW"]

    def test_raw_peak_is_recorded_alongside_the_percentage(self):
        tmp = tempfile.mkdtemp()
        p = Path(tmp) / "t.jsonl"
        _write(p, [
            {"type": "user", "uuid": "u1", "timestamp": "2026-06-28T10:00:00Z",
             "message": {"role": "user", "content": "go"}},
            _asst("a1x", "a1", 100, "2026-06-28T10:00:02Z",
                  input_tokens=1000, cache_read_input_tokens=99_000),
        ])
        pl = {"session_id": "S", "transcript_path": str(p), "cwd": "/x/proj"}
        ingest.on_session_start(pl, tmp)
        ingest.on_stop(pl, tmp)
        ingest.on_session_end(pl, tmp)
        conn = db.connect(tmp)
        row = conn.execute(
            "SELECT peak_context_tokens, peak_context_pct FROM runs").fetchone()
        self.assertEqual(row["peak_context_tokens"], 100_000)
        self.assertEqual(row["peak_context_pct"], 50.0)


class TestBackfill(unittest.TestCase):
    def test_backfill_repairs_a_corrupted_row(self):
        tmp = tempfile.mkdtemp()
        p = Path(tmp) / "t.jsonl"
        _write(p, [
            {"type": "user", "uuid": "u1", "timestamp": "2026-06-28T10:00:00Z",
             "message": {"role": "user", "content": "go"}},
            _asst("a1x", "a1", 1000, "2026-06-28T10:00:02Z"),
        ])
        pl = {"session_id": "S", "transcript_path": str(p), "cwd": "/x/proj"}
        ingest.on_session_start(pl, tmp)
        ingest.on_stop(pl, tmp)
        conn = db.connect(tmp)
        # Simulate exactly what the old SubagentStop hook left behind.
        conn.execute("UPDATE turns SET output_tokens = 90, "
                     "query_source = 'subagent' WHERE turn_id = 'u1'")
        conn.commit()

        maintenance.backfill(conn)
        row = conn.execute(
            "SELECT output_tokens, query_source, is_prompt FROM turns "
            "WHERE turn_id='u1'").fetchone()
        self.assertEqual(row["output_tokens"], 1000)
        self.assertEqual(row["query_source"], "main")
        self.assertEqual(row["is_prompt"], 1)

    def test_salvage_fixes_rows_whose_transcript_is_gone(self):
        tmp = tempfile.mkdtemp()
        db.init_db(tmp)
        conn = db.connect(tmp)
        conn.execute("INSERT INTO runs (run_id, capture_mode, started_at) "
                     "VALUES ('r1','passive','2026-06-28T10:00:00Z')")
        conn.execute(
            "INSERT INTO turns (turn_id, run_id, session_id, query_source, "
            "is_prompt, prompt_text) VALUES "
            "('u1','r1','S','subagent',1,'a real human prompt')")
        conn.execute(
            "INSERT INTO turns (turn_id, run_id, session_id, query_source, "
            "is_prompt, prompt_text) VALUES "
            "('u2','r1','S','main',1,'<task-notification>x</task-notification>')")
        conn.commit()
        res = maintenance.salvage_unrebuildable(conn)
        self.assertEqual(res["relabelled_main"], 1)
        self.assertEqual(res["marked_not_prompt"], 1)
        rows = dict(conn.execute(
            "SELECT turn_id, query_source FROM turns").fetchall())
        self.assertEqual(rows["u1"], "main")
        self.assertEqual(conn.execute(
            "SELECT is_prompt FROM turns WHERE turn_id='u2'").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()


class TestHooksAreSelfSufficient(unittest.TestCase):
    """A plugin installed mid-session sees its first event at Stop, not
    SessionStart. Without the schema in place that failed — silently, forever."""

    def test_stop_works_without_a_prior_session_start(self):
        tmp = tempfile.mkdtemp()
        p = Path(tmp) / "t.jsonl"
        _write(p, [
            {"type": "user", "uuid": "u1", "timestamp": "2026-06-28T10:00:00Z",
             "message": {"role": "user", "content": "go"}},
            _asst("a1x", "a1", 100, "2026-06-28T10:00:02Z"),
        ])
        pl = {"session_id": "S", "transcript_path": str(p), "cwd": "/x/proj"}
        ingest.on_stop(pl, tmp)          # no on_session_start first
        conn = db.connect(tmp)
        self.assertEqual(conn.execute(
            "SELECT output_tokens FROM turns WHERE turn_id='u1'").fetchone()[0],
            100)

    def test_session_end_works_without_a_prior_session_start(self):
        tmp = tempfile.mkdtemp()
        p = Path(tmp) / "t.jsonl"
        _write(p, [
            {"type": "user", "uuid": "u1", "timestamp": "2026-06-28T10:00:00Z",
             "message": {"role": "user", "content": "go"}},
            _asst("a1x", "a1", 100, "2026-06-28T10:00:02Z"),
        ])
        pl = {"session_id": "S", "transcript_path": str(p), "cwd": "/x/proj"}
        ingest.on_session_end(pl, tmp)
        conn = db.connect(tmp)
        self.assertEqual(conn.execute(
            "SELECT closed_by FROM runs").fetchone()[0], "SessionEnd")
