"""Feature 2 — anti-pattern / incident-to-eval catalog (report antipatterns).

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


def _run(conn, *, ttype="debug", outcome="failed", started="2026-07-10T10:00:00Z",
         **friction):
    _COUNTER[0] += 1
    f = {"interrupts": 0, "re_prompts": 0, "edits_without_read": 0,
         "reasoning_loops": 0, "premature_stops": 0}
    f.update(friction)
    conn.execute(
        """INSERT INTO runs
           (run_id, capture_mode, started_at, ended_at, task_type, outcome,
            outcome_source, interrupts, re_prompts, edits_without_read,
            reasoning_loops, premature_stops, source)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?, 'transcript')""",
        (f"run-{_COUNTER[0]}", "passive", started, started, ttype, outcome,
         "inferred", f["interrupts"], f["re_prompts"], f["edits_without_read"],
         f["reasoning_loops"], f["premature_stops"]))
    conn.commit()


class TestAntipatterns(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        db.init_db(self.tmp)
        self.conn = db.connect(self.tmp)

    def test_empty_is_friendly(self):
        self.assertIn("No friction signals recorded yet",
                      report.render_antipatterns(self.conn))

    def test_lists_friction_with_rubric_mapping(self):
        _run(self.conn, edits_without_read=1)
        _run(self.conn, edits_without_read=2)
        out = report.render_antipatterns(self.conn)
        self.assertIn("edits_without_read", out)
        self.assertIn("blind_edits", out)          # mapped rubric dim shown

    def test_flags_rubric_candidate_for_gap(self):
        # interrupts has no rubric dimension -> appears under "Rubric candidates"
        _run(self.conn, interrupts=1)
        _run(self.conn, interrupts=2)
        out = report.render_antipatterns(self.conn)
        self.assertIn("Rubric candidates", out)
        self.assertIn("`interrupts`", out)

    def test_since_window(self):
        _run(self.conn, interrupts=1, started="2000-01-01T00:00:00Z")
        self.assertIn("No friction signals",
                      report.render_antipatterns(self.conn, "30d"))

    def test_unparsable_since_says_so_instead_of_claiming_a_window(self):
        # '2w' is not in the \\d+[dh] grammar, so the query widens to all time.
        # The header must not claim a window it never applied.
        _run(self.conn, interrupts=1, started="2000-01-01T00:00:00Z")
        out = report.render_antipatterns(self.conn, "2w")
        self.assertNotIn("(last 2w)", out)
        self.assertIn("Ignoring `--since 2w`", out)
        self.assertIn("interrupts", out)          # all-time data is present

    def test_friction_lead_is_omitted_when_there_is_no_friction_table(self):
        # prompt scores but zero friction: the "Recurring friction…" lead would
        # otherwise head an empty section.
        self.conn.execute(
            "INSERT INTO turns (turn_id, run_id, session_id, seq) "
            "VALUES ('t1','run-x','s1',0)")
        self.conn.execute(
            "INSERT INTO scores (subject_type, subject_id, dimension, score, "
            "rubric_version, created_at) "
            "VALUES ('prompt','t1','clarity',1,'2','2026-07-10T10:00:00Z')")
        self.conn.commit()
        out = report.render_antipatterns(self.conn)
        self.assertNotIn("Recurring friction across runs", out)
        self.assertIn("Weakest prompt habits", out)

    def test_mixed_rubric_versions_are_flagged_not_silently_pooled(self):
        for i, ver in enumerate(("1", "2")):
            self.conn.execute(
                "INSERT INTO turns (turn_id, run_id, session_id, seq) "
                "VALUES (?,'run-x','s1',?)", (f"t{i}", i))
            self.conn.execute(
                "INSERT INTO scores (subject_type, subject_id, dimension, score, "
                "rubric_version, created_at) "
                "VALUES ('prompt',?,'clarity',?,?,'2026-07-10T10:00:00Z')",
                (f"t{i}", i, ver))
        self.conn.commit()
        out = report.render_antipatterns(self.conn)
        self.assertIn("pool rubric v1, v2", out)


if __name__ == "__main__":
    unittest.main()
