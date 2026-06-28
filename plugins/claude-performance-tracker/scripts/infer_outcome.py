"""Heuristic outcome inference for passive runs.

Passive runs carry no self-reported outcome, so we infer one from observable
signals and ALWAYS record `outcome_source='inferred'` plus the signals that
produced the label (stored as JSON in runs.inferred_signals). Inferred labels
are never blended with self-reported truth in headline rankings, and `unknown`
is the honest fallback when signals are ambiguous.

This is a SCAFFOLD; the signal→label mapping is tracked as an issue and is meant
to be tunable and auditable.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class OutcomeSignals:
    late_interrupts: int = 0
    re_prompts: int = 0
    negative_sentiment_hits: int = 0
    explicit_redo: bool = False
    clean_close: bool = False
    topic_change_after: bool = False
    extra: dict = field(default_factory=dict)


def infer(signals: OutcomeSignals) -> tuple[str, dict]:
    """Return (label, signals_dict). label in {success, partial, failed, unknown}.

    TODO(issue): implement the mapping, e.g.
        failed  <- late_interrupts + (negative_sentiment or explicit_redo)
        partial <- re_prompts then continuation, no clean close
        success <- clean_close and no late_interrupts (or topic_change_after)
        unknown <- otherwise
    """
    raise NotImplementedError
