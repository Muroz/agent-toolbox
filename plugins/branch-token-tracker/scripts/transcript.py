"""Turn + token-usage extraction from a Claude Code transcript JSONL.

Trimmed for this plugin's one job: a turn is a user prompt plus the assistant
messages that answered it, and all we keep is the token envelope. No prompt
text, no sidechain handling, no friction signals.

Two details in here are load-bearing and non-obvious, both learned from the
transcript format rather than chosen:

  * A turn boundary is a `type=user` record that is not `isMeta` and carries no
    `toolUseResult` — tool results are also `type=user` records, and counting
    them would inflate the turn count several-fold.
  * Assistant records are deduped by `message.id`, last occurrence winning. The
    same message id appears more than once as it streams, and each copy repeats
    the cumulative usage; summing them all multiplies the token counts.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class Turn:
    turn_id: str
    started_at: str | None = None
    ended_at: str | None = None
    model: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    num_tool_calls: int = 0
    _msgs: dict = field(default_factory=dict, repr=False)


def parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def duration_ms(start: str | None, end: str | None) -> int | None:
    a, b = parse_iso(start), parse_iso(end)
    if a is None or b is None:
        return None
    if a.tzinfo is None:
        a = a.replace(tzinfo=timezone.utc)
    if b.tzinfo is None:
        b = b.replace(tzinfo=timezone.utc)
    return int((b - a).total_seconds() * 1000)


def _is_user_prompt(rec: dict) -> bool:
    if rec.get("type") != "user" or rec.get("isMeta"):
        return False
    if "toolUseResult" in rec:
        return False
    content = (rec.get("message") or {}).get("content")
    if isinstance(content, str):
        return content.strip() != ""
    if isinstance(content, list) and content:
        first = content[0]
        return isinstance(first, dict) and first.get("type") == "text"
    return False


def _load(path: str) -> list:
    rows = []
    try:
        fh = open(path, "r")
    except OSError:
        return rows
    with fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def parse_turns(path: str) -> list:
    """Parse a transcript into turns with their token envelopes.

    Only turns that received at least one assistant response are returned — an
    unanswered prompt has no tokens to attribute. Sidechain (subagent) records
    are included: their tokens are spent on the ticket just like the main
    thread's, and this plugin has no per-source breakdown to keep them out of.
    """
    turns: list = []
    cur: Turn | None = None

    for rec in _load(path):
        if _is_user_prompt(rec):
            if cur is not None and cur._msgs:
                turns.append(cur)
            cur = Turn(turn_id=rec.get("uuid") or f"turn-{len(turns)}",
                       started_at=rec.get("timestamp"))
        elif rec.get("type") == "assistant" and cur is not None:
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
        turn.cache_creation_tokens += int(
            usage.get("cache_creation_input_tokens") or 0)
        content = msg.get("content")
        if isinstance(content, list):
            turn.num_tool_calls += sum(
                1 for b in content
                if isinstance(b, dict) and b.get("type") == "tool_use")
        ts = rec.get("timestamp")
        if ts and (last_ts is None or ts > last_ts):
            last_ts = ts
        if msg.get("model"):
            last_model = msg["model"]
    turn.ended_at = last_ts
    turn.model = last_model
