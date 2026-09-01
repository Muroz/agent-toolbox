"""`btt backfill` — repairing a store written by an older parser.

The turns are re-derived from the transcripts on disk, so spend that an earlier
parser could not see is recoverable rather than lost. What it must NOT do is
re-resolve the branch: a session can span several branches, and the stored
ticket is the contemporaneous record of which one the work actually ran on.

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
import maintenance  # noqa: E402


def _note(task_id, tokens):
    return ("<task-notification>\n"
            f"<task-id>{task_id}</task-id>\n"
            f"<usage><subagent_tokens>{tokens}</subagent_tokens>"
            "<tool_uses>4</tool_uses></usage>\n</task-notification>")


class BackfillTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.data = self.root / "data"
        self.data.mkdir()
        self.projects = self.root / "projects" / "proj"
        self.projects.mkdir(parents=True)
        db.init_db(str(self.data))

        self.sid = "sess-1"
        rows = [
            {"type": "user", "uuid": "u1", "timestamp": "2026-09-01T10:00:00Z",
             "message": {"role": "user", "content": "first prompt"}},
            {"type": "assistant", "uuid": "a1", "timestamp": "2026-09-01T10:00:05Z",
             "message": {"role": "assistant", "id": "m1", "model": "claude-opus-5",
                         "content": [{"type": "text", "text": "ok"}],
                         "usage": {"input_tokens": 10, "output_tokens": 100,
                                   "cache_read_input_tokens": 0,
                                   "cache_creation_input_tokens": 0}}},
            # the agent an older parser could not see
            {"type": "attachment", "uuid": "at1", "timestamp": "2026-09-01T10:02:00Z",
             "attachment": {"type": "queued_command", "prompt": _note("bg1", 9999)}},
            {"type": "user", "uuid": "u2", "timestamp": "2026-09-01T11:00:00Z",
             "message": {"role": "user", "content": "second prompt"}},
            {"type": "assistant", "uuid": "a2", "timestamp": "2026-09-01T11:00:05Z",
             "message": {"role": "assistant", "id": "m2", "model": "claude-opus-5",
                         "content": [{"type": "text", "text": "ok"}],
                         "usage": {"input_tokens": 5, "output_tokens": 50,
                                   "cache_read_input_tokens": 0,
                                   "cache_creation_input_tokens": 0}}},
            {"type": "attachment", "uuid": "at2", "timestamp": "2026-09-01T11:02:00Z",
             "attachment": {"type": "queued_command", "prompt": _note("bg2", 4444)}},
        ]
        with open(self.projects / f"{self.sid}.jsonl", "w") as fh:
            for r in rows:
                fh.write(json.dumps(r) + "\n")

        # Pre-existing rows, as an older parser would have left them: the two
        # main turns only, and on DIFFERENT branches — the session switched.
        conn = db.connect(str(self.data))
        for uuid, ts, ticket, branch in (
                ("u1", "2026-09-01T10:00:00Z", "PROJ-1", "feature/PROJ-1"),
                ("u2", "2026-09-01T11:00:00Z", "PROJ-2", "feature/PROJ-2")):
            conn.execute(
                "INSERT INTO turns (turn_id, session_id, project, branch, ticket,"
                " started_at, query_source, is_prompt) VALUES (?,?,?,?,?,?,'main',1)",
                (uuid, self.sid, "proj", branch, ticket, ts))
        conn.commit()
        conn.close()

    def _backfill(self):
        conn = db.connect(str(self.data))
        with mock.patch.object(maintenance, "PROJECTS_DIR",
                               str(self.root / "projects")):
            stats = maintenance.backfill(conn)
        conn.close()
        return stats

    def _rows(self):
        conn = sqlite3.connect(self.data / "tokens.db")
        conn.row_factory = sqlite3.Row
        try:
            return {r["turn_id"]: dict(r) for r in conn.execute("SELECT * FROM turns")}
        finally:
            conn.close()

    def test_recovers_turns_the_old_parser_missed(self):
        stats = self._backfill()
        self.assertEqual(stats["added"], 2)
        rows = self._rows()
        self.assertIn("agent:bg1", rows)
        self.assertIn("agent:bg2", rows)
        self.assertEqual(rows["agent:bg1"]["total_tokens_agg"], 9999)
        self.assertEqual(rows["agent:bg1"]["query_source"], "subagent")
        self.assertEqual(rows["agent:bg1"]["is_prompt"], 0)

    def test_new_rows_inherit_the_contemporaneous_ticket(self):
        """The agent is charged to the branch that was checked out when it ran.

        Re-resolving the branch would file both agents under whatever is checked
        out today; taking the session's first ticket would file the second one
        wrongly. Each must land on the turn it actually ran under.
        """
        self._backfill()
        rows = self._rows()
        self.assertEqual(rows["agent:bg1"]["ticket"], "PROJ-1")
        self.assertEqual(rows["agent:bg1"]["branch"], "feature/PROJ-1")
        self.assertEqual(rows["agent:bg2"]["ticket"], "PROJ-2")
        self.assertEqual(rows["agent:bg2"]["branch"], "feature/PROJ-2")

    def test_existing_attribution_is_never_rewritten(self):
        self._backfill()
        rows = self._rows()
        self.assertEqual(rows["u1"]["ticket"], "PROJ-1")
        self.assertEqual(rows["u2"]["ticket"], "PROJ-2")

    def test_counts_only_grow(self):
        """A turn captured mid-flight gets refreshed, never shrunk."""
        conn = db.connect(str(self.data))
        conn.execute("UPDATE turns SET output_tokens = 999999 WHERE turn_id = 'u1'")
        conn.commit(); conn.close()
        self._backfill()
        self.assertEqual(self._rows()["u1"]["output_tokens"], 999999)

    def test_is_idempotent(self):
        self._backfill()
        first = self._rows()
        stats = self._backfill()
        self.assertEqual(stats["added"], 0)
        self.assertEqual(self._rows(), first)

    def test_session_without_a_transcript_is_left_alone(self):
        (self.projects / f"{self.sid}.jsonl").unlink()
        stats = self._backfill()
        self.assertEqual(stats["skipped_no_transcript"], 1)
        self.assertEqual(stats["added"], 0)
        self.assertEqual(len(self._rows()), 2)  # the originals survive untouched

    def test_a_real_split_replaces_a_stored_aggregate(self):
        """The double-count trap.

        A row captured before the subagent log was read holds a bare
        `total_tokens_agg` and no split. Backfill now supplies the real split.
        `MAX()` on every column would keep BOTH — and the raw total sums the
        splits and the aggregate, so the agent would be counted twice.
        """
        import json as _json
        # a stored aggregate-only subagent row, as an older parser left it
        conn = db.connect(str(self.data))
        conn.execute(
            "INSERT INTO turns (turn_id, session_id, project, branch, ticket,"
            " started_at, query_source, is_prompt, total_tokens_agg)"
            " VALUES ('agent:bg9', ?, 'proj', 'feature/PROJ-1', 'PROJ-1',"
            " '2026-09-01T10:02:00Z', 'subagent', 0, 9999)", (self.sid,))
        conn.commit(); conn.close()

        # ...and the agent's own log, which has the real envelope
        sub = self.projects / self.sid / "subagents"
        sub.mkdir(parents=True, exist_ok=True)
        with open(sub / "agent-bg9.jsonl", "w") as fh:
            fh.write(_json.dumps({
                "type": "assistant", "uuid": "s1",
                "timestamp": "2026-09-01T10:02:00Z",
                "message": {"role": "assistant", "id": "sm1",
                            "model": "claude-opus-5",
                            "content": [{"type": "text", "text": "ok"}],
                            "usage": {"input_tokens": 2, "output_tokens": 500,
                                      "cache_read_input_tokens": 40000,
                                      "cache_creation_input_tokens": 3000}}}) + "\n")

        self._backfill()
        row = self._rows()["agent:bg9"]
        self.assertEqual(row["output_tokens"], 500)
        self.assertEqual(row["cache_read_tokens"], 40000)
        self.assertEqual(row["total_tokens_agg"], 0,
                         "stale aggregate kept beside the split — double count")
        # the row's attribution is still the one it was captured under
        self.assertEqual(row["ticket"], "PROJ-1")

    def test_an_aggregate_only_row_keeps_its_aggregate(self):
        """No log on disk: the bare total is still the best evidence there is."""
        conn = db.connect(str(self.data))
        conn.execute(
            "INSERT INTO turns (turn_id, session_id, project, branch, ticket,"
            " started_at, query_source, is_prompt, total_tokens_agg)"
            " VALUES ('agent:gone', ?, 'proj', 'b', 'PROJ-1',"
            " '2026-09-01T10:02:00Z', 'subagent', 0, 777)", (self.sid,))
        conn.commit(); conn.close()
        self._backfill()
        self.assertEqual(self._rows()["agent:gone"]["total_tokens_agg"], 777)


if __name__ == "__main__":
    unittest.main()
