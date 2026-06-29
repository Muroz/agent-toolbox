"""Slice 7 — subagent token attribution.

    python3 -m unittest discover -s tests
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import db  # noqa: E402
import ingest  # noqa: E402
import report  # noqa: E402
import transcript as T  # noqa: E402


def _write(path, rows):
    with open(path, "w") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")


def _parent(path):
    _write(path, [
        {"type": "user", "uuid": "pu1", "timestamp": "2026-06-28T10:00:00Z",
         "message": {"role": "user", "content": "main task"}},
        {"type": "assistant", "uuid": "pa1x", "timestamp": "2026-06-28T10:00:02Z",
         "message": {"role": "assistant", "id": "pa1", "model": "claude-opus-4-8",
                     "content": [{"type": "text", "text": "ok"}],
                     "usage": {"input_tokens": 10, "output_tokens": 100,
                               "cache_read_input_tokens": 0,
                               "cache_creation_input_tokens": 0}}},
    ])


def _subagent(path):
    # A subagent's own transcript: every line is sidechain.
    _write(path, [
        {"type": "user", "uuid": "su1", "isSidechain": True,
         "timestamp": "2026-06-28T10:00:01Z",
         "message": {"role": "user", "content": "explore the codebase"}},
        {"type": "assistant", "uuid": "sa1x", "isSidechain": True,
         "timestamp": "2026-06-28T10:00:01.5Z",
         "message": {"role": "assistant", "id": "sa1", "model": "claude-haiku-4-5",
                     "content": [{"type": "text", "text": "found it"}],
                     "usage": {"input_tokens": 5, "output_tokens": 50,
                               "cache_read_input_tokens": 0,
                               "cache_creation_input_tokens": 0}}},
    ])


class TestSubagentAttribution(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.parent = Path(self.tmp) / "parent.jsonl"
        self.sub = Path(self.tmp) / "subagent.jsonl"
        _parent(self.parent)
        _subagent(self.sub)
        self.pl = {"session_id": "S", "transcript_path": str(self.parent),
                   "cwd": "/x/proj"}

    def _cycle(self):
        ingest.on_session_start(self.pl, self.tmp)
        ingest.on_stop(self.pl, self.tmp)                       # main turn
        ingest.on_subagent_stop(
            {"session_id": "S", "transcript_path": str(self.sub),
             "agent_type": "Explore"}, self.tmp)                # subagent turns
        ingest.on_session_end(self.pl, self.tmp)               # finalize

    def test_sidechain_flag_matters(self):
        self.assertEqual(len(T.parse_turns(str(self.sub))), 0)          # skipped
        self.assertEqual(len(T.parse_turns(str(self.sub), True)), 1)    # included

    def test_subagent_turns_attributed_to_parent_run(self):
        self._cycle()
        c = db.connect(self.tmp)
        run_id = c.execute(
            "SELECT run_id FROM turns WHERE query_source='main'").fetchone()[0]
        sub = c.execute(
            "SELECT run_id, output_tokens FROM turns WHERE query_source='subagent'"
        ).fetchone()
        self.assertEqual(sub[0], run_id)   # same parent run
        self.assertEqual(sub[1], 50)

    def test_run_total_includes_subagent(self):
        self._cycle()
        c = db.connect(self.tmp)
        run_out = c.execute("SELECT output_tokens FROM runs").fetchone()[0]
        main_out = c.execute(
            "SELECT SUM(output_tokens) FROM turns WHERE query_source='main'"
        ).fetchone()[0]
        self.assertEqual(main_out, 100)
        self.assertEqual(run_out, 150)     # 100 main + 50 subagent
        self.assertGreater(run_out, main_out)

    def test_overview_breaks_out_query_source(self):
        self._cycle()
        out = report.render_overview_for(self.tmp)
        self.assertIn("## By query source", out)
        self.assertIn("subagent", out)


if __name__ == "__main__":
    unittest.main()
