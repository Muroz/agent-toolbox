"""Slice 2 — passive turn capture (SessionStart -> Stop -> SessionEnd).

    python3 -m unittest discover -s tests
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import db  # noqa: E402
import ingest  # noqa: E402


def _asst(mid, model, inp, out, cr, cc, ts, tool=False):
    content = [{"type": "text", "text": "..."}]
    if tool:
        content.append({"type": "tool_use", "name": "Bash", "input": {}})
    return {
        "type": "assistant", "uuid": f"{mid}-{ts}", "timestamp": ts,
        "message": {
            "role": "assistant", "id": mid, "model": model, "content": content,
            "usage": {
                "input_tokens": inp, "output_tokens": out,
                "cache_read_input_tokens": cr, "cache_creation_input_tokens": cc,
            },
        },
    }


def _transcript(path: Path) -> None:
    rows = [
        # meta line — NOT a prompt
        {"type": "user", "isMeta": True, "uuid": "m0", "timestamp": "2026-06-28T10:00:00Z",
         "message": {"role": "user", "content": "<command-name>x</command-name>"}},
        # real prompt 1
        {"type": "user", "uuid": "u1", "timestamp": "2026-06-28T10:00:01Z",
         "message": {"role": "user", "content": "first task"}},
        _asst("a1", "claude-opus-4-7", 10, 20, 100, 5, "2026-06-28T10:00:02Z", tool=True),
        # duplicate of a1 (same message.id) — must be counted once
        _asst("a1", "claude-opus-4-7", 10, 20, 100, 5, "2026-06-28T10:00:03Z", tool=True),
        # tool_result — NOT a prompt
        {"type": "user", "uuid": "tr1", "timestamp": "2026-06-28T10:00:04Z",
         "toolUseResult": {"ok": True},
         "message": {"role": "user", "content": [{"type": "tool_result", "content": "done"}]}},
        _asst("a2", "claude-opus-4-7", 2, 30, 200, 0, "2026-06-28T10:00:05Z"),
        # real prompt 2
        {"type": "user", "uuid": "u2", "timestamp": "2026-06-28T10:00:10Z",
         "message": {"role": "user", "content": "second task"}},
        _asst("a3", "claude-opus-4-8", 5, 40, 50, 1, "2026-06-28T10:00:12Z", tool=True),
    ]
    with open(path, "w") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")


class TestPassiveCapture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.session = "sess-XYZ"
        self.tpath = Path(self.tmp) / "transcript.jsonl"
        _transcript(self.tpath)
        self.payload = {
            "session_id": self.session,
            "transcript_path": str(self.tpath),
            "cwd": "/Users/x/Coding/myproject",
        }

    def _run_full_cycle(self):
        ingest.on_session_start(self.payload, self.tmp)
        ingest.on_stop(self.payload, self.tmp)
        ingest.on_session_end(self.payload, self.tmp)

    def _conn(self):
        return db.connect(self.tmp)

    def test_one_run_two_turns(self):
        self._run_full_cycle()
        c = self._conn()
        self.assertEqual(c.execute("SELECT COUNT(*) FROM runs").fetchone()[0], 1)
        self.assertEqual(c.execute("SELECT COUNT(*) FROM turns").fetchone()[0], 2)

    def test_token_sums_dedup_and_exclusions(self):
        self._run_full_cycle()
        c = self._conn()
        # Turn 1 = a1 (counted once despite duplicate) + a2
        t1 = c.execute(
            "SELECT input_tokens, output_tokens, cache_read_tokens, "
            "cache_creation_tokens, num_tool_calls FROM turns WHERE turn_id='u1'"
        ).fetchone()
        self.assertEqual(tuple(t1), (12, 50, 300, 5, 1))
        # Turn 2 = a3
        t2 = c.execute(
            "SELECT input_tokens, output_tokens, cache_read_tokens, "
            "cache_creation_tokens, num_tool_calls FROM turns WHERE turn_id='u2'"
        ).fetchone()
        self.assertEqual(tuple(t2), (5, 40, 50, 1, 1))

    def test_run_totals_equal_sum_of_turns(self):
        self._run_full_cycle()
        c = self._conn()
        run = c.execute(
            "SELECT input_tokens, output_tokens, cache_read_tokens, "
            "cache_creation_tokens, num_tool_calls, num_prompts FROM runs"
        ).fetchone()
        self.assertEqual(tuple(run), (17, 90, 350, 6, 2, 2))

    def test_run_id_is_session_independent(self):
        self._run_full_cycle()
        c = self._conn()
        run_id = c.execute("SELECT run_id FROM runs").fetchone()[0]
        self.assertTrue(run_id.startswith("run-"))
        self.assertNotEqual(run_id, self.session)

    def test_source_is_transcript_everywhere(self):
        self._run_full_cycle()
        c = self._conn()
        self.assertEqual(
            [r[0] for r in c.execute("SELECT DISTINCT source FROM runs")], ["transcript"])
        self.assertEqual(
            [r[0] for r in c.execute("SELECT DISTINCT source FROM turns")], ["transcript"])

    def test_models_and_close_recorded(self):
        self._run_full_cycle()
        c = self._conn()
        models, closed_by, ended = c.execute(
            "SELECT models, closed_by, ended_at FROM runs").fetchone()
        self.assertIn("claude-opus-4-7", models)
        self.assertIn("claude-opus-4-8", models)
        self.assertEqual(closed_by, "SessionEnd")
        self.assertIsNotNone(ended)

    def test_idempotent_replay(self):
        self._run_full_cycle()
        # Replay every hook again — must not double-count.
        self._run_full_cycle()
        c = self._conn()
        self.assertEqual(c.execute("SELECT COUNT(*) FROM runs").fetchone()[0], 1)
        self.assertEqual(c.execute("SELECT COUNT(*) FROM turns").fetchone()[0], 2)
        total = c.execute("SELECT output_tokens FROM runs").fetchone()[0]
        self.assertEqual(total, 90)


if __name__ == "__main__":
    unittest.main()
