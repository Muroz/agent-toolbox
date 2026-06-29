"""Slice 9 — qualitative scoring backbone (evaluate.py + rubric.py).

The usage-evaluator subagent itself is model-driven; here we test the
deterministic pieces it sits between: list-unjudged, context, and persist.

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
import evaluate  # noqa: E402
import ingest  # noqa: E402
import rubric  # noqa: E402


def _transcript(path):
    rows = [
        {"type": "user", "uuid": "t1", "timestamp": "2026-06-28T10:01:00Z",
         "message": {"role": "user", "content": "build the feature"}},
        {"type": "assistant", "uuid": "a1x", "timestamp": "2026-06-28T10:01:02Z",
         "message": {"role": "assistant", "id": "a1", "model": "claude-opus-4-8",
                     "content": [{"type": "text", "text": "ok"}],
                     "usage": {"input_tokens": 5, "output_tokens": 50,
                               "cache_read_input_tokens": 0,
                               "cache_creation_input_tokens": 0}}},
    ]
    with open(path, "w") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")


class TestEvaluate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        tp = Path(self.tmp) / "t.jsonl"
        _transcript(tp)
        pl = {"session_id": "s1", "transcript_path": str(tp), "cwd": "/x/proj"}
        ingest.on_session_start(pl, self.tmp)
        ingest.on_stop(pl, self.tmp)
        ingest.on_session_end(pl, self.tmp)
        self.conn = db.connect(self.tmp)
        self.run_id = self.conn.execute("SELECT run_id FROM runs").fetchone()[0]
        self.turn_id = self.conn.execute(
            "SELECT turn_id FROM turns").fetchone()[0]

    def test_rubric_version_parses_without_yaml(self):
        self.assertEqual(rubric.version(), "1")
        self.assertIn("ownership_dodging", rubric.dimension_keys())

    def test_list_unjudged_then_excludes_after_persist(self):
        before = evaluate.list_unjudged(self.conn, 10)
        self.assertEqual([r["run_id"] for r in before], [self.run_id])
        evaluate.persist(self.conn, self.run_id, {
            "overall_grade": "B",
            "run_scores": [{"dimension": "ownership_dodging", "score": 2}],
            "prompt_scores": []})
        after = evaluate.list_unjudged(self.conn, 10)
        self.assertEqual(after, [])

    def test_context_has_transcripts_and_turns(self):
        ctx = evaluate.context(self.conn, self.run_id)
        self.assertTrue(ctx["transcripts"])
        self.assertEqual(ctx["rubric_version"], "1")
        self.assertEqual([t["turn_id"] for t in ctx["turns"]], [self.turn_id])
        self.assertEqual(ctx["turns"][0]["prompt"], "build the feature")

    def test_persist_writes_verdict_and_eav_scores(self):
        verdict = {
            "model": "claude-haiku-4-5", "rubric_version": "1",
            "overall_grade": "B", "notes": "fine",
            "run_scores": [
                {"dimension": "ownership_dodging", "score": 2, "rationale": "clean"},
                {"dimension": "brand_new_dim", "score": 1, "rationale": "extensible"},
            ],
            "prompt_scores": [
                {"turn_id": self.turn_id, "dimension": "clarity", "score": 2,
                 "rationale": "clear"}],
        }
        result = evaluate.persist(self.conn, self.run_id, verdict)
        self.assertEqual(result["scores_written"], 3)

        v = self.conn.execute(
            "SELECT model, rubric_version, overall_grade FROM judge_verdicts "
            "WHERE run_id=?", (self.run_id,)).fetchone()
        self.assertEqual(tuple(v), ("claude-haiku-4-5", "1", "B"))

        # run-level EAV scores, including a dimension NOT in rubric.yaml
        run_scores = dict(self.conn.execute(
            "SELECT dimension, score FROM scores "
            "WHERE subject_type='run' AND subject_id=?", (self.run_id,)).fetchall())
        self.assertEqual(run_scores, {"ownership_dodging": 2, "brand_new_dim": 1})

        # prompt-level score keyed by turn_id
        ps = self.conn.execute(
            "SELECT dimension, score, rubric_version FROM scores "
            "WHERE subject_type='prompt' AND subject_id=?", (self.turn_id,)).fetchone()
        self.assertEqual(tuple(ps), ("clarity", 2, "1"))

    def test_persist_defaults_rubric_version(self):
        # no rubric_version in verdict -> stamped from rubric.yaml
        evaluate.persist(self.conn, self.run_id, {
            "run_scores": [{"dimension": "reasoning_loops", "score": 0}],
            "prompt_scores": []})
        rv = self.conn.execute(
            "SELECT rubric_version FROM scores WHERE dimension='reasoning_loops'"
        ).fetchone()[0]
        self.assertEqual(rv, "1")


if __name__ == "__main__":
    unittest.main()
