"""Transcript parsing and per-branch capture.

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
import ingest  # noqa: E402
import transcript  # noqa: E402


def _usage(inp=10, out=100, cread=1000, ccreate=50):
    return {"input_tokens": inp, "output_tokens": out,
            "cache_read_input_tokens": cread,
            "cache_creation_input_tokens": ccreate}


def _write_transcript(path, records):
    with open(path, "w") as fh:
        for r in records:
            fh.write(json.dumps(r) + "\n")


def _prompt(uid, ts, text="do the thing"):
    return {"type": "user", "uuid": uid, "timestamp": ts,
            "message": {"role": "user", "content": text}}


def _reply(uid, mid, ts, usage=None, tools=0, model="claude-opus-5"):
    content = [{"type": "text", "text": "ok"}]
    content += [{"type": "tool_use", "name": "Read", "input": {}}
                for _ in range(tools)]
    return {"type": "assistant", "uuid": uid, "timestamp": ts,
            "message": {"role": "assistant", "id": mid, "model": model,
                        "content": content, "usage": usage or _usage()}}


class TestTranscriptParsing(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = str(Path(self.tmp) / "session.jsonl")

    def test_sums_the_token_envelope(self):
        _write_transcript(self.path, [
            _prompt("t1", "2026-08-01T10:00:00Z"),
            _reply("a1", "m1", "2026-08-01T10:00:05Z", _usage(5, 50, 500, 25),
                   tools=2),
        ])
        turns = transcript.parse_turns(self.path)
        self.assertEqual(len(turns), 1)
        t = turns[0]
        self.assertEqual(
            (t.input_tokens, t.output_tokens, t.cache_read_tokens,
             t.cache_creation_tokens, t.num_tool_calls),
            (5, 50, 500, 25, 2))
        self.assertEqual(t.model, "claude-opus-5")
        self.assertEqual(t.ended_at, "2026-08-01T10:00:05Z")

    def test_tool_results_are_not_turn_boundaries(self):
        # tool results are type=user too; counting them multiplies the turn count
        _write_transcript(self.path, [
            _prompt("t1", "2026-08-01T10:00:00Z"),
            _reply("a1", "m1", "2026-08-01T10:00:05Z"),
            {"type": "user", "uuid": "tr1", "timestamp": "2026-08-01T10:00:06Z",
             "toolUseResult": {"stdout": "…"},
             "message": {"role": "user", "content": "result"}},
            _reply("a2", "m2", "2026-08-01T10:00:08Z"),
        ])
        self.assertEqual(len(transcript.parse_turns(self.path)), 1)

    def test_meta_records_are_not_turn_boundaries(self):
        _write_transcript(self.path, [
            {"type": "user", "uuid": "meta", "isMeta": True,
             "timestamp": "2026-08-01T09:59:00Z",
             "message": {"role": "user", "content": "<system>"}},
            _prompt("t1", "2026-08-01T10:00:00Z"),
            _reply("a1", "m1", "2026-08-01T10:00:05Z"),
        ])
        turns = transcript.parse_turns(self.path)
        self.assertEqual([t.turn_id for t in turns], ["t1"])

    def test_repeated_message_ids_are_deduped_not_summed(self):
        # the same message.id streams more than once, each copy carrying the
        # cumulative usage; summing them multiplies the counts
        _write_transcript(self.path, [
            _prompt("t1", "2026-08-01T10:00:00Z"),
            _reply("a1", "m1", "2026-08-01T10:00:03Z", _usage(1, 10, 100, 5)),
            _reply("a1b", "m1", "2026-08-01T10:00:05Z", _usage(1, 40, 100, 5)),
        ])
        t = transcript.parse_turns(self.path)[0]
        self.assertEqual(t.output_tokens, 40)   # last wins, not 10 + 40

    def test_unanswered_prompt_is_dropped(self):
        _write_transcript(self.path, [_prompt("t1", "2026-08-01T10:00:00Z")])
        self.assertEqual(transcript.parse_turns(self.path), [])

    def test_malformed_lines_are_skipped(self):
        with open(self.path, "w") as fh:
            fh.write("{not json\n")
            fh.write(json.dumps(_prompt("t1", "2026-08-01T10:00:00Z")) + "\n")
            fh.write("\n")
            fh.write(json.dumps(_reply("a1", "m1", "2026-08-01T10:00:05Z")) + "\n")
        self.assertEqual(len(transcript.parse_turns(self.path)), 1)

    def test_missing_file_is_not_an_error(self):
        self.assertEqual(transcript.parse_turns(self.path + ".nope"), [])


class TestCapture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        db.init_db(self.tmp)
        self.conn = db.connect(self.tmp)
        self.path = str(Path(self.tmp) / "session.jsonl")

    def _capture(self, ticket, branch, session="s1"):
        return ingest.capture(self.conn, session, self.path, project="repo",
                              branch=branch, ticket=ticket)

    def test_capture_is_insert_only(self):
        _write_transcript(self.path, [
            _prompt("t1", "2026-08-01T10:00:00Z"),
            _reply("a1", "m1", "2026-08-01T10:00:05Z"),
        ])
        self.assertEqual(self._capture("PROJ-1", "feature/PROJ-1"), 1)
        # re-reading the same transcript writes nothing new
        self.assertEqual(self._capture("PROJ-1", "feature/PROJ-1"), 0)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM turns").fetchone()[0], 1)

    def test_mid_session_branch_switch_splits_the_tickets(self):
        # turn 1 runs on PROJ-1 …
        _write_transcript(self.path, [
            _prompt("t1", "2026-08-01T10:00:00Z"),
            _reply("a1", "m1", "2026-08-01T10:00:05Z"),
        ])
        self._capture("PROJ-1", "feature/PROJ-1")
        # … then the user switches branch and keeps working in the same session
        _write_transcript(self.path, [
            _prompt("t1", "2026-08-01T10:00:00Z"),
            _reply("a1", "m1", "2026-08-01T10:00:05Z"),
            _prompt("t2", "2026-08-01T11:00:00Z"),
            _reply("a2", "m2", "2026-08-01T11:00:05Z"),
        ])
        self.assertEqual(self._capture("PROJ-2", "feature/PROJ-2"), 1)
        got = dict(self.conn.execute("SELECT turn_id, ticket FROM turns"))
        self.assertEqual(got, {"t1": "PROJ-1", "t2": "PROJ-2"})

    def test_missing_transcript_captures_nothing(self):
        self.assertEqual(
            ingest.capture(self.conn, "s1", "/no/such/file.jsonl",
                           project="repo", branch=None, ticket="unassigned"), 0)

    def test_current_branch_outside_a_repo_is_none(self):
        self.assertIsNone(ingest.current_branch(self.tmp))
        self.assertIsNone(ingest.current_branch("/no/such/dir"))
        self.assertIsNone(ingest.current_branch(None))


if __name__ == "__main__":
    unittest.main()
