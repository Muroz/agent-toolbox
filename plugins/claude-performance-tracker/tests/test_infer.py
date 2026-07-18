"""Slice 8 — inferred outcome for passive runs.

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
import infer_outcome  # noqa: E402
import ingest  # noqa: E402
import store  # noqa: E402

_C = [0]


def _transcript(path, prompts, produced=True, interrupts=0):
    rows = []
    for i, p in enumerate(prompts):
        rows.append({"type": "user", "uuid": f"u{i}",
                     "timestamp": f"2026-06-28T10:{i*2+1:02d}:00Z",
                     "message": {"role": "user", "content": p}})
        rows.append({"type": "assistant", "uuid": f"a{i}x",
                     "timestamp": f"2026-06-28T10:{i*2+2:02d}:00Z",
                     "message": {"role": "assistant", "id": f"a{i}",
                                 "model": "claude-opus-4-8",
                                 "content": [{"type": "text", "text": "ok"}],
                                 "usage": {"input_tokens": 5,
                                           "output_tokens": 50 if produced else 0,
                                           "cache_read_input_tokens": 0,
                                           "cache_creation_input_tokens": 0}}})
    for j in range(interrupts):
        rows.append({"type": "user", "uuid": f"int{j}",
                     "timestamp": f"2026-06-28T10:59:{j:02d}Z",
                     "toolUseResult": {"interrupted": True, "stdout": "", "stderr": ""},
                     "message": {"role": "user",
                                 "content": [{"type": "tool_result", "content": ""}]}})
    with open(path, "w") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")


class TestInferOutcome(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def _infer(self, prompts, **kw):
        _C[0] += 1
        sid = f"s{_C[0]}"
        tp = Path(self.tmp) / f"{sid}.jsonl"
        _transcript(tp, prompts, **kw)
        pl = {"session_id": sid, "transcript_path": str(tp), "cwd": "/x/proj"}
        ingest.on_session_start(pl, self.tmp)
        ingest.on_stop(pl, self.tmp)
        ingest.on_session_end(pl, self.tmp)
        c = db.connect(self.tmp)
        return c.execute(
            "SELECT outcome, outcome_source, inferred_signals FROM runs "
            "WHERE run_id = (SELECT run_id FROM sessions WHERE session_id=?)",
            (sid,)).fetchone()

    def test_success_on_positive_cue(self):
        row = self._infer(["build the feature", "thanks, that works"])
        self.assertEqual(row[0], "success")
        self.assertEqual(row[1], "inferred")

    def test_failed_on_negative_cue(self):
        row = self._infer(["fix the bug", "no, that doesn't work"])
        self.assertEqual(row[0], "failed")

    def test_partial_on_reprompt(self):
        row = self._infer(["do the thing", "no, try a different approach"])
        self.assertEqual(row[0], "partial")

    def test_failed_on_heavy_interrupts(self):
        row = self._infer(["do X", "continue"], interrupts=2)
        self.assertEqual(row[0], "failed")

    def test_unknown_when_no_output_no_cues(self):
        row = self._infer(["just checking"], produced=False)
        self.assertEqual(row[0], "unknown")

    def test_outcome_source_and_signals_audited(self):
        row = self._infer(["build it", "thanks, that works"])
        self.assertEqual(row[1], "inferred")
        sig = json.loads(row[2])
        self.assertEqual(sig["positive_cues"], 1)
        self.assertIn("n_prompts", sig)
        self.assertIn("produced", sig)

    def test_self_report_never_overwritten(self):
        db.init_db(self.tmp)
        conn = db.connect(self.tmp)
        rid, _ = store.start_tracked_run(conn, "sess", "exp", "feature", "M", None, None)
        store.finish_tracked_run(conn, "sess", "success", 5, None)
        # inference must refuse to touch a tracked / self-reported run
        self.assertIsNone(infer_outcome.infer_and_store(conn, rid))
        row = conn.execute(
            "SELECT outcome, outcome_source FROM runs WHERE run_id=?", (rid,)).fetchone()
        self.assertEqual(tuple(row), ("success", "self_report"))


if __name__ == "__main__":
    unittest.main()
