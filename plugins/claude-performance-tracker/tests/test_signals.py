"""Slice 5 — full deterministic envelope.

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
import signals  # noqa: E402


def _asst(mid, ts, content, usage, stop="end_turn"):
    return {"type": "assistant", "uuid": f"{mid}x", "timestamp": ts,
            "message": {"role": "assistant", "id": mid, "model": "claude-opus-4-8",
                        "stop_reason": stop, "content": content, "usage": usage}}


def _tool(name, **inp):
    return {"type": "tool_use", "name": name, "input": inp}


def _result(**kw):
    return {"type": "user", "uuid": kw.pop("uuid", "tr"), "timestamp": kw.pop("ts", "2026-06-28T10:00:00Z"),
            "toolUseResult": kw, "message": {"role": "user",
            "content": [{"type": "tool_result", "content": "ok"}]}}


def _transcript(path: Path) -> None:
    rows = [
        # --- turn 1
        {"type": "user", "uuid": "u1", "timestamp": "2026-06-28T10:01:00Z",
         "message": {"role": "user", "content": "first task"}},
        {"type": "permission-mode", "permissionMode": "plan",
         "timestamp": "2026-06-28T10:01:01Z"},
        _asst("a1", "2026-06-28T10:01:02Z",
              [_tool("Read", file_path="/x/app.py"),
               _tool("Read", file_path="/x/app.py"),
               _tool("Read", file_path="/x/app.py"),
               _tool("mcp__playwright__browser_click"),
               _tool("Agent", subagent_type="Explore", description="look")],
              {"input_tokens": 100, "cache_read_input_tokens": 50000,
               "cache_creation_input_tokens": 1000, "output_tokens": 10}),
        _result(uuid="r1", filePath="/x/app.py", content="...", interrupted=False),
        _asst("a1b", "2026-06-28T10:01:05Z",
              [_tool("Edit", file_path="/x/app.py")],
              {"input_tokens": 5, "cache_read_input_tokens": 51000,
               "cache_creation_input_tokens": 0, "output_tokens": 8}),
        _result(uuid="r2", filePath="/x/app.py",
                structuredPatch=[{"lines": ["+new line", "-old line", " ctx"]}],
                interrupted=False),
        # an interrupted Bash result in turn 1
        _result(uuid="r3", stdout="", stderr="", interrupted=True),
        # --- turn 2 (correction cue -> re_prompt)
        {"type": "user", "uuid": "u2", "timestamp": "2026-06-28T10:02:00Z",
         "message": {"role": "user", "content": "no, actually do it differently"}},
        _asst("a2", "2026-06-28T10:02:02Z",
              [_tool("Write", file_path="/x/README.md"),
               _tool("Skill", skill="verify"),
               _tool("Edit", file_path="/x/other.py")],
              {"input_tokens": 200, "cache_read_input_tokens": 150000,
               "cache_creation_input_tokens": 2000, "output_tokens": 12},
              stop="max_tokens"),
        _result(uuid="r4", filePath="/x/README.md",
                structuredPatch=[{"lines": ["+# Title", "+hello world docs"]}]),
        _result(uuid="r5", filePath="/x/other.py",
                structuredPatch=[{"lines": ["+x", "-y"]}], interrupted=False),
    ]
    with open(path, "w") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")


class TestEnvelope(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.tpath = Path(self.tmp) / "t.jsonl"
        _transcript(self.tpath)

    def test_extract_and_aggregate(self):
        env = signals.aggregate(list(signals.extract_bundles(str(self.tpath)).values()))
        self.assertEqual(env["lines_added"], 4)        # app +1, readme +2, other +1
        self.assertEqual(env["lines_removed"], 2)      # app -1, other -1
        self.assertEqual(env["doc_words"], 5)          # "# Title"(2) + "hello world docs"(3)
        self.assertEqual(env["files_touched"], 3)
        self.assertEqual(env["permission_mode"], "plan")
        self.assertEqual(env["subagents_used"], "Explore")
        self.assertEqual(env["skills_used"], "verify")
        self.assertEqual(env["mcp_tools_used"], "playwright")
        self.assertEqual(env["interrupts"], 1)
        self.assertEqual(env["re_prompts"], 1)
        self.assertEqual(env["edits_without_read"], 1)  # other.py never read
        self.assertEqual(env["reasoning_loops"], 1)     # app.py read 3x
        self.assertEqual(env["premature_stops"], 1)     # max_tokens
        self.assertEqual(env["peak_context_pct"], 76.1)  # 152200 / 200000

    def test_envelope_persisted_on_finalize(self):
        payload = {"session_id": "s1", "transcript_path": str(self.tpath),
                   "cwd": "/x/proj"}
        ingest.on_session_start(payload, self.tmp)
        ingest.on_stop(payload, self.tmp)
        ingest.on_session_end(payload, self.tmp)
        c = db.connect(self.tmp)
        row = c.execute(
            "SELECT lines_added, lines_removed, doc_words, files_touched, "
            "permission_mode, subagents_used, skills_used, mcp_tools_used, "
            "interrupts, re_prompts, edits_without_read, reasoning_loops, "
            "premature_stops, peak_context_pct FROM runs").fetchone()
        self.assertEqual(
            tuple(row),
            (4, 2, 5, 3, "plan", "Explore", "verify", "playwright",
             1, 1, 1, 1, 1, 76.1))

    def test_scoped_to_run_turns(self):
        # only turn 2 belongs to a (hypothetical) tracked run -> only its signals
        bundles = signals.extract_bundles(str(self.tpath))
        only_t2 = [b for tid, b in bundles.items() if tid == "u2"]
        env = signals.aggregate(only_t2)
        self.assertEqual(env["lines_added"], 3)   # readme +2, other +1 (not app)
        self.assertEqual(env["subagents_used"], None)  # Agent was in turn 1
        self.assertEqual(env["skills_used"], "verify")


if __name__ == "__main__":
    unittest.main()
