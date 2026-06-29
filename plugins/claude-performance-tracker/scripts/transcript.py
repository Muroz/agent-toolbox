"""Transcript parsing for claude-performance-tracker.

Turns a Claude Code session JSONL transcript into a list of `Turn`s. This is the
single reusable parser; later slices extend `Turn` with output/friction/context
signals, but the turn-boundary and token-envelope logic lives here.

Key facts about the transcript format this relies on:
  * Assistant lines duplicate — the same logical message appears multiple times
    with the same `message.id`. Token usage must be counted ONCE per distinct
    `message.id`, not per line.
  * A real user prompt (a turn boundary) is a `type=user` line that is NOT meta
    (`isMeta`) and NOT a tool result. Tool results and injected/meta lines are
    not prompts.
  * There is no per-turn id in the transcript, so a turn is keyed on the user
    prompt's `uuid`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class Turn:
    turn_id: str
    seq: int
    started_at: str | None
    ended_at: str | None
    model: str | None
    query_source: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_creation_tokens: int
    num_tool_calls: int
    prompt_text: str
    _msgs: dict = field(default_factory=dict, repr=False)


def parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def _is_user_prompt(rec: dict) -> bool:
    if rec.get("type") != "user" or rec.get("isMeta"):
        return False
    if rec.get("toolUseResult") is not None or "toolUseResult" in rec:
        return False
    content = (rec.get("message") or {}).get("content")
    if isinstance(content, str):
        return content.strip() != ""
    if isinstance(content, list) and content:
        first = content[0]
        return isinstance(first, dict) and first.get("type") == "text"
    return False


def _prompt_text(rec: dict) -> str:
    content = (rec.get("message") or {}).get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            b.get("text", "") for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        )
    return ""


def _load(path: str) -> list[dict]:
    rows = []
    with open(path, "r") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def parse_turns(path: str, include_sidechain: bool = False) -> list[Turn]:
    """Parse a transcript into turns with their token envelopes.

    Only turns that received at least one assistant response are returned.
    `include_sidechain` must be True when parsing a subagent's own transcript
    (whose lines are all sidechain); it stays False for a main transcript so any
    embedded subagent lines are not double-counted.
    """
    rows = _load(path)
    turns: list[Turn] = []
    cur: Turn | None = None

    for rec in rows:
        if _is_user_prompt(rec):
            if cur is not None and cur._msgs:
                turns.append(cur)
            cur = Turn(
                turn_id=rec.get("uuid") or f"turn-{len(turns)}",
                seq=len(turns),
                started_at=rec.get("timestamp"),
                ended_at=None,
                model=None,
                query_source="main",
                input_tokens=0, output_tokens=0,
                cache_read_tokens=0, cache_creation_tokens=0,
                num_tool_calls=0,
                prompt_text=_prompt_text(rec),
            )
        elif (
            rec.get("type") == "assistant"
            and cur is not None
            and (include_sidechain or not rec.get("isSidechain"))
        ):
            mid = (rec.get("message") or {}).get("id") or rec.get("uuid")
            if mid:
                cur._msgs[mid] = rec  # last occurrence per message.id wins

    if cur is not None and cur._msgs:
        turns.append(cur)

    for turn in turns:
        _finalize(turn)
    return turns


def _finalize(turn: Turn) -> None:
    last_ts = None
    last_model = None
    for rec in turn._msgs.values():
        msg = rec.get("message") or {}
        usage = msg.get("usage") or {}
        turn.input_tokens += int(usage.get("input_tokens") or 0)
        turn.output_tokens += int(usage.get("output_tokens") or 0)
        turn.cache_read_tokens += int(usage.get("cache_read_input_tokens") or 0)
        turn.cache_creation_tokens += int(usage.get("cache_creation_input_tokens") or 0)
        content = msg.get("content")
        if isinstance(content, list):
            turn.num_tool_calls += sum(
                1 for b in content
                if isinstance(b, dict) and b.get("type") == "tool_use"
            )
        ts = rec.get("timestamp")
        if ts and (last_ts is None or ts > last_ts):
            last_ts = ts
        if msg.get("model"):
            last_model = msg["model"]
    turn.ended_at = last_ts
    turn.model = last_model


def duration_ms(start: str | None, end: str | None) -> int | None:
    a, b = parse_iso(start), parse_iso(end)
    if a is None or b is None:
        return None
    if a.tzinfo is None:
        a = a.replace(tzinfo=timezone.utc)
    if b.tzinfo is None:
        b = b.replace(tzinfo=timezone.utc)
    return int((b - a).total_seconds() * 1000)
