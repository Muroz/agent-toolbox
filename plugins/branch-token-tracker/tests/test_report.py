"""Reporting: rollup per ticket, drilldown, windows, output formats.

    python3 -m unittest discover -s tests
"""

import csv
import io
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import db  # noqa: E402
import report  # noqa: E402

_COUNTER = [0]


def _turn(conn, *, ticket, branch="feature/x", session="s1", project="repo",
          started="2026-08-01T10:00:00Z", ended="2026-08-01T10:05:00Z",
          inp=10, out=100, cread=1000, ccreate=50):
    _COUNTER[0] += 1
    conn.execute(
        """INSERT INTO turns
           (turn_id, session_id, project, branch, ticket, model, started_at,
            ended_at, input_tokens, output_tokens, cache_read_tokens,
            cache_creation_tokens, num_tool_calls)
           VALUES (?,?,?,?,?,'claude-opus-5',?,?,?,?,?,?,3)""",
        (f"t{_COUNTER[0]}", session, project, branch, ticket, started, ended,
         inp, out, cread, ccreate))
    conn.commit()


class _DB(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        db.init_db(self.tmp)
        self.conn = db.connect(self.tmp)


class TestRollup(_DB):
    def test_groups_by_ticket_and_sorts_by_spend(self):
        _turn(self.conn, ticket="PROJ-1", out=100)
        _turn(self.conn, ticket="PROJ-1", out=100)
        _turn(self.conn, ticket="PROJ-2", out=5000)
        rows = report.by_ticket(self.conn)
        self.assertEqual([r["ticket"] for r in rows], ["PROJ-2", "PROJ-1"])
        proj1 = next(r for r in rows if r["ticket"] == "PROJ-1")
        self.assertEqual(proj1["turns"], 2)
        self.assertEqual(proj1["output_tokens"], 200)
        # total spans all four token buckets, cache included
        self.assertEqual(proj1["total_tokens"], 2 * (10 + 100 + 1000 + 50))

    def test_counts_distinct_sessions_not_turns(self):
        _turn(self.conn, ticket="PROJ-1", session="s1")
        _turn(self.conn, ticket="PROJ-1", session="s1")
        _turn(self.conn, ticket="PROJ-1", session="s2")
        self.assertEqual(report.by_ticket(self.conn)[0]["sessions"], 2)

    def test_collects_every_branch_that_fed_a_ticket(self):
        _turn(self.conn, ticket="PROJ-1", branch="feature/PROJ-1")
        _turn(self.conn, ticket="PROJ-1", branch="hotfix/PROJ-1")
        self.assertEqual(report.by_ticket(self.conn)[0]["branches"],
                         ["feature/PROJ-1", "hotfix/PROJ-1"])

    def test_project_filter(self):
        _turn(self.conn, ticket="PROJ-1", project="alpha")
        _turn(self.conn, ticket="PROJ-2", project="beta")
        rows = report.by_ticket(self.conn, project="alpha")
        self.assertEqual([r["ticket"] for r in rows], ["PROJ-1"])

    def test_wall_clock_spans_first_to_last(self):
        _turn(self.conn, ticket="PROJ-1", started="2026-08-01T10:00:00Z",
              ended="2026-08-01T10:30:00Z")
        self.assertEqual(report.by_ticket(self.conn)[0]["wall_clock_ms"],
                         30 * 60 * 1000)


class TestDrilldown(_DB):
    def test_one_row_per_session_and_branch(self):
        _turn(self.conn, ticket="PROJ-1", session="s1", branch="feature/PROJ-1")
        _turn(self.conn, ticket="PROJ-1", session="s1", branch="feature/PROJ-1")
        _turn(self.conn, ticket="PROJ-1", session="s2", branch="hotfix/PROJ-1")
        _turn(self.conn, ticket="PROJ-2", session="s3")
        rows = report.for_ticket(self.conn, "PROJ-1")
        self.assertEqual(len(rows), 2)
        self.assertEqual(sum(r["turns"] for r in rows), 3)

    def test_unknown_ticket_is_explained_not_blank(self):
        out = report.render_drilldown("NOPE-1", [], "", "")
        self.assertIn("No turns recorded for this ticket", out)


class TestWindows(_DB):
    def test_valid_window_filters(self):
        recent = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        _turn(self.conn, ticket="OLD-1", started="2000-01-01T00:00:00Z")
        _turn(self.conn, ticket="NEW-1", started=recent)
        cutoff, ok = report.parse_bound("30d")
        self.assertTrue(ok)
        rows = report.by_ticket(self.conn, cutoff)
        self.assertEqual([r["ticket"] for r in rows], ["NEW-1"])

    def test_unparsable_window_says_so_rather_than_claiming_one(self):
        _turn(self.conn, ticket="PROJ-1", started="2000-01-01T00:00:00Z")
        cutoff, ok = report.parse_bound("2w")
        self.assertIsNone(cutoff)
        self.assertFalse(ok)
        bad = [("since", "2w")]
        out = report.render_tickets(
            report.by_ticket(self.conn, cutoff),
            report._window("2w", None, bad), report._notes(bad), None)
        self.assertNotIn("(last 2w)", out)
        self.assertIn("Ignoring `--since 2w`", out)
        self.assertIn("PROJ-1", out)          # all-time data still shown

    def test_no_window_is_not_an_error(self):
        self.assertEqual(report.parse_bound(None), (None, True))


class TestFormats(_DB):
    def setUp(self):
        super().setUp()
        _turn(self.conn, ticket="PROJ-1", branch="feature/PROJ-1")
        _turn(self.conn, ticket="#883", branch="fix/#883-x")
        self.rows = report.by_ticket(self.conn)

    def test_markdown_is_a_table(self):
        out = report.render(self.rows, "markdown", ticket=None, window="",
                            notes="", project=None)
        self.assertIn("| ticket |", out)
        self.assertIn("PROJ-1", out)
        self.assertIn("#883", out)

    def test_json_round_trips(self):
        out = report.render(self.rows, "json", ticket=None, window="",
                            notes="", project=None)
        parsed = json.loads(out)
        self.assertEqual({r["ticket"] for r in parsed}, {"PROJ-1", "#883"})

    def test_csv_has_a_header_and_one_row_per_ticket(self):
        out = report.render(self.rows, "csv", ticket=None, window="",
                            notes="", project=None)
        parsed = list(csv.DictReader(io.StringIO(out)))
        self.assertEqual(len(parsed), 2)
        self.assertIn("total_tokens", parsed[0])
        # the thing being measured leads, not the numbers
        self.assertEqual(out.splitlines()[0].split(",")[0], "ticket")
        # the list-valued column is dropped rather than mangled into the csv
        self.assertNotIn("branches", parsed[0])

    def test_empty_store_is_friendly_not_a_crash(self):
        empty = report.render_tickets([], None, True, None)
        self.assertIn("No turns captured yet", empty)
        self.assertEqual(report.render_csv([]), "")


if __name__ == "__main__":
    unittest.main()
