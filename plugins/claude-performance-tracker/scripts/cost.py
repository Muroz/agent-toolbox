"""Canonical token-cost model.

Raw token counts are not comparable across approaches, because the four token
classes are billed at very different rates. Summing them — which every report
here used to do — makes a run that reuses a long cached prefix look far more
expensive than one that rebuilds its context from scratch, which is backwards.

So every ranking goes through `weighted` tokens: token counts normalised to
*input-equivalent* units using the published billing multipliers.

    input            1.0x   (the unit)
    output           5.0x   (every current model prices output at 5x input)
    cache write 5m   1.25x
    cache write 1h   2.0x
    cache read       0.1x

The output multiplier is model-independent for the whole current lineup (Opus 5
$5/$25, Opus 4.8 $5/$25, Sonnet 5 $3/$15, Haiku 4.5 $1/$5, Fable 5 $10/$50), so
weighted tokens are directly comparable across models. Converting to money needs
the per-model input price, which is what `USD_PER_MTOK_INPUT` is for; models we
don't know are priced as None and reported as unknown rather than guessed.
"""

from __future__ import annotations

W_INPUT = 1.0
W_OUTPUT = 5.0
W_CACHE_WRITE_5M = 1.25
W_CACHE_WRITE_1H = 2.0
W_CACHE_READ = 0.1

# Input price per million tokens. Output is 5x these for every entry.
USD_PER_MTOK_INPUT = {
    "claude-opus-5": 5.0,
    "claude-opus-4-8": 5.0,
    "claude-opus-4-7": 5.0,
    "claude-opus-4-6": 5.0,
    "claude-sonnet-5": 3.0,
    "claude-sonnet-4-6": 3.0,
    "claude-haiku-4-5": 1.0,
    "claude-fable-5": 10.0,
    "claude-mythos-5": 10.0,
}

# SQL for weighted tokens over a row exposing the token columns. `cache_creation_
# 1h_tokens` is the 1h slice of `cache_creation_tokens`; the remainder is 5m.
WEIGHTED_SQL = (
    "(COALESCE(input_tokens,0) * {i}"
    " + COALESCE(output_tokens,0) * {o}"
    " + COALESCE(cache_read_tokens,0) * {r}"
    " + COALESCE(cache_creation_1h_tokens,0) * {w1h}"
    " + MAX(COALESCE(cache_creation_tokens,0)"
    "       - COALESCE(cache_creation_1h_tokens,0), 0) * {w5m})"
).format(i=W_INPUT, o=W_OUTPUT, r=W_CACHE_READ,
         w1h=W_CACHE_WRITE_1H, w5m=W_CACHE_WRITE_5M)


def base_model(model: str | None) -> str | None:
    """Strip a Claude Code context-window suffix, e.g. `claude-opus-5[1m]`."""
    if not model:
        return None
    return model.split("[", 1)[0].strip() or None


def weighted(input_tokens=0, output_tokens=0, cache_read_tokens=0,
             cache_creation_tokens=0, cache_creation_1h_tokens=0) -> float:
    """Input-equivalent tokens. Mirrors WEIGHTED_SQL exactly."""
    write_1h = max(int(cache_creation_1h_tokens or 0), 0)
    write_5m = max(int(cache_creation_tokens or 0) - write_1h, 0)
    return (int(input_tokens or 0) * W_INPUT
            + int(output_tokens or 0) * W_OUTPUT
            + int(cache_read_tokens or 0) * W_CACHE_READ
            + write_1h * W_CACHE_WRITE_1H
            + write_5m * W_CACHE_WRITE_5M)


def usd(weighted_tokens: float, model: str | None) -> float | None:
    """Dollar estimate for a weighted-token figure, or None for unknown models."""
    price = USD_PER_MTOK_INPUT.get(base_model(model) or "")
    if price is None:
        return None
    return weighted_tokens / 1_000_000 * price
