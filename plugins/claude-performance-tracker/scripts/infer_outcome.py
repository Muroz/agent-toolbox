"""Heuristic outcome inference for passive runs.

Passive runs carry no self-reported outcome, so we infer one from deterministic,
observable signals and ALWAYS record `outcome_source='inferred'` plus the signals
that produced the label (stored as JSON in runs.inferred_signals). Inferred labels
are never blended with self-reported truth in the headline rankings (the compare
view filters to outcome_source='self_report'), and `unknown` is the honest
fallback when signals are ambiguous.

Signals (all deterministic):
  * n_prompts      — number of main user prompts in the run.
  * interrupts     — toolUseResult.interrupted count (from the envelope).
  * re_prompts     — correction-cue prompts (from the envelope).
  * positive_cues  — user prompts containing approval language.
  * negative_cues  — user prompts containing failure language.
  * produced       — whether the run produced output (tokens or LOC).

Decision order (documented, tunable):
  1. negative cue, or interrupts >= max(2, n_prompts)        -> failed
  2. positive cue and no interrupts                          -> success
  3. no prompts at all                                       -> unknown
  4. produced output with no re-prompts and no interrupts    -> success
  5. any re-prompts or interrupts                            -> partial
  6. otherwise                                               -> unknown
"""

from __future__ import annotations

import json
import re
import sqlite3

POSITIVE = re.compile(
    r"\b(thanks|thank you|perfect|lgtm|looks good|that works|works now|"
    r"ship it|great work|nice work|awesome)\b", re.IGNORECASE)
NEGATIVE = re.compile(
    r"(does(n'?t| not) work|did(n'?t| not) work|still (broken|failing|wrong|"
    r"not working)|not working|that failed|that'?s wrong|revert that|undo that|"
    r"you broke)", re.IGNORECASE)


def compute_signals(conn: sqlite3.Connection, run_id: str) -> dict:
    prompts = [r[0] or "" for r in conn.execute(
        "SELECT prompt_text FROM turns WHERE run_id = ? AND query_source = 'main' "
        "ORDER BY seq", (run_id,))]
    row = conn.execute(
        "SELECT interrupts, re_prompts, output_tokens, lines_added "
        "FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    interrupts, re_p, out_tok, lines = row if row else (0, 0, 0, 0)
    return {
        "n_prompts": len(prompts),
        "interrupts": interrupts or 0,
        "re_prompts": re_p or 0,
        "positive_cues": sum(1 for p in prompts if POSITIVE.search(p)),
        "negative_cues": sum(1 for p in prompts if NEGATIVE.search(p)),
        "produced": bool((out_tok or 0) > 0 or (lines or 0) > 0),
    }


def infer(s: dict) -> str:
    n = s["n_prompts"]
    if s["negative_cues"] > 0 or (n > 0 and s["interrupts"] >= max(2, n)):
        return "failed"
    if s["positive_cues"] > 0 and s["interrupts"] == 0:
        return "success"
    if n == 0:
        return "unknown"
    if s["produced"] and s["re_prompts"] == 0 and s["interrupts"] == 0:
        return "success"
    if s["re_prompts"] > 0 or s["interrupts"] > 0:
        return "partial"
    return "unknown"


def infer_and_store(conn: sqlite3.Connection, run_id: str) -> str | None:
    """Infer and persist an outcome for a passive run that has no self-reported
    outcome. Returns the inferred label, or None if not applicable."""
    row = conn.execute(
        "SELECT capture_mode, outcome_source FROM runs WHERE run_id = ?",
        (run_id,)).fetchone()
    if not row or row[0] != "passive" or row[1] == "self_report":
        return None
    signals = compute_signals(conn, run_id)
    label = infer(signals)
    conn.execute(
        "UPDATE runs SET outcome = ?, outcome_source = 'inferred', "
        "inferred_signals = ? WHERE run_id = ?",
        (label, json.dumps(signals), run_id))
    conn.commit()
    return label
