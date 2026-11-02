"""Canonical token-cost model (mirrors the copy in perf-tracker).

Raw token counts are not comparable across approaches, because the four token
classes are billed at very different rates. Summing them — which every report
here used to do — makes a ticket that reuses a long cached prefix look far more
expensive than one that rebuilds its context from scratch, which is backwards.

So every ranking goes through `weighted` tokens: token counts normalised to
*input-equivalent* units using the published billing multipliers.

    input            1.0x   (the unit)
    output           5.0x   (every current model prices output at 5x input)
    cache write 5m   1.25x
    cache write 1h   2.0x
    cache read       0.1x

The output multiplier is model-independent for the whole current lineup, so
weighted tokens are directly comparable across models. The cache-read
multiplier is not: it is 0.1x for most models but lower on the newest ones. So
weighted tokens rank work, and dollars come from `USD_SQL`, which prices every
turn at its own model's per-class rates. Models we don't know are priced as
None and reported as unknown rather than guessed.
"""

from __future__ import annotations

W_INPUT = 1.0
W_OUTPUT = 5.0
W_CACHE_WRITE_5M = 1.25
W_CACHE_WRITE_1H = 2.0
W_CACHE_READ = 0.1

# Per-million-token prices: (input, cache read). Output is 5x input for every
# entry, and cache writes are taken at 1.25x (5m) and 2x (1h) input. Cache reads
# are listed per model because they are NOT a fixed 0.1x: Opus 5.5 reads at
# $0.20 on a $4 input (0.05x) and Fable 5.1 at $0.25 on $10 (0.025x). Pricing a
# dollar figure off weighted tokens alone would overstate those models' cache
# reads two- and fourfold.
PRICES = {
    "claude-fable-5-1": (10.0, 0.25),
    "claude-fable-5": (10.0, 1.0),
    "claude-mythos-5-1": (10.0, 0.25),
    "claude-mythos-5": (10.0, 1.0),
    "claude-opus-5-5": (4.0, 0.20),
    "claude-opus-5": (5.0, 0.50),
    "claude-opus-4-8": (5.0, 0.50),
    "claude-opus-4-7": (5.0, 0.50),
    "claude-opus-4-6": (5.0, 0.50),
    "claude-sonnet-5-5": (2.0, 0.20),
    "claude-sonnet-5": (2.0, 0.20),
    "claude-sonnet-4-6": (3.0, 0.30),
    # Haiku 5.5 doubles its rates past 100K prompt tokens. Per-call prompt size
    # is not kept, so this is the floor.
    "claude-haiku-5-5": (0.10, 0.01),
    "claude-haiku-4-5": (1.0, 0.10),
}

# Kept for callers that only need the input rate.
USD_PER_MTOK_INPUT = {m: p[0] for m, p in PRICES.items()}


def _usd_case() -> str:
    """SQL pricing one `turns` row in dollars by its own model; NULL if unknown.

    Matches the bare id and the context-window variant (`claude-opus-5[1m]`).
    """
    arms = []
    for model, (inp, read) in PRICES.items():
        cost = ("(COALESCE(input_tokens,0) * {i}"
                " + COALESCE(output_tokens,0) * {o}"
                " + COALESCE(cache_read_tokens,0) * {r}"
                " + COALESCE(cache_creation_1h_tokens,0) * {w1h}"
                " + MAX(COALESCE(cache_creation_tokens,0)"
                "       - COALESCE(cache_creation_1h_tokens,0), 0) * {w5m})"
                " / 1000000.0").format(
                    i=inp, o=inp * W_OUTPUT, r=read,
                    w1h=inp * W_CACHE_WRITE_1H, w5m=inp * W_CACHE_WRITE_5M)
        arms.append(f"WHEN model = '{model}' OR model LIKE '{model}[%' "
                    f"THEN {cost}")
    return "(CASE " + " ".join(arms) + " END)"


USD_SQL = _usd_case()

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


def usd_split(model: str | None, input_tokens=0, output_tokens=0,
              cache_read_tokens=0, cache_creation_tokens=0,
              cache_creation_1h_tokens=0) -> float | None:
    """Dollars for one model's token split. Mirrors USD_SQL exactly."""
    price = PRICES.get(base_model(model) or "")
    if price is None:
        return None
    inp, read = price
    write_1h = max(int(cache_creation_1h_tokens or 0), 0)
    write_5m = max(int(cache_creation_tokens or 0) - write_1h, 0)
    return (int(input_tokens or 0) * inp
            + int(output_tokens or 0) * inp * W_OUTPUT
            + int(cache_read_tokens or 0) * read
            + write_1h * inp * W_CACHE_WRITE_1H
            + write_5m * inp * W_CACHE_WRITE_5M) / 1_000_000


def usd(weighted_tokens: float, model: str | None) -> float | None:
    """Dollar estimate for a weighted-token figure, or None for unknown models."""
    price = USD_PER_MTOK_INPUT.get(base_model(model) or "")
    if price is None:
        return None
    return weighted_tokens / 1_000_000 * price
