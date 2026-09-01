"""Regression cover for the audit fixes in branch-token-tracker.

    python3 -m unittest discover -s tests
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import cost  # noqa: E402
import db  # noqa: E402
import ingest  # noqa: E402
import transcript as T  # noqa: E402


def _write(path, rows):
    with open(path, "w") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")


def _asst(uuid, mid, out, ts):
    return {"type": "assistant", "uuid": uuid, "timestamp": ts,
            "message": {"role": "assistant", "id": mid, "model": "claude-opus-5",
                        "content": [{"type": "text", "text": "ok"}],
                        "usage": {"input_tokens": 10, "output_tokens": out,
                                  "cache_read_input_tokens": 0,
                                  "cache_creation_input_tokens": 0}}}


def _agent_result(uuid, agent_id, out, ts):
    return {"type": "user", "uuid": uuid, "timestamp": ts,
            "message": {"role": "user", "content": [{"type": "tool_result",
                                                     "content": "done"}]},
            "toolUseResult": {
                "status": "completed", "agentId": agent_id,
                "agentType": "Explore", "resolvedModel": "claude-opus-5[1m]",
                "totalDurationMs": 1000, "totalToolUseCount": 3,
                "usage": {"input_tokens": 5, "output_tokens": out,
                          "cache_read_input_tokens": 100,
                          "cache_creation_input_tokens": 0}}}


class TestSubagentSpendIsBilled(unittest.TestCase):
    """The parser used to include sidechain records so subagent spend would
    count against the ticket. `isSidechain` is false on every real record, so it
    counted nothing and subagent tokens went unbilled entirely."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.tp = Path(self.tmp) / "t.jsonl"
        _write(self.tp, [
            {"type": "user", "uuid": "u1", "timestamp": "2026-06-28T10:00:00Z",
             "message": {"role": "user", "content": "do it"}},
            _asst("a1x", "a1", 100, "2026-06-28T10:00:02Z"),
            _agent_result("r1", "sub1", 700, "2026-06-28T10:00:30Z"),
            _asst("a2x", "a2", 200, "2026-06-28T10:01:00Z"),
        ])
        self.pl = {"session_id": "S", "cwd": self.tmp,
                   "transcript_path": str(self.tp)}

    def test_parser_yields_a_subagent_row(self):
        turns = {t.turn_id: t for t in T.parse_turns(str(self.tp))}
        self.assertIn("agent:sub1", turns)
        self.assertEqual(turns["agent:sub1"].output_tokens, 700)
        self.assertEqual(turns["agent:sub1"].query_source, "subagent")
        # And the main turn keeps everything it produced, before and after.
        self.assertEqual(turns["u1"].output_tokens, 300)


class TestEnvelopeRefresh(unittest.TestCase):
    def test_a_turn_caught_mid_flight_is_topped_up(self):
        tmp = tempfile.mkdtemp()
        p = Path(tmp) / "t.jsonl"
        rows = [{"type": "user", "uuid": "u1",
                 "timestamp": "2026-06-28T10:00:00Z",
                 "message": {"role": "user", "content": "go"}},
                _asst("a1x", "a1", 100, "2026-06-28T10:00:02Z")]
        _write(p, rows)
        db.init_db(tmp)
        conn = db.connect(tmp)
        ingest.capture(conn, "S", str(p), project="proj", branch="main",
                       ticket="ABC-1")
        rows.append(_asst("a2x", "a2", 900, "2026-06-28T10:01:00Z"))
        _write(p, rows)
        ingest.capture(conn, "S", str(p), project="proj", branch="main",
                       ticket="ABC-1")
        self.assertEqual(conn.execute(
            "SELECT output_tokens FROM turns WHERE turn_id='u1'").fetchone()[0],
            1000)

    def test_ticket_attribution_stays_pinned_across_a_branch_switch(self):
        """The whole point of capturing per Stop: work done before the switch
        must stay on the old ticket."""
        tmp = tempfile.mkdtemp()
        p = Path(tmp) / "t.jsonl"
        rows = [{"type": "user", "uuid": "u1",
                 "timestamp": "2026-06-28T10:00:00Z",
                 "message": {"role": "user", "content": "go"}},
                _asst("a1x", "a1", 100, "2026-06-28T10:00:02Z")]
        _write(p, rows)
        db.init_db(tmp)
        conn = db.connect(tmp)
        ingest.capture(conn, "S", str(p), project="p", branch="feat/ABC-1",
                       ticket="ABC-1")
        rows += [{"type": "user", "uuid": "u2",
                  "timestamp": "2026-06-28T10:02:00Z",
                  "message": {"role": "user", "content": "more"}},
                 _asst("a3x", "a3", 50, "2026-06-28T10:02:02Z")]
        _write(p, rows)
        ingest.capture(conn, "S", str(p), project="p", branch="feat/XYZ-9",
                       ticket="XYZ-9")
        got = dict(conn.execute("SELECT turn_id, ticket FROM turns").fetchall())
        self.assertEqual(got["u1"], "ABC-1")
        self.assertEqual(got["u2"], "XYZ-9")


class TestSyntheticPrompts(unittest.TestCase):
    def test_injected_records_do_not_become_turns(self):
        tmp = tempfile.mkdtemp()
        p = Path(tmp) / "t.jsonl"
        _write(p, [
            {"type": "user", "uuid": "u1", "timestamp": "2026-06-28T10:00:00Z",
             "message": {"role": "user", "content": "go"}},
            _asst("a1x", "a1", 100, "2026-06-28T10:00:02Z"),
            {"type": "user", "uuid": "n1", "timestamp": "2026-06-28T10:00:10Z",
             "message": {"role": "user", "content":
                         "<task-notification><task-id>z</task-id>"
                         "</task-notification>"}},
            _asst("a2x", "a2", 400, "2026-06-28T10:00:20Z"),
        ])
        turns = T.parse_turns(str(p))
        self.assertEqual(len(turns), 1)
        self.assertEqual(turns[0].output_tokens, 500)


class TestWeightedCost(unittest.TestCase):
    def test_raw_total_is_not_a_cost(self):
        """A cache-heavy ticket bills far less than its raw token sum implies."""
        raw = 100 + 1000 + 500_000
        weighted = cost.weighted(input_tokens=100, output_tokens=1000,
                                 cache_read_tokens=500_000)
        self.assertLess(weighted, raw / 5)

    def test_sql_matches_python(self):
        db_dir = tempfile.mkdtemp()
        db.init_db(db_dir)
        conn = db.connect(db_dir)
        conn.execute(
            "INSERT INTO turns (turn_id, session_id, ticket, input_tokens,"
            " output_tokens, cache_read_tokens, cache_creation_tokens,"
            " cache_creation_1h_tokens) VALUES ('t','S','ABC-1',10,20,30,40,10)")
        conn.commit()
        got = conn.execute(f"SELECT {cost.WEIGHTED_SQL} FROM turns").fetchone()[0]
        self.assertAlmostEqual(got, cost.weighted(10, 20, 30, 40, 10), places=3)


class TestMigration(unittest.TestCase):
    def test_missing_column_is_added(self):
        tmp = tempfile.mkdtemp()
        db.init_db(tmp)
        conn = db.connect(tmp)
        conn.execute("ALTER TABLE turns DROP COLUMN query_source")
        conn.commit()
        conn.close()
        applied = db.init_db(tmp)
        self.assertIn("+turns.query_source", applied)


class TestCurrentJson(unittest.TestCase):
    def test_written_on_stop_not_only_at_session_end(self):
        """A statusline reading this file used to show the previous session."""
        tmp = tempfile.mkdtemp()
        p = Path(tmp) / "t.jsonl"
        _write(p, [
            {"type": "user", "uuid": "u1", "timestamp": "2026-06-28T10:00:00Z",
             "message": {"role": "user", "content": "go"}},
            _asst("a1x", "a1", 100, "2026-06-28T10:00:02Z"),
        ])
        import io
        import contextlib
        payload = json.dumps({"session_id": "S", "cwd": tmp,
                              "transcript_path": str(p)})
        old = sys.stdin
        sys.stdin = io.StringIO(payload)
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                ingest.run("Stop", tmp)
        finally:
            sys.stdin = old
        cur = json.loads((Path(tmp) / "current.json").read_text())
        self.assertEqual(cur["session_turns"], 1)
        self.assertTrue(cur["updated_at"])       # never null any more
        self.assertIn("session_weighted", cur)


if __name__ == "__main__":
    unittest.main()
