"""Slice 10 — /usage-report degradation + run <id>.

    python3 -m unittest discover -s tests
"""

import json
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import db  # noqa: E402
import evaluate  # noqa: E402
import ingest  # noqa: E402
import report  # noqa: E402


def _frun(conn, model, started, **f):
    cols = dict(capture_mode="passive", ended_at=started, models=model,
                num_prompts=2, output_tokens=1000, input_tokens=10,
                cache_read_tokens=0, cache_creation_tokens=0, interrupts=0,
                edits_without_read=0, reasoning_loops=0, peak_context_pct=40.0)
    cols.update(f)
    rid = f"run-{uuid.uuid4().hex[:8]}"
    keys = ["run_id", "started_at"] + list(cols)
    vals = [rid, started] + list(cols.values())
    conn.execute(
        f"INSERT INTO runs ({','.join(keys)}, source) "
        f"VALUES ({','.join('?' for _ in keys)}, 'transcript')", vals)
    conn.commit()
    return rid


class TestDegradation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        db.init_db(self.tmp)
        self.conn = db.connect(self.tmp)

    def test_empty(self):
        self.assertIn("No finalized runs", report.render_degradation(self.conn))

    def test_per_model_periods_and_trend(self):
        _frun(self.conn, "claude-opus-4-8", "2026-05-10T10:00:00Z", interrupts=0)
        _frun(self.conn, "claude-opus-4-8", "2026-05-20T10:00:00Z", interrupts=0)
        _frun(self.conn, "claude-opus-4-8", "2026-06-05T10:00:00Z", interrupts=3)
        _frun(self.conn, "claude-opus-4-7", "2026-06-06T10:00:00Z", interrupts=1)
        out = report.render_degradation(self.conn, "month")
        self.assertIn("## claude-opus-4-8", out)
        self.assertIn("## claude-opus-4-7", out)
        self.assertIn("2026-05", out)
        self.assertIn("2026-06", out)

    def test_judge_column_uses_scores(self):
        rid = _frun(self.conn, "claude-opus-4-8", "2026-06-10T10:00:00Z")
        evaluate.persist(self.conn, rid, {
            "run_scores": [{"dimension": "ownership_dodging", "score": 2}],
            "prompt_scores": []})
        out = report.render_degradation(self.conn, "month")
        self.assertIn("judge", out)
        self.assertNotIn("| 2026-06 | 1 | 1,000 | 0.0 | 0.0 | 0.0 | 40.0 | — |", out)


class TestRunScorecard(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        tp = Path(self.tmp) / "t.jsonl"
        with open(tp, "w") as fh:
            for r in [
                {"type": "user", "uuid": "t1", "timestamp": "2026-06-28T10:01:00Z",
                 "message": {"role": "user", "content": "build the feature"}},
                {"type": "assistant", "uuid": "a1x", "timestamp": "2026-06-28T10:01:02Z",
                 "message": {"role": "assistant", "id": "a1", "model": "claude-opus-4-8",
                             "content": [{"type": "text", "text": "ok"}],
                             "usage": {"input_tokens": 5, "output_tokens": 50,
                                       "cache_read_input_tokens": 0,
                                       "cache_creation_input_tokens": 0}}},
            ]:
                fh.write(json.dumps(r) + "\n")
        pl = {"session_id": "s1", "transcript_path": str(tp), "cwd": "/x/proj"}
        ingest.on_session_start(pl, self.tmp)
        ingest.on_stop(pl, self.tmp)
        ingest.on_session_end(pl, self.tmp)
        self.conn = db.connect(self.tmp)
        self.run_id = self.conn.execute("SELECT run_id FROM runs").fetchone()[0]
        self.turn_id = self.conn.execute("SELECT turn_id FROM turns").fetchone()[0]

    def test_unknown_run(self):
        self.assertIn("No run found", report.render_run(self.conn, "nope"))

    def test_scorecard_sections(self):
        out = report.render_run(self.conn, self.run_id)
        for section in ["# Run scorecard", "## Approach", "## Cost & output",
                        "## Friction & context", "## Outcome"]:
            self.assertIn(section, out)
        self.assertIn("output tokens", out)
        self.assertIn("inferred", out)  # passive run -> inferred outcome

    def test_scorecard_includes_judge_and_prompt_quality(self):
        evaluate.persist(self.conn, self.run_id, {
            "overall_grade": "A", "notes": "solid",
            "run_scores": [{"dimension": "ownership_dodging", "score": 2,
                            "rationale": "took ownership"}],
            "prompt_scores": [{"turn_id": self.turn_id, "dimension": "clarity",
                               "score": 2, "rationale": "clear ask"}]})
        out = report.render_run(self.conn, self.run_id)
        self.assertIn("## Judge verdict", out)
        self.assertIn("### Agent behavior", out)
        self.assertIn("ownership_dodging", out)
        self.assertIn("### Prompt quality", out)
        self.assertIn("clarity", out)


if __name__ == "__main__":
    unittest.main()
