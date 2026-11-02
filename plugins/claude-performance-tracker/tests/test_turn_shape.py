"""Per-turn cost shape: model calls, context size, tool output, idle gaps.

    python3 -m unittest discover -s tests
"""

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import db  # noqa: E402
import store  # noqa: E402


def _reply(mid, ts, cread=1000, ccreate=50):
    return {"type": "assistant", "uuid": f"{mid}-{ts}", "timestamp": ts,
            "message": {"role": "assistant", "id": mid,
                        "model": "claude-opus-5-5",
                        "content": [{"type": "text", "text": "ok"}],
                        "usage": {"input_tokens": 10, "output_tokens": 100,
                                  "cache_read_input_tokens": cread,
                                  "cache_creation_input_tokens": ccreate}}}


def _prompt(uid, ts):
    return {"type": "user", "uuid": uid, "timestamp": ts,
            "message": {"role": "user", "content": "go"}}


class TestTurnShape(unittest.TestCase):
    def test_capture_stores_calls_context_and_idle_cache_rewrites(self):
        tmp = Path(tempfile.mkdtemp())
        path = tmp / "s.jsonl"
        rows = [_prompt("p1", "2026-08-01T10:00:00Z"),
                _reply("m1", "2026-08-01T10:00:05Z"),
                _prompt("p2", "2026-08-01T10:30:00Z"),
                _reply("m2", "2026-08-01T10:30:05Z", ccreate=90000),
                _reply("m3", "2026-08-01T10:30:09Z", cread=91000)]
        path.write_text("".join(json.dumps(r) + "\n" for r in rows))
        db.init_db(str(tmp))
        conn = db.connect(str(tmp))
        run_id = store.open_passive_run(conn, "sess", str(path), "proj")
        store.capture_session_turns(conn, run_id, "sess", str(path))
        got = conn.execute(
            "SELECT turn_id, api_calls, peak_context_tokens, idle_gaps, "
            "cold_cache_write_tokens FROM turns ORDER BY seq").fetchall()
        self.assertEqual([tuple(r) for r in got],
                         [("p1", 1, 1060, 0, 0),
                          ("p2", 2, 91060, 1, 90000)])
        conn.close()


if __name__ == "__main__":
    unittest.main()
