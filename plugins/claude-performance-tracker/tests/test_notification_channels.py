"""A backgrounded agent's spend must be found on every channel it arrives on.

An async `Agent` tool result carries no usage at all — only `agentId`, `status`
and an output-file path. The agent's tokens reach the main transcript later, in
a `<task-notification>`, and that notification is the ONLY record of them.

It does not always arrive as a `type=user` record. Scanning only that shape,
which this parser used to do, silently dropped every agent whose notification
came through as an `attachment`. In the real session that exposed it, two of
four agents were lost and 143,035 of 239,102 subagent tokens (60%) went
unrecorded — and it read as a complete total rather than a gap, because the
other two agents were present.

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
    with open(path, "w") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")


def _note(task_id, tokens, tools=3, dur=900):
    return ("<task-notification>\n"
            f"<task-id>{task_id}</task-id>\n"
            "<status>completed</status>\n"
            f"<usage><subagent_tokens>{tokens}</subagent_tokens>"
            f"<tool_uses>{tools}</tool_uses>"
            f"<duration_ms>{dur}</duration_ms></usage>\n"
            "</task-notification>")


# The three shapes the same notification can arrive on, all seen in one real
# transcript. `queue-operation` is the enqueue event; `attachment` is the queued
# command; `type=user` is the delivered message.
def as_user(task_id, tokens, ts, **kw):
    return {"type": "user", "uuid": f"n-{task_id}", "timestamp": ts,
            "message": {"role": "user", "content": _note(task_id, tokens, **kw)}}


def as_attachment(task_id, tokens, ts, **kw):
    return {"type": "attachment", "uuid": f"at-{task_id}", "timestamp": ts,
            "isSidechain": False,
            "attachment": {"type": "queued_command",
                           "prompt": _note(task_id, tokens, **kw)}}


def as_queue_op(task_id, tokens, ts, **kw):
    return {"type": "queue-operation", "operation": "enqueue", "timestamp": ts,
            "content": _note(task_id, tokens, **kw)}


CHANNELS = {"user": as_user, "attachment": as_attachment,
            "queue-operation": as_queue_op}


def _prompt(uuid, ts):
    return {"type": "user", "uuid": uuid, "timestamp": ts,
            "message": {"role": "user", "content": "do the thing"}}


def _assistant(uuid, mid, ts, out=100):
    return {"type": "assistant", "uuid": uuid, "timestamp": ts,
            "message": {"role": "assistant", "id": mid, "model": "claude-opus-5",
                        "content": [{"type": "text", "text": "ok"}],
                        "usage": {"input_tokens": 10, "output_tokens": out,
                                  "cache_read_input_tokens": 0,
                                  "cache_creation_input_tokens": 0}}}


class NotificationChannelTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def parse(self, rows, name="t.jsonl"):
        p = Path(self.tmp.name) / name
        _write(p, rows)
        return {t.turn_id: t for t in T.parse_turns(str(p))}

    def test_every_channel_is_scanned(self):
        """The regression: an attachment-delivered agent must not be dropped."""
        for channel, build in CHANNELS.items():
            with self.subTest(channel=channel):
                turns = self.parse([
                    _prompt("u1", "2026-09-01T10:00:00Z"),
                    _assistant("a1", "m1", "2026-09-01T10:00:02Z"),
                    build("bg1", 4242, "2026-09-01T10:05:00Z"),
                ], name=f"{channel}.jsonl")
                self.assertIn("agent:bg1", turns,
                              f"agent reporting via {channel} was dropped")
                agg = turns["agent:bg1"]
                self.assertEqual(agg.total_tokens_agg, 4242)
                self.assertEqual(agg.query_source, "subagent")
                self.assertEqual(agg.num_tool_calls, 3)
                # No per-class split is knowable, so it must not be invented.
                self.assertEqual(agg.output_tokens, 0)

    def test_notification_is_never_a_prompt_on_any_channel(self):
        """Whatever channel it lands on, it is not something a human typed."""
        for channel, build in CHANNELS.items():
            with self.subTest(channel=channel):
                turns = self.parse([
                    _prompt("u1", "2026-09-01T10:00:00Z"),
                    _assistant("a1", "m1", "2026-09-01T10:00:02Z"),
                    build("bg1", 10, "2026-09-01T10:05:00Z"),
                ], name=f"p-{channel}.jsonl")
                self.assertEqual(
                    sum(1 for t in turns.values()
                        if t.is_prompt and t.query_source == "main"), 1)

    def test_same_agent_on_all_channels_yields_one_row(self):
        """The enqueue, the queued command and the delivery are one event."""
        turns = self.parse([
            _prompt("u1", "2026-09-01T10:00:00Z"),
            _assistant("a1", "m1", "2026-09-01T10:00:02Z"),
            as_queue_op("bg1", 5000, "2026-09-01T10:05:00Z"),
            as_attachment("bg1", 5000, "2026-09-01T10:05:01Z"),
            as_user("bg1", 5000, "2026-09-01T10:05:02Z"),
        ])
        subs = [t for t in turns.values() if t.query_source == "subagent"]
        self.assertEqual(len(subs), 1, "the same agent was counted more than once")
        self.assertEqual(subs[0].total_tokens_agg, 5000)

    def test_repeat_notification_keeps_the_largest_total(self):
        """`<subagent_tokens>` is cumulative, and a resumed agent notifies again.

        First-wins would freeze the agent at its first stop and lose every token
        it spent after being resumed.
        """
        turns = self.parse([
            _prompt("u1", "2026-09-01T10:00:00Z"),
            as_user("bg1", 1000, "2026-09-01T10:05:00Z"),
            as_attachment("bg1", 7500, "2026-09-01T10:20:00Z"),
        ])
        self.assertEqual(turns["agent:bg1"].total_tokens_agg, 7500)

    def test_out_of_order_repeat_does_not_shrink_the_total(self):
        turns = self.parse([
            _prompt("u1", "2026-09-01T10:00:00Z"),
            as_attachment("bg1", 7500, "2026-09-01T10:20:00Z"),
            as_user("bg1", 1000, "2026-09-01T10:05:00Z"),
        ])
        self.assertEqual(turns["agent:bg1"].total_tokens_agg, 7500)

    def test_rich_envelope_always_beats_an_aggregate(self):
        """A real per-class split is strictly better than a bare total."""
        rich = {"type": "user", "uuid": "r1", "timestamp": "2026-09-01T10:06:00Z",
                "message": {"role": "user",
                            "content": [{"type": "tool_result", "content": "done"}]},
                "toolUseResult": {"status": "completed", "agentId": "bg1",
                                  "agentType": "Explore",
                                  "resolvedModel": "claude-opus-5",
                                  "totalToolUseCount": 9,
                                  "usage": {"input_tokens": 5, "output_tokens": 600,
                                            "cache_read_input_tokens": 70,
                                            "cache_creation_input_tokens": 8}}}
        for order, rows in {
            "aggregate first": [as_attachment("bg1", 99999, "2026-09-01T10:05:00Z"), rich],
            "rich first": [rich, as_attachment("bg1", 99999, "2026-09-01T10:05:00Z")],
        }.items():
            with self.subTest(order=order):
                turns = self.parse([_prompt("u1", "2026-09-01T10:00:00Z")] + rows,
                                   name=f"{order.replace(' ','-')}.jsonl")
                t = turns["agent:bg1"]
                self.assertEqual(t.output_tokens, 600)
                self.assertEqual(t.input_tokens, 5)
                # A huge aggregate must not displace the real split, nor be
                # added to it — that would double-count the same work.
                self.assertEqual(t.total_tokens_agg, 0)

    def test_the_incident_shape(self):
        """Four agents, two delivered as `user`, two as `attachment`.

        Reproduces the session that exposed this: before the fix only the two
        `user` ones were stored, understating subagent spend by 60%.
        """
        rows = [_prompt("u1", "2026-09-01T10:00:00Z"),
                _assistant("a1", "m1", "2026-09-01T10:00:02Z")]
        expected = {"ag1": 52365, "ag2": 91334, "ag3": 51701, "ag4": 43702}
        # interleaved exactly as the real transcript had them
        rows += [as_queue_op("ag1", 52365, "2026-09-01T10:01:00Z"),
                 as_user("ag1", 52365, "2026-09-01T10:01:01Z"),
                 as_queue_op("ag2", 91334, "2026-09-01T10:02:00Z"),
                 as_attachment("ag2", 91334, "2026-09-01T10:02:01Z"),
                 as_queue_op("ag3", 51701, "2026-09-01T10:03:00Z"),
                 as_attachment("ag3", 51701, "2026-09-01T10:03:01Z"),
                 as_queue_op("ag4", 43702, "2026-09-01T10:04:00Z"),
                 as_user("ag4", 43702, "2026-09-01T10:04:01Z")]
        turns = self.parse(rows)
        subs = {t.turn_id: t for t in turns.values() if t.query_source == "subagent"}
        self.assertEqual(len(subs), 4, "not every agent was captured")
        for aid, tok in expected.items():
            self.assertEqual(subs[f"agent:{aid}"].total_tokens_agg, tok)
        self.assertEqual(sum(t.total_tokens_agg for t in subs.values()), 239102)
        # and the single human prompt is still the only prompt
        self.assertEqual(sum(1 for t in turns.values()
                             if t.is_prompt and t.query_source == "main"), 1)

    def test_unrelated_records_are_ignored(self):
        """The scan must not misread ordinary records as notifications."""
        turns = self.parse([
            _prompt("u1", "2026-09-01T10:00:00Z"),
            _assistant("a1", "m1", "2026-09-01T10:00:02Z"),
            {"type": "attachment", "uuid": "at0", "timestamp": "2026-09-01T10:00:03Z",
             "attachment": {"type": "queued_command", "prompt": "just a queued prompt"}},
            {"type": "queue-operation", "operation": "enqueue",
             "timestamp": "2026-09-01T10:00:04Z", "content": "nothing to see"},
            # a notification without a token count is not a spend record
            {"type": "attachment", "uuid": "at1", "timestamp": "2026-09-01T10:00:05Z",
             "attachment": {"type": "queued_command",
                            "prompt": "<task-notification><task-id>x</task-id>"
                                      "<status>running</status></task-notification>"}},
        ])
        self.assertEqual([t for t in turns.values() if t.query_source == "subagent"], [])


if __name__ == "__main__":
    unittest.main()
