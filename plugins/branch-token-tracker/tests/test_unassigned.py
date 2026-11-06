"""Work on branches with no ticket: reporting it by session, and purging it.

Capture always stores it under the fallback ticket (`unassigned`). The report
can show that bucket as one row or one row per session, and
`btt purge-unassigned` can delete it. A purge must stick: capture and backfill
both re-insert any transcript turn they have not stored, under whatever branch
is checked out at the time, so a purged turn would otherwise come back billed
to a ticket.

    python3 -m unittest discover -s tests
"""

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import db  # noqa: E402
import ingest  # noqa: E402
import maintenance  # noqa: E402
import report  # noqa: E402


def _usage(out=100):
    return {"input_tokens": 10, "output_tokens": out,
            "cache_read_input_tokens": 1000,
            "cache_creation_input_tokens": 50}


def _exchange(uid, ts, text="do the thing"):
    return [
        {"type": "user", "uuid": uid, "timestamp": ts,
         "message": {"role": "user", "content": text}},
        {"type": "assistant", "uuid": f"a-{uid}", "timestamp": ts,
         "message": {"role": "assistant", "id": f"m-{uid}",
                     "model": "claude-opus-5",
                     "content": [{"type": "text", "text": "ok"}],
                     "usage": _usage()}},
    ]


def _write(path, records):
    with open(path, "w") as fh:
        for r in records:
            fh.write(json.dumps(r) + "\n")


class _Store(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.data = self.root / "data"
        self.data.mkdir()
        db.init_db(str(self.data))
        self.conn = db.connect(str(self.data))
        self.addCleanup(self.conn.close)
        self._n = 0

    def turn(self, ticket, session, *, branch="main", out=100):
        self._n += 1
        self.conn.execute(
            """INSERT INTO turns (turn_id, session_id, project, branch, ticket,
                   model, started_at, ended_at, input_tokens, output_tokens,
                   cache_read_tokens, cache_creation_tokens)
               VALUES (?,?,?,?,?,'claude-opus-5',?,?,10,?,1000,50)""",
            (f"t{self._n}", session, "repo", branch, ticket,
             f"2026-09-01T10:{self._n:02d}:00Z",
             f"2026-09-01T10:{self._n:02d}:30Z", out))
        self.conn.commit()

    def session(self, sid, title=None, first_prompt=None):
        self.conn.execute(
            "INSERT INTO sessions (session_id, title, first_prompt) "
            "VALUES (?,?,?)", (sid, title, first_prompt))
        self.conn.commit()

    def count(self, sql, params=()):
        return self.conn.execute(sql, params).fetchone()[0]


class TestReportBySession(_Store):
    def setUp(self):
        super().setUp()
        self.turn("PROJ-1", "s1", branch="feature/PROJ-1")
        self.turn("PROJ-1", "s2", branch="feature/PROJ-1")
        self.turn("unassigned", "s3", out=500)
        self.turn("unassigned", "s4", out=50)
        self.session("s3", title="Fix the flaky test")
        self.session("s4", first_prompt="look at the logs")

    def test_bucket_is_the_default(self):
        rows = report.by_ticket(self.conn)
        self.assertEqual(sorted(r["ticket"] for r in rows),
                         ["PROJ-1", "unassigned"])

    def test_sessions_splits_only_the_fallback(self):
        rows = report.by_ticket(self.conn, split_fallback="unassigned")
        got = {(r["ticket"], r["session_id"]) for r in rows}
        self.assertEqual(got, {("PROJ-1", None), ("unassigned", "s3"),
                               ("unassigned", "s4")})
        proj = next(r for r in rows if r["ticket"] == "PROJ-1")
        self.assertEqual(proj["sessions"], 2)

    def test_split_keeps_the_totals(self):
        lump = report.by_ticket(self.conn)
        split = report.by_ticket(self.conn, split_fallback="unassigned")
        self.assertEqual(sum(r["total_tokens"] for r in lump),
                         sum(r["total_tokens"] for r in split))
        # Weighted is rounded per row, so more rows can drift by one each.
        self.assertAlmostEqual(sum(r["weighted_tokens"] for r in lump),
                               sum(r["weighted_tokens"] for r in split),
                               delta=len(split))

    def test_markdown_names_each_session(self):
        out = report.render_tickets(
            report.by_ticket(self.conn, split_fallback="unassigned"), "", "",
            None)
        self.assertIn("unassigned: Fix the flaky test (s3)", out)
        self.assertIn('unassigned: "look at the logs" (s4)', out)
        self.assertIn("**2 ticket(s)", out)

    def test_export_keeps_titles_out_unless_asked(self):
        rows = report.by_ticket(self.conn, split_fallback="unassigned")
        csv_out = report.render(rows, "csv", ticket=None, window="", notes="",
                                project=None)
        self.assertNotIn("flaky", csv_out)
        self.assertIn("session_id", csv_out.splitlines()[0])
        with_text = report.render(rows, "csv", ticket=None, window="",
                                  notes="", project=None, include_text=True)
        self.assertIn("Fix the flaky test", with_text)

    def test_period_split_by_session(self):
        rows = report.by_period(self.conn, "day", split=True,
                                split_fallback="unassigned")
        got = sorted((r["ticket"], r["session_id"] or "") for r in rows)
        self.assertEqual(got, [("PROJ-1", ""), ("unassigned", "s3"),
                               ("unassigned", "s4")])


class TestPurge(_Store):
    def setUp(self):
        super().setUp()
        self.turn("PROJ-1", "mixed", branch="feature/PROJ-1")
        self.turn("unassigned", "mixed")
        self.turn("unassigned", "only")
        self.session("mixed", title="half and half")
        self.session("only", title="personal stuff")

    def test_dry_run_changes_nothing(self):
        stats = maintenance.purge(self.conn, "unassigned", apply=False)
        self.assertEqual((stats["turns"], stats["sessions"],
                          stats["sessions_removed"]), (2, 2, 1))
        self.assertEqual(self.count("SELECT COUNT(*) FROM turns"), 3)
        self.assertEqual(self.count("SELECT COUNT(*) FROM purged_turns"), 0)

    def test_apply_deletes_and_tombstones(self):
        stats = maintenance.purge(self.conn, "unassigned", apply=True,
                                  backup_dir=self.root)
        self.assertEqual(self.count(
            "SELECT COUNT(*) FROM turns WHERE ticket = 'unassigned'"), 0)
        self.assertEqual(self.count("SELECT COUNT(*) FROM turns"), 1)
        self.assertEqual(self.count("SELECT COUNT(*) FROM purged_turns"), 2)
        sessions = {r[0] for r in self.conn.execute(
            "SELECT session_id FROM sessions")}
        self.assertEqual(sessions, {"mixed"})  # still has a PROJ-1 turn

        backup = sqlite3.connect(stats["backup"])
        try:
            self.assertEqual(backup.execute(
                "SELECT COUNT(*) FROM turns").fetchone()[0], 3)
        finally:
            backup.close()

    def test_nothing_to_purge(self):
        maintenance.purge(self.conn, "unassigned", apply=True,
                          backup_dir=self.root)
        stats = maintenance.purge(self.conn, "unassigned", apply=True,
                                  backup_dir=self.root)
        self.assertEqual(stats["turns"], 0)
        self.assertIsNone(stats["backup"])


class TestPurgeSticks(_Store):
    """Purged turns never come back, under any ticket."""

    def setUp(self):
        super().setUp()
        self.path = self.root / "projects" / "proj" / "s1.jsonl"
        self.path.parent.mkdir(parents=True)
        _write(self.path, _exchange("u1", "2026-09-01T10:00:00Z", "private"))
        ingest.capture(self.conn, "s1", str(self.path), project="repo",
                       branch="main", ticket="unassigned")
        maintenance.purge(self.conn, "unassigned", apply=True,
                          backup_dir=self.root)

    def test_capture_on_a_ticket_branch_skips_purged_turns(self):
        # The user resumes the session after checking out a ticket branch.
        _write(self.path, _exchange("u1", "2026-09-01T10:00:00Z", "private")
               + _exchange("u2", "2026-09-01T11:00:00Z"))
        written = ingest.capture(self.conn, "s1", str(self.path),
                                 project="repo", branch="feature/PROJ-9",
                                 ticket="PROJ-9")
        self.assertEqual(written, 1)
        got = dict(self.conn.execute("SELECT turn_id, ticket FROM turns"))
        self.assertEqual(got, {"u2": "PROJ-9"})

    def test_rereading_a_purged_session_restores_no_session_row(self):
        ingest.capture(self.conn, "s1", str(self.path), project="repo",
                       branch="main", ticket="unassigned")
        self.assertEqual(self.count("SELECT COUNT(*) FROM turns"), 0)
        self.assertEqual(self.count("SELECT COUNT(*) FROM sessions"), 0)

    def test_new_unassigned_work_is_still_captured(self):
        _write(self.path, _exchange("u1", "2026-09-01T10:00:00Z", "private")
               + _exchange("u3", "2026-09-02T10:00:00Z"))
        ingest.capture(self.conn, "s1", str(self.path), project="repo",
                       branch="main", ticket="unassigned")
        got = dict(self.conn.execute("SELECT turn_id, ticket FROM turns"))
        self.assertEqual(got, {"u3": "unassigned"})

    def test_backfill_skips_purged_turns(self):
        # A session with a surviving ticket turn is one backfill re-reads.
        _write(self.path, _exchange("u1", "2026-09-01T10:00:00Z", "private")
               + _exchange("u2", "2026-09-01T11:00:00Z"))
        ingest.capture(self.conn, "s1", str(self.path), project="repo",
                       branch="feature/PROJ-9", ticket="PROJ-9")
        with mock.patch.object(maintenance, "PROJECTS_DIR",
                               str(self.root / "projects")):
            stats = maintenance.backfill(self.conn)
        self.assertEqual(stats["added"], 0)
        got = dict(self.conn.execute("SELECT turn_id, ticket FROM turns"))
        self.assertEqual(got, {"u2": "PROJ-9"})


if __name__ == "__main__":
    unittest.main()
