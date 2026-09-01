"""A subagent's real envelope comes from its own transcript.

An async `Agent` tool result carries no usage, and the `<task-notification>`
that follows carries one bare number with no per-class split — which measured
against the agent's own log turns out to be roughly the non-cached tokens,
around 40% of its real weighted cost. Since output bills 5x input and cache
writes 1.25x, a bare total cannot be weighted and so was left out of cost
entirely, making agent-heavy work look cheaper than it was.

The agent's own transcript sits beside the session's and has the full envelope:

    <projects>/<slug>/<session-id>.jsonl
    <projects>/<slug>/<session-id>/subagents/agent-<id>.jsonl

    python3 -m unittest discover -s tests
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import transcript as T  # noqa: E402


def _write(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")


def _note(task_id, tokens):
    return ("<task-notification>\n"
            f"<task-id>{task_id}</task-id>\n"
            f"<usage><subagent_tokens>{tokens}</subagent_tokens>"
            "<tool_uses>2</tool_uses></usage>\n</task-notification>")


def _sub_assistant(mid, ts, *, out, cread, cwrite, cwrite_1h=0, tools=0):
    content = [{"type": "text", "text": "working"}]
    content += [{"type": "tool_use", "name": "Read", "id": f"{mid}-t{i}",
                 "input": {}} for i in range(tools)]
    return {"type": "assistant", "uuid": f"u-{mid}", "timestamp": ts,
            "isSidechain": True, "effort": "high",
            "message": {"role": "assistant", "id": mid, "model": "claude-opus-5",
                        "content": content,
                        "usage": {"input_tokens": 2, "output_tokens": out,
                                  "cache_read_input_tokens": cread,
                                  "cache_creation_input_tokens": cwrite,
                                  "cache_creation": {
                                      "ephemeral_5m_input_tokens": cwrite - cwrite_1h,
                                      "ephemeral_1h_input_tokens": cwrite_1h}}}}


class SubagentLogTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.main = root / "sess.jsonl"
        self.subdir = root / "sess" / "subagents"

    def _main_transcript(self, extra=()):
        _write(self.main, [
            {"type": "user", "uuid": "u1", "timestamp": "2026-09-01T10:00:00Z",
             "message": {"role": "user", "content": "go"}},
            {"type": "assistant", "uuid": "a1", "timestamp": "2026-09-01T10:00:02Z",
             "message": {"role": "assistant", "id": "m1", "model": "claude-opus-5",
                         "content": [{"type": "text", "text": "ok"}],
                         "usage": {"input_tokens": 5, "output_tokens": 50,
                                   "cache_read_input_tokens": 100,
                                   "cache_creation_input_tokens": 10}}},
        ] + list(extra))

    def _write_agent_log(self, agent_id, calls, meta=None):
        _write(self.subdir / f"agent-{agent_id}.jsonl", calls)
        if meta is not None:
            (self.subdir / f"agent-{agent_id}.meta.json").write_text(json.dumps(meta))

    def _agents(self):
        return {t.turn_id: t for t in T.parse_turns(str(self.main))
                if t.query_source == "subagent"}

    def test_own_log_supersedes_the_aggregate(self):
        self._main_transcript([
            {"type": "attachment", "uuid": "at1", "timestamp": "2026-09-01T10:05:00Z",
             "attachment": {"type": "queued_command", "prompt": _note("bg1", 9999)}}])
        self._write_agent_log("bg1", [
            _sub_assistant("s1", "2026-09-01T10:01:00Z", out=100, cread=0,
                           cwrite=5000, tools=2),
            _sub_assistant("s2", "2026-09-01T10:02:00Z", out=200, cread=5000,
                           cwrite=1000, tools=1),
        ], meta={"agentType": "Explore", "spawnDepth": 1})

        t = self._agents()["agent:bg1"]
        self.assertEqual(t.output_tokens, 300)
        self.assertEqual(t.cache_read_tokens, 5000)
        self.assertEqual(t.cache_creation_tokens, 6000)
        self.assertEqual(t.num_tool_calls, 3)
        self.assertEqual(t.agent_type, "Explore")
        self.assertEqual(t.model, "claude-opus-5")
        # The aggregate must be cleared: keeping it beside the split would
        # count the same agent twice wherever the two are summed.
        self.assertEqual(t.total_tokens_agg, 0)

    def test_the_aggregate_understates_the_real_cost(self):
        """Not a rounding difference — the reason this file exists."""
        self._main_transcript([
            {"type": "attachment", "uuid": "at1", "timestamp": "2026-09-01T10:05:00Z",
             "attachment": {"type": "queued_command", "prompt": _note("bg1", 5300)}}])
        self._write_agent_log("bg1", [
            _sub_assistant("s1", "2026-09-01T10:01:00Z", out=300, cread=50000,
                           cwrite=5000),
        ])
        t = self._agents()["agent:bg1"]
        raw = (t.input_tokens + t.output_tokens + t.cache_read_tokens
               + t.cache_creation_tokens)
        self.assertGreater(raw, 5300 * 5)

    def test_1h_cache_split_is_preserved(self):
        self._main_transcript([
            {"type": "attachment", "uuid": "at1", "timestamp": "2026-09-01T10:05:00Z",
             "attachment": {"type": "queued_command", "prompt": _note("bg1", 10)}}])
        self._write_agent_log("bg1", [
            _sub_assistant("s1", "2026-09-01T10:01:00Z", out=10, cread=0,
                           cwrite=8000, cwrite_1h=3000)])
        t = self._agents()["agent:bg1"]
        self.assertEqual(t.cache_creation_tokens, 8000)
        self.assertEqual(t.cache_creation_1h_tokens, 3000)

    def test_duplicate_streamed_records_are_counted_once(self):
        """Same rule as a main turn: dedupe by message.id."""
        self._main_transcript([
            {"type": "attachment", "uuid": "at1", "timestamp": "2026-09-01T10:05:00Z",
             "attachment": {"type": "queued_command", "prompt": _note("bg1", 10)}}])
        call = _sub_assistant("s1", "2026-09-01T10:01:00Z", out=400, cread=900,
                              cwrite=70)
        self._write_agent_log("bg1", [call, call, call])
        t = self._agents()["agent:bg1"]
        self.assertEqual(t.output_tokens, 400)

    def test_missing_log_falls_back_to_the_aggregate(self):
        """A deleted or never-written log must not lose the agent entirely."""
        self._main_transcript([
            {"type": "attachment", "uuid": "at1", "timestamp": "2026-09-01T10:05:00Z",
             "attachment": {"type": "queued_command", "prompt": _note("bg1", 4242)}}])
        t = self._agents()["agent:bg1"]
        self.assertEqual(t.total_tokens_agg, 4242)
        self.assertEqual(t.output_tokens, 0)

    def test_empty_log_falls_back_to_the_aggregate(self):
        self._main_transcript([
            {"type": "attachment", "uuid": "at1", "timestamp": "2026-09-01T10:05:00Z",
             "attachment": {"type": "queued_command", "prompt": _note("bg1", 4242)}}])
        _write(self.subdir / "agent-bg1.jsonl", [])
        self.assertEqual(self._agents()["agent:bg1"].total_tokens_agg, 4242)

    def test_main_turn_is_untouched_by_the_upgrade(self):
        self._main_transcript([
            {"type": "attachment", "uuid": "at1", "timestamp": "2026-09-01T10:05:00Z",
             "attachment": {"type": "queued_command", "prompt": _note("bg1", 10)}}])
        self._write_agent_log("bg1", [
            _sub_assistant("s1", "2026-09-01T10:01:00Z", out=9999, cread=9999,
                           cwrite=9999)])
        main = [t for t in T.parse_turns(str(self.main))
                if t.query_source == "main"]
        self.assertEqual(len(main), 1)
        self.assertEqual(main[0].output_tokens, 50)
        self.assertEqual(main[0].cache_read_tokens, 100)

    def test_agent_active_time_comes_from_its_own_stamps(self):
        self._main_transcript([
            {"type": "attachment", "uuid": "at1", "timestamp": "2026-09-01T10:05:00Z",
             "attachment": {"type": "queued_command", "prompt": _note("bg1", 10)}}])
        self._write_agent_log("bg1", [
            _sub_assistant("s1", "2026-09-01T10:01:00Z", out=1, cread=0, cwrite=1),
            _sub_assistant("s2", "2026-09-01T10:01:30Z", out=1, cread=0, cwrite=1)])
        t = self._agents()["agent:bg1"]
        self.assertEqual(t.active_ms, 30_000)
        self.assertEqual(t.started_at, "2026-09-01T10:01:00Z")

    def test_an_agent_with_a_log_but_no_notification_is_still_counted(self):
        """Capture must not be hostage to the notification arriving.

        An entire delivery channel once went unnoticed; an agent that left a log
        spent tokens whether or not the main transcript ever mentioned it.
        """
        self._main_transcript()  # no notification at all
        self._write_agent_log("ghost", [
            _sub_assistant("s1", "2026-09-01T10:01:00Z", out=250, cread=800,
                           cwrite=60, tools=2)], meta={"agentType": "Plan"})
        t = self._agents()["agent:ghost"]
        self.assertEqual(t.output_tokens, 250)
        self.assertEqual(t.agent_type, "Plan")
        self.assertEqual(t.query_source, "subagent")
        self.assertEqual(t.is_prompt, False)

    def test_discovery_does_not_invent_agents(self):
        self._main_transcript()
        self.subdir.mkdir(parents=True, exist_ok=True)
        (self.subdir / "notes.txt").write_text("not a log")
        (self.subdir / "agent-x.meta.json").write_text("{}")
        self.assertEqual(self._agents(), {})


if __name__ == "__main__":
    unittest.main()
