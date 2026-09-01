"""Subagent token attribution.

Subagent usage is NOT carried by sidechain records — `isSidechain` is false on
every record of every real Claude Code transcript. It arrives in the `Agent`
tool's `toolUseResult`, which is what these tests exercise.

The regression guarded here is the one that made the old SubagentStop hook
destructive: a subagent finishing mid-turn must never truncate, re-label, or
otherwise disturb the main turn that launched it.

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
import report  # noqa: E402
import transcript as T  # noqa: E402


def _write(path, rows):
    with open(path, "w") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")


def _assistant(uuid, mid, out, ts, tools=0):
    content = [{"type": "text", "text": "ok"}]
    content += [{"type": "tool_use", "name": "Agent", "id": f"t{i}",
                 "input": {"subagent_type": "Explore"}} for i in range(tools)]
    return {"type": "assistant", "uuid": uuid, "timestamp": ts, "effort": "high",
            "message": {"role": "assistant", "id": mid, "model": "claude-opus-5",
                        "content": content,
                        "usage": {"input_tokens": 10, "output_tokens": out,
                                  "cache_read_input_tokens": 0,
                                  "cache_creation_input_tokens": 0}}}


def _agent_result(uuid, agent_id, out, ts, status="completed"):
    """A completed `Agent` tool result — the real carrier of subagent usage."""
    return {"type": "user", "uuid": uuid, "timestamp": ts,
            "message": {"role": "user", "content": [{"type": "tool_result",
                                                     "content": "done"}]},
            "toolUseResult": {
                "status": status, "agentId": agent_id, "agentType": "Explore",
                "resolvedModel": "claude-opus-5[1m]", "prompt": "explore",
                "totalDurationMs": 4000, "totalTokens": out + 15,
                "totalToolUseCount": 7,
                "usage": {"input_tokens": 5, "output_tokens": out,
                          "cache_read_input_tokens": 10,
                          "cache_creation_input_tokens": 0,
                          "cache_creation": {"ephemeral_1h_input_tokens": 0,
                                             "ephemeral_5m_input_tokens": 0}}}}


class TestSubagentAttribution(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.tp = Path(self.tmp) / "main.jsonl"
        # One human prompt. The assistant works, launches a subagent that
        # finishes mid-turn, then keeps working for a long time afterwards.
        _write(self.tp, [
            {"type": "user", "uuid": "u1", "timestamp": "2026-06-28T10:00:00Z",
             "message": {"role": "user", "content": "main task"}},
            _assistant("a1x", "a1", 100, "2026-06-28T10:00:02Z", tools=1),
            _agent_result("r1", "abc123", 50, "2026-06-28T10:00:30Z"),
            _assistant("a2x", "a2", 900, "2026-06-28T10:01:00Z"),
        ])
        self.pl = {"session_id": "S", "transcript_path": str(self.tp),
                   "cwd": "/x/proj"}

    def _cycle(self):
        ingest.on_session_start(self.pl, self.tmp)
        ingest.on_stop(self.pl, self.tmp)
        ingest.on_session_end(self.pl, self.tmp)

    def test_subagent_gets_its_own_row_keyed_on_agent_id(self):
        turns = {t.turn_id: t for t in T.parse_turns(str(self.tp))}
        self.assertIn("agent:abc123", turns)
        sub = turns["agent:abc123"]
        self.assertEqual(sub.query_source, "subagent")
        self.assertFalse(sub.is_prompt)
        self.assertEqual(sub.agent_type, "Explore")
        self.assertEqual(sub.output_tokens, 50)
        self.assertEqual(sub.model, "claude-opus-5[1m]")

    def test_main_turn_keeps_its_full_envelope(self):
        """The bug: a subagent finishing mid-turn used to freeze the main turn,
        losing every token the assistant produced afterwards."""
        turns = {t.turn_id: t for t in T.parse_turns(str(self.tp))}
        main = turns["u1"]
        self.assertEqual(main.query_source, "main")
        self.assertTrue(main.is_prompt)
        self.assertEqual(main.output_tokens, 1000)   # 100 before + 900 after
        self.assertEqual(main.effort, "high")

    def test_no_subagent_stop_hook_exists(self):
        """A SubagentStop hook cannot work: its transcript_path is the main
        transcript, so it can only ever capture a half-finished main turn."""
        self.assertNotIn("SubagentStop", ingest.HANDLERS)

    def test_async_launched_result_carries_no_usage(self):
        p = Path(self.tmp) / "async.jsonl"
        _write(p, [
            {"type": "user", "uuid": "u1", "timestamp": "2026-06-28T10:00:00Z",
             "message": {"role": "user", "content": "go"}},
            _assistant("a1x", "a1", 10, "2026-06-28T10:00:02Z"),
            {"type": "user", "uuid": "r1", "timestamp": "2026-06-28T10:00:03Z",
             "message": {"role": "user", "content": [{"type": "tool_result",
                                                      "content": "launched"}]},
             "toolUseResult": {"status": "async_launched", "agentId": "bg1",
                               "agentType": "Explore"}},
        ])
        ids = {t.turn_id for t in T.parse_turns(str(p))}
        self.assertNotIn("agent:bg1", ids)

    def test_backgrounded_agent_reports_via_task_notification(self):
        p = Path(self.tmp) / "notify.jsonl"
        _write(p, [
            {"type": "user", "uuid": "u1", "timestamp": "2026-06-28T10:00:00Z",
             "message": {"role": "user", "content": "go"}},
            _assistant("a1x", "a1", 10, "2026-06-28T10:00:02Z"),
            {"type": "user", "uuid": "n1", "timestamp": "2026-06-28T10:05:00Z",
             "message": {"role": "user", "content":
                         "<task-notification><task-id>bg9</task-id>"
                         "<usage><subagent_tokens>4242</subagent_tokens>"
                         "<tool_uses>3</tool_uses>"
                         "<duration_ms>900</duration_ms></usage>"
                         "</task-notification>"}},
        ])
        turns = {t.turn_id: t for t in T.parse_turns(str(p))}
        self.assertIn("agent:bg9", turns)
        agg = turns["agent:bg9"]
        self.assertEqual(agg.total_tokens_agg, 4242)
        self.assertEqual(agg.query_source, "subagent")
        # No per-class split is knowable, so it must not be guessed at.
        self.assertEqual(agg.output_tokens, 0)
        # And the notification must not have counted as a human prompt.
        self.assertEqual(sum(1 for t in turns.values()
                             if t.is_prompt and t.query_source == "main"), 1)

    def test_run_totals_include_subagent(self):
        self._cycle()
        c = db.connect(self.tmp)
        run = c.execute(
            "SELECT output_tokens, num_prompts FROM runs").fetchone()
        self.assertEqual(run[0], 1050)   # 1000 main + 50 subagent
        self.assertEqual(run[1], 1)      # one human prompt, not two rows

    def test_overview_breaks_out_query_source(self):
        self._cycle()
        out = report.render_overview_for(self.tmp)
        self.assertIn("## By query source", out)
        self.assertIn("## By subagent", out)
        self.assertIn("Explore", out)


if __name__ == "__main__":
    unittest.main()
