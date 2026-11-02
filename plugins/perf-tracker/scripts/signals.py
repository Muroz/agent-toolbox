"""Deterministic envelope derivation from a session transcript.

Extends the raw token envelope with the approach descriptor, tangible output,
friction signals and context-window pressure. Everything here is a *deterministic*
function of the transcript — the fuzzy, judgement-heavy scoring is the
usage-evaluator subagent's job (a later slice), not this module's.

Signals are extracted per turn, then aggregated over the turns that belong to a
run, so a passive run and a tracked run that share a session get distinct
envelopes.

Signal definitions (documented on purpose — these are proxies):
  * lines_added / lines_removed — summed from `toolUseResult.structuredPatch`
    of Edit/Write/MultiEdit (+/- lines); a new-file Write with no patch counts
    its content lines as added.
  * files_touched — distinct file paths edited/written.
  * doc_words — words added to doc files (.md/.mdx/.markdown/.txt/.rst).
  * permission_mode — distinct `permissionMode`s seen (reflects mixed-mode runs).
  * models — distinct assistant models (already aggregated from turns).
  * subagents_used — distinct `subagent_type` of Agent/Task tool calls.
  * skills_used — distinct Skill `skill` inputs plus slash `/command` names.
  * mcp_tools_used — distinct MCP servers invoked (the `mcp__<server>__...` prefix).
  * interrupts — count of Claude Code's `[Request interrupted ...]` marker. (It
    used to look for `toolUseResult.interrupted`, a field that appears in no real
    transcript, so the signal read zero on every run ever recorded.)
  * edits_without_read — edits to a path not previously read/edited in the run
    (ordered replay across the run's turns).
  * reasoning_loops — count of files read 3+ times in the run (re-reading proxy).
  * premature_stops — assistant responses whose `stop_reason == "max_tokens"`.
  * re_prompts — user prompts (after the first) that open with a correction cue.
  * peak_context_tokens — max (input + cache_read + cache_creation) tokens of any
    response. This is the raw, assumption-free figure.
  * peak_context_pct — the above against a context window the transcript never
    records. Set $CPT_CONTEXT_WINDOW to state it; otherwise we assume the 200k
    default tier and escalate to 1M only when the observed peak exceeds it, so
    the number is never above 100% but IS an assumption. Prefer the raw tokens.
  * compact_count — compaction events; clear_count — `/clear` commands.

A note on which bundles belong to a run: a prompt that never got an assistant
reply (`/clear` is the common case) produces no row in `turns`, so filtering
bundles by "turn_id is one of this run's turns" silently dropped every one of
them — which is why clear_count was structurally stuck at zero. Unowned bundles
are now carried forward to the run of the nearest preceding owned bundle.
"""

from __future__ import annotations

import json
import os
import re
from collections import Counter

import transcript as T

DOC_EXTS = (".md", ".mdx", ".markdown", ".txt", ".rst")
WINDOW_STD = 200_000
WINDOW_EXTENDED = 1_000_000


def context_window(max_ctx: int) -> int:
    """The window to measure `max_ctx` against.

    Claude Code does not record its context window anywhere in the transcript and
    `message.model` never carries the `[1m]` suffix, so this cannot be detected —
    only assumed or configured. $CPT_CONTEXT_WINDOW states it explicitly.
    """
    override = os.environ.get("CPT_CONTEXT_WINDOW")
    if override:
        try:
            return max(int(override), 1)
        except ValueError:
            pass
    return WINDOW_EXTENDED if max_ctx > WINDOW_STD else WINDOW_STD
CORRECTION_CUE = re.compile(
    r"^\s*(no[,.\s]|actually\b|instead\b|that'?s wrong|undo\b|revert\b|"
    r"stop\b|wait[,.\s]|nope\b)", re.IGNORECASE)


def _is_doc(path: str) -> bool:
    return bool(path) and path.lower().endswith(DOC_EXTS)


def _patch_counts(patch, doc: bool):
    """(added, removed, doc_words_added) from a structuredPatch list."""
    added = removed = words = 0
    for hunk in patch or []:
        for ln in (hunk.get("lines") or []):
            if ln.startswith("+"):
                added += 1
                if doc:
                    words += len(ln[1:].split())
            elif ln.startswith("-"):
                removed += 1
    return added, removed, words


class _Bundle:
    __slots__ = ("seq", "prompt", "lines_added", "lines_removed", "doc_words",
                 "files", "subagents", "skills", "mcp", "perm_modes",
                 "interrupts", "clears", "compacts", "premature",
                 "max_ctx", "events")

    def __init__(self, seq, prompt):
        self.seq = seq
        self.prompt = prompt
        self.lines_added = self.lines_removed = self.doc_words = 0
        self.interrupts = self.clears = self.compacts = self.premature = 0
        self.max_ctx = 0
        self.files = set()
        self.subagents, self.skills, self.mcp, self.perm_modes = set(), set(), set(), set()
        self.events = []  # ordered ('read'|'edit', path)


def _is_prompt(rec: dict) -> bool:
    """A real human prompt — the same test the turn parser uses, so a bundle's
    key always matches the `turns.turn_id` it belongs to."""
    return T._is_user_record(rec) and not T.is_synthetic_prompt(T.message_text(rec))


def extract_bundles(transcript_path: str) -> dict:
    """Map turn_id -> _Bundle of per-turn deterministic signals."""
    bundles: dict = {}
    cur = None
    with open(transcript_path) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            t = rec.get("type")

            if _is_prompt(rec):
                tid = rec.get("uuid") or f"turn-{len(bundles)}"
                content = (rec.get("message") or {}).get("content")
                text = content if isinstance(content, str) else "\n".join(
                    b.get("text", "") for b in content
                    if isinstance(b, dict) and b.get("type") == "text")
                cur = _Bundle(len(bundles), text)
                bundles[tid] = cur
                for cmd in re.findall(r"<command-name>([^<]*)</command-name>", text):
                    cur.skills.add(cmd.strip())
                    if cmd.strip().lstrip("/").startswith("clear"):
                        cur.clears += 1
                continue

            if cur is None:
                continue

            if t == "user" and "toolUseResult" not in rec:
                # Not a prompt (handled above) and not a tool result (handled
                # below) — but Claude Code's interrupt marker arrives as one of
                # these injected user records.
                if T.INTERRUPT_MARKER in T.message_text(rec):
                    cur.interrupts += 1
                continue

            if t in ("permission-mode", "mode"):
                pm = rec.get("permissionMode")
                if pm:
                    cur.perm_modes.add(pm)
                continue

            if rec.get("isCompactSummary") or "compact" in str(t).lower():
                cur.compacts += 1

            msg = rec.get("message") if isinstance(rec.get("message"), dict) else {}
            content = msg.get("content")

            if t == "assistant" and not rec.get("isSidechain"):
                usage = msg.get("usage") or {}
                ctx = (int(usage.get("input_tokens") or 0)
                       + int(usage.get("cache_read_input_tokens") or 0)
                       + int(usage.get("cache_creation_input_tokens") or 0))
                cur.max_ctx = max(cur.max_ctx, ctx)
                if msg.get("stop_reason") == "max_tokens":
                    cur.premature += 1
                if isinstance(content, list):
                    for b in content:
                        if not isinstance(b, dict) or b.get("type") != "tool_use":
                            continue
                        name = b.get("name") or ""
                        inp = b.get("input") or {}
                        if name.startswith("mcp__"):
                            parts = name.split("__")
                            cur.mcp.add(parts[1] if len(parts) > 1 else name)
                        elif name in ("Agent", "Task"):
                            st = inp.get("subagent_type") or inp.get("agentType")
                            if st:
                                cur.subagents.add(st)
                        elif name == "Skill":
                            if inp.get("skill"):
                                cur.skills.add(inp["skill"])
                        elif name == "Read":
                            fp = inp.get("file_path")
                            if fp:
                                cur.events.append(("read", fp))
                        elif name == "Write":
                            # creating/overwriting a file is not a blind edit, but
                            # it does establish context for later edits.
                            fp = inp.get("file_path")
                            if fp:
                                cur.events.append(("write", fp))
                        elif name in ("Edit", "MultiEdit"):
                            fp = inp.get("file_path")
                            if fp:
                                cur.events.append(("edit", fp))

            if T.INTERRUPT_MARKER in T.message_text(rec):
                cur.interrupts += 1

            tur = rec.get("toolUseResult")
            if isinstance(tur, dict):
                # LOC comes solely from structuredPatch (present on Edit/Write/
                # MultiEdit results). Read results also carry filePath+content but
                # no patch, so they never count as output.
                patch = tur.get("structuredPatch")
                fp = tur.get("filePath")
                if fp and patch:
                    cur.files.add(fp)
                    a, r, w = _patch_counts(patch, _is_doc(fp))
                    cur.lines_added += a
                    cur.lines_removed += r
                    cur.doc_words += w
    return bundles


def aggregate(bundles: list) -> dict:
    """Aggregate an ordered list of _Bundle (the run's turns) into envelope fields."""
    env = {
        "lines_added": 0, "lines_removed": 0, "doc_words": 0, "files_touched": 0,
        "interrupts": 0, "re_prompts": 0, "edits_without_read": 0,
        "reasoning_loops": 0, "premature_stops": 0, "compact_count": 0,
        "clear_count": 0, "peak_context_tokens": 0, "peak_context_pct": 0.0,
        "permission_mode": None, "subagents_used": None,
        "skills_used": None, "mcp_tools_used": None,
    }
    files, subs, skills, mcp, perms = set(), set(), set(), set(), set()
    seen, read_counts = set(), Counter()
    max_ctx = 0
    ordered = sorted(bundles, key=lambda b: b.seq)
    for i, b in enumerate(ordered):
        env["lines_added"] += b.lines_added
        env["lines_removed"] += b.lines_removed
        env["doc_words"] += b.doc_words
        env["interrupts"] += b.interrupts
        env["premature_stops"] += b.premature
        env["compact_count"] += b.compacts
        env["clear_count"] += b.clears
        files |= b.files
        subs |= b.subagents
        skills |= b.skills
        mcp |= b.mcp
        perms |= b.perm_modes
        max_ctx = max(max_ctx, b.max_ctx)
        if i > 0 and b.prompt and CORRECTION_CUE.match(b.prompt):
            env["re_prompts"] += 1
        for action, path in b.events:
            if action == "read":
                read_counts[path] += 1
                seen.add(path)
            elif action == "write":
                seen.add(path)  # creating a file establishes context
            else:  # edit / multiedit
                if path not in seen:
                    env["edits_without_read"] += 1
                seen.add(path)
    env["files_touched"] = len(files)
    env["reasoning_loops"] = sum(1 for c in read_counts.values() if c >= 3)
    env["peak_context_tokens"] = max_ctx
    env["peak_context_pct"] = round(max_ctx / context_window(max_ctx) * 100, 1)
    env["permission_mode"] = ",".join(sorted(perms)) or None
    env["subagents_used"] = ",".join(sorted(subs)) or None
    env["skills_used"] = ",".join(sorted(skills)) or None
    env["mcp_tools_used"] = ",".join(sorted(mcp)) or None
    return env


def derive_run_envelope(conn, run_id: str) -> dict | None:
    """Derive the envelope for a run from its sessions' transcripts.

    Bundles whose turn is stored against this run are owned outright. A bundle
    with no stored turn — an unanswered prompt such as `/clear` — is attributed
    to the run of the nearest preceding owned bundle, so its signals are not
    silently discarded. Returns None if no transcript is available.
    """
    turn_ids = {r[0] for r in conn.execute(
        "SELECT turn_id FROM turns WHERE run_id = ?", (run_id,))}
    if not turn_ids:
        return None
    sessions = [r[0] for r in conn.execute(
        "SELECT DISTINCT session_id FROM turns WHERE run_id = ?", (run_id,))]
    run_bundles: list = []
    any_transcript = False
    for sid in sessions:
        row = conn.execute(
            "SELECT transcript_path FROM sessions WHERE session_id = ?", (sid,)
        ).fetchone()
        path = row[0] if row else None
        if not path:
            continue
        try:
            extracted = extract_bundles(path)
        except OSError:
            continue
        any_transcript = True
        # This session's turns may be split across a passive and a tracked run,
        # so resolve ownership per turn rather than assuming the whole session.
        owner = {r[0]: r[1] for r in conn.execute(
            "SELECT turn_id, run_id FROM turns WHERE session_id = ?", (sid,))}
        carried = None
        for tid, bundle in extracted.items():
            resolved = owner.get(tid, carried)
            if tid in owner:
                carried = owner[tid]
            if resolved == run_id:
                run_bundles.append(bundle)
    if not any_transcript:
        return None
    return aggregate(run_bundles)
