"""Slice 6 — /usage-report compare.

    python3 -m unittest discover -s tests
"""

import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import db  # noqa: E402
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


class TestCompare(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        db.init_db(self.tmp)
        self.conn = db.connect(self.tmp)

    def test_empty_is_friendly(self):
        out = report.render_compare(self.conn, "model", 3)
        self.assertIn("No self-reported successful tracked runs", out)

    def test_ranks_cheaper_approach_first(self):
        # feature/M: opus-4-8 cheap (median 200), opus-4-7 expensive (median 1500)
        for o in (100, 200, 300):
            _run(self.conn, model="claude-opus-4-8", out=o)
        for o in (1000, 2000):
            _run(self.conn, model="claude-opus-4-7", out=o)
        out = report.render_compare(self.conn, "model", 3)
        self.assertIn("feature · M", out)
        self.assertIn("(5 successful runs)", out)
        self.assertLess(out.index("claude-opus-4-8"), out.index("claude-opus-4-7"))

    def test_small_sample_guard(self):
        for o in (100, 200):  # only 2 successful in feature/S
            _run(self.conn, size="S", out=o)
        out = report.render_compare(self.conn, "model", 3)
        self.assertIn("feature · S", out)
        self.assertIn("insufficient data: 2 successful", out)

    def test_only_selfreport_success_counts(self):
        for o in (100, 200, 300):
            _run(self.conn, out=o)                       # 3 self-report successes
        _run(self.conn, outcome="failed", out=10)        # excluded
        _run(self.conn, outcome="partial", out=10)       # excluded
        _run(self.conn, outcome="success", src="inferred", ttype=None, size=None)
        out = report.render_compare(self.conn, "model", 3)
        self.assertIn("(3 successful runs)", out)        # failed/partial not counted
        self.assertIn("1 inferred-success run(s) excluded", out)

    def test_group_by_mode(self):
        for o in (100, 200, 300):
            _run(self.conn, pmode="acceptEdits", out=o)
        for o in (50, 60, 70):
            _run(self.conn, pmode="plan", out=o)
        out = report.render_compare(self.conn, "mode", 3)
        self.assertIn("by mode", out)
        # plan (median 60) is cheaper than acceptEdits (median 200)
        self.assertLess(out.index("plan"), out.index("acceptEdits"))


if __name__ == "__main__":
    unittest.main()
