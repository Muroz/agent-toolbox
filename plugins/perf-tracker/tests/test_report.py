"""Slice 3 — /usage-report overview.

    python3 -m unittest discover -s tests
"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import db  # noqa: E402
import ingest  # noqa: E402
import report  # noqa: E402


def _write_transcript(path: Path) -> None:
    rows = [
        {"type": "user", "uuid": "p1", "timestamp": "2026-06-28T10:00:01Z",
         "message": {"role": "user", "content": "do the thing"}},
        {"type": "assistant", "uuid": "a1x", "timestamp": "2026-06-28T10:00:03Z",
         "message": {"role": "assistant", "id": "a1", "model": "claude-opus-4-8",
                     "content": [{"type": "text", "text": "ok"},
                                 {"type": "tool_use", "name": "Bash", "input": {}}],
                     "usage": {"input_tokens": 7, "output_tokens": 11,
                               "cache_read_input_tokens": 3,
                               "cache_creation_input_tokens": 2}}},
    ]
    with open(path, "w") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")


class TestOverview(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_empty_db_is_friendly(self):
        db.init_db(self.tmp)
        out = report.render_overview_for(self.tmp)
        self.assertIn("No usage captured yet", out)
        self.assertNotIn("Traceback", out)

    def _capture_one(self):
        tpath = Path(self.tmp) / "t.jsonl"
        _write_transcript(tpath)
        payload = {"session_id": "s1", "transcript_path": str(tpath),
                   "cwd": "/home/me/Coding/demo"}
        ingest.on_session_start(payload, self.tmp)
        ingest.on_stop(payload, self.tmp)
        ingest.on_session_end(payload, self.tmp)

    def test_overview_reconciles_and_renders(self):
        self._capture_one()
        out = report.render_overview_for(self.tmp)
        self.assertIn("# Usage overview", out)
        self.assertIn("1 runs · 1 prompts", out)
        # totals reconcile with the captured turn
        self.assertIn("| input tokens | 7 |", out)
        self.assertIn("| output tokens | 11 |", out)
        self.assertIn("| tool calls | 1 |", out)
        # breakdowns present
        self.assertIn("claude-opus-4-8", out)
        self.assertIn("demo", out)  # project from cwd basename
        self.assertIn("## By model", out)
        self.assertIn("## By project", out)
        self.assertIn("## By day", out)

    def test_default_view_is_overview_via_cli(self):
        self._capture_one()
        proc = subprocess.run(
            [sys.executable, str(SCRIPTS / "report.py"), "--data-dir", self.tmp],
            capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0)
        self.assertIn("# Usage overview", proc.stdout)

    def test_explicit_overview_via_cli(self):
        self._capture_one()
        proc = subprocess.run(
            [sys.executable, str(SCRIPTS / "report.py"),
             "overview", "--data-dir", self.tmp],
            capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0)
        self.assertIn("# Usage overview", proc.stdout)


if __name__ == "__main__":
    unittest.main()
