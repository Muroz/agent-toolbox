"""Shared read-time insight aggregations (insights.py).

    python3 -m unittest discover -s tests
"""

import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import db  # noqa: E402
import insights  # noqa: E402

_COUNTER = [0]


def _run(conn, *, ttype="feature", size="M", outcome="success", src="self_report",
         model="claude-opus-4-8", out=100, prompts=1, started="2026-06-10T10:00:00Z",
         friction=None, satisfaction=None, note=None, mode="tracked"):
    _COUNTER[0] += 1
    rid = f"run-{_COUNTER[0]}"
    f = {"interrupts": 0, "re_prompts": 0, "edits_without_read": 0,
         "reasoning_loops": 0, "premature_stops": 0}
    f.update(friction or {})
    conn.execute(
        """INSERT INTO runs
           (run_id, capture_mode, started_at, ended_at, task_type, size_class,
            outcome, outcome_source, models, output_tokens, num_prompts,
            interrupts, re_prompts, edits_without_read, reasoning_loops,
            premature_stops, satisfaction, note, source)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'transcript')""",
        (rid, mode, started, started, ttype, size, outcome, src, model, out,
         prompts, f["interrupts"], f["re_prompts"], f["edits_without_read"],
         f["reasoning_loops"], f["premature_stops"], satisfaction, note))
    conn.commit()
    return rid


def _prompt_score(conn, dim, score):
    _COUNTER[0] += 1
    conn.execute(
        """INSERT INTO scores
           (subject_type, subject_id, dimension, score, rationale,
            rubric_version, created_at)
           VALUES ('prompt', ?, ?, ?, '', '1', '2026-06-10T10:00:00Z')""",
        (f"turn-{_COUNTER[0]}", dim, score))
    conn.commit()


class TestInsights(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        db.init_db(self.tmp)
        self.conn = db.connect(self.tmp)

    def test_bucket_winners_ranks_and_flags_confidence(self):
        for o in (100, 200, 300):
            _run(self.conn, out=o)
        b = insights.bucket_winners(self.conn, "model", 5)
        self.assertEqual(len(b), 1)
        self.assertFalse(b[0]["confident"])  # 3 < 5
        # Ranking is on weighted (input-equivalent) tokens, and these runs are
        # pure output, which bills at 5x input — so the 200-token median
        # run is 1000 weighted units.
        self.assertEqual(b[0]["ranked"][0]["median_weighted_tokens"], 1000)

    def test_recurring_friction_ranked_and_mapped(self):
        _run(self.conn, friction={"edits_without_read": 2}, outcome="failed")
        _run(self.conn, friction={"edits_without_read": 1})
        _run(self.conn, friction={"interrupts": 1}, outcome="partial")
        fr = {f["signal"]: f for f in insights.recurring_friction(self.conn)}
        self.assertEqual(fr["edits_without_read"]["runs_hit"], 2)
        self.assertEqual(fr["edits_without_read"]["bad_outcome_runs"], 1)
        self.assertEqual(fr["edits_without_read"]["rubric_dimension"], "blind_edits")
        # interrupts has no rubric dimension -> a gap/candidate
        self.assertIsNone(fr["interrupts"]["rubric_dimension"])

    def test_since_window_excludes_old(self):
        _run(self.conn, friction={"interrupts": 1}, started="2000-01-01T00:00:00Z")
        self.assertEqual(insights.recurring_friction(self.conn, "30d"), [])
        self.assertEqual(len(insights.recurring_friction(self.conn)), 1)

    def test_weak_prompt_dimensions_sorted(self):
        _prompt_score(self.conn, "clarity", 2)
        _prompt_score(self.conn, "specificity", 0)
        _prompt_score(self.conn, "specificity", 1)
        weak = insights.weak_prompt_dimensions(self.conn)
        self.assertEqual(weak[0]["dimension"], "specificity")  # avg 0.5 lowest
        self.assertEqual(weak[0]["avg_score"], 0.5)

    def test_degradation_deltas_first_vs_last(self):
        _run(self.conn, started="2026-05-10T10:00:00Z", friction={"interrupts": 0})
        _run(self.conn, started="2026-07-10T10:00:00Z", friction={"interrupts": 3})
        d = insights.degradation_deltas(self.conn, "month")
        self.assertEqual(len(d), 1)
        self.assertEqual(d[0]["friction_delta"], 3.0)

    def test_synthesis_context_shape(self):
        _run(self.conn, outcome="failed", note="ran out of context")
        ctx = insights.synthesis_context(self.conn)
        for key in ("bucket_winners", "recurring_friction",
                    "weak_prompt_dimensions", "degradation_deltas",
                    "failure_notes"):
            self.assertIn(key, ctx)
        self.assertEqual(ctx["failure_notes"][0]["note"], "ran out of context")


if __name__ == "__main__":
    unittest.main()
