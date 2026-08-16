"""Feature 1 — approach recommender (report recommend + insights.recommend).

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
import report  # noqa: E402

_COUNTER = [0]


def _run(conn, *, ttype="feature", size="M", outcome="success",
         src="self_report", model="claude-opus-4-8", out=0, prompts=1,
         wall=60000, pmode="plan", mode="tracked"):
    _COUNTER[0] += 1
    conn.execute(
        """INSERT INTO runs
           (run_id, capture_mode, started_at, task_type, size_class, outcome,
            outcome_source, models, permission_mode, output_tokens, input_tokens,
            cache_read_tokens, cache_creation_tokens, num_prompts, wall_clock_ms,
            source)
           VALUES (?,?,?,?,?,?,?,?,?,?,0,0,0,?,?, 'transcript')""",
        (f"run-{_COUNTER[0]}", mode, "2026-06-28T10:00:00Z", ttype, size, outcome,
         src, model, pmode, out, prompts, wall))
    conn.commit()


class TestRecommend(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        db.init_db(self.tmp)
        self.conn = db.connect(self.tmp)

    def test_empty_bucket_is_friendly(self):
        out = report.render_recommend(self.conn, "refactor", "L", "model", 3)
        self.assertIn("No self-reported successful runs for this bucket", out)

    def test_picks_cheapest_confident(self):
        for o in (100, 200, 300):
            _run(self.conn, model="claude-opus-4-8", out=o)   # median 200
        for o in (1000, 2000, 3000):
            _run(self.conn, model="claude-opus-4-7", out=o)   # median 2000
        out = report.render_recommend(self.conn, "feature", "M", "model", 3)
        self.assertIn("**claude-opus-4-8**", out)
        self.assertNotIn("low confidence", out)
        # both approaches listed, winner first
        self.assertLess(out.index("claude-opus-4-8"), out.index("claude-opus-4-7"))

    def test_low_confidence_flagged(self):
        for o in (100, 200):  # only 2 successes, need >= 3
            _run(self.conn, out=o)
        out = report.render_recommend(self.conn, "feature", "M", "model", 3)
        self.assertIn("low confidence", out)

    def test_per_bucket_overview(self):
        for o in (10, 20, 30):
            _run(self.conn, ttype="bugfix", size="S", out=o)
        for o in (40, 50, 60):
            _run(self.conn, ttype="refactor", size="L", out=o)
        out = report.render_recommend(self.conn, None, None, "model", 3)
        self.assertIn("Recommended approach per bucket", out)
        self.assertIn("bugfix · S", out)
        self.assertIn("refactor · L", out)

    def test_insights_recommend_layer(self):
        for o in (100, 200, 300):
            _run(self.conn, out=o)
        b = insights.recommend(self.conn, "feature", "M", "model", 3)
        self.assertTrue(b["confident"])
        self.assertEqual(b["ranked"][0]["median_total_tokens"], 200)
        # a bucket with no data comes back empty, not crashing
        empty = insights.recommend(self.conn, "debug", "S", "model", 3)
        self.assertEqual(empty["ranked"], [])
        self.assertFalse(empty["confident"])


if __name__ == "__main__":
    unittest.main()
