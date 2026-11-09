"""Token prices, USD per million tokens.

Source: the Claude API reference (bundled `claude-api` skill, cached
2026-06-24), re-checked 2026-09-13. The 4.5 rows were added 2026-09-16 from
platform.claude.com/docs/en/about-claude/pricing, fetched rather than recalled
-- billions of tokens in real use were on models this table had never heard of.
The 5.5 rows came from the same page on 2026-10-18, and the 5.2 rows on
2026-11-09, release day, before which Fable 5.2 fell through to the Fable 5
row (a quarter too much, cache reads at 4x). Until then Opus 5.5 fell
through to the Opus 5 row by prefix (a quarter too much, and cache reads at
2.5x), Sonnet 5.5 read cache at twice its rate, and Haiku 5.5 was unpriced.
This file is the ONLY place rates appear.

Caching, which dominates the bill in a long agent session:

* Cache reads bill at 0.1x the input rate -- except Fable 5.1 and Mythos
  5.1 at 0.025x ($0.25/MTok), Fable 5.2 and Mythos 5.2 at 0.025x ($0.20/MTok), and Opus 5.5 and Sonnet 5.5 at 0.05x ($0.20 and
  $0.10/MTok).
* Cache writes bill at 1.25x for the 5-minute TTL and 2x for the 1-hour TTL.
  Claude Code writes at the 1-HOUR TTL, so assuming 1.25x under-bills real
  sessions by 1.6x. The transcript does record which: `usage.cache_creation`
  splits into `ephemeral_5m_input_tokens` / `ephemeral_1h_input_tokens`.
  `cache_write` here is the total and `cache_write_1h` the subset billed at 2x,
  so a transcript with no breakdown degrades to the API default of 1.25x.

Haiku 5.5 bills each REQUEST by its prompt length: up to 100K tokens (cache
reads and writes included) at the rates below, over that at 5x. Callers hand
this totals across many replies, which cannot say which replies were over, so
Haiku 5.5 is priced at its first tier: a floor, not the figure.

Cost is still an ESTIMATE -- it is not billing data -- and is labelled as one
in the UI.
"""
from __future__ import annotations

CACHE_READ_MULTIPLIER = 0.1
CACHE_WRITE_5M_MULTIPLIER = 1.25
CACHE_WRITE_1H_MULTIPLIER = 2.0

# model id -> (input $/MTok, output $/MTok, context window)
RATES: dict[str, tuple[float, float, int]] = {
    "claude-fable-5-2":  (8.00, 40.00, 1_000_000),
    "claude-mythos-5-2": (8.00, 40.00, 1_000_000),
    "claude-fable-5-1":  (10.00, 50.00, 1_000_000),
    "claude-mythos-5-1": (10.00, 50.00, 1_000_000),
    "claude-fable-5":    (10.00, 50.00, 1_000_000),
    "claude-opus-5-5":   (4.00, 20.00, 1_000_000),
    "claude-opus-5":     (5.00, 25.00, 1_000_000),
    "claude-opus-4-8":   (5.00, 25.00, 1_000_000),
    "claude-opus-4-7":   (5.00, 25.00, 1_000_000),
    "claude-opus-4-6":   (5.00, 25.00, 1_000_000),
    "claude-sonnet-5-5": (2.00, 10.00, 1_000_000),
    "claude-sonnet-5":   (2.00, 10.00, 1_000_000),
    "claude-sonnet-4-6": (3.00, 15.00, 1_000_000),
    # Opus 4.5 and Sonnet 4.5: a large share of real-world tokens were on these
    # two, and RATES once had no row for either -- so cost_usd() returned 0.00
    # and billions of tokens read as free. 200k, not 1M: the 1M window starts
    # at 4.6 ("Claude 4.6 and later models include the full 1M token context
    # window"), and these predate it.
    "claude-opus-4-5":   (5.00, 25.00,   200_000),
    "claude-sonnet-4-5": (3.00, 15.00,   200_000),
    "claude-haiku-5-5":  (0.10,  0.50, 1_000_000),   # the first tier: see the docstring
    "claude-haiku-4-5":  (1.00,  5.00,   200_000),
}

# Models whose cache reads are not the usual 0.1x (the pricing page's
# footnotes 1 and 2).
CACHE_READ_MULTIPLIERS: dict[str, float] = {
    "claude-fable-5-2":  0.025,
    "claude-mythos-5-2": 0.025,
    "claude-fable-5-1":  0.025,
    "claude-mythos-5-1": 0.025,
    "claude-opus-5-5":   0.05,
    "claude-sonnet-5-5": 0.05,
}

DEFAULT_CONTEXT = 1_000_000


def _resolve(model: str) -> str | None:
    """Exact id, else the LONGEST known prefix.

    Claude Code reports suffixed ids like `claude-opus-5[1m]`. Longest-prefix
    matters because `claude-fable-5` is a prefix of `claude-fable-5-1`, which
    reads cache at a different rate.
    """
    if not model:
        return None
    if model in RATES:
        return model
    for known in sorted(RATES, key=len, reverse=True):
        if model.startswith(known):
            return known
    return None


def context_limit(model: str) -> int:
    known = _resolve(model)
    return RATES[known][2] if known else DEFAULT_CONTEXT


def cache_read_multiplier(model: str) -> float:
    return CACHE_READ_MULTIPLIERS.get(_resolve(model), CACHE_READ_MULTIPLIER)


def is_priced(model: str) -> bool:
    """Is there a rate for this model at all?

    cost_usd() returns 0.0 for a model it does not know, which is
    indistinguishable from a model that genuinely cost nothing -- and that is
    not hypothetical: Opus 4.5 and Sonnet 4.5 were missing, so billions of
    tokens sat in the total at $0.00 and nothing said why. Callers that report
    a figure ask this first and say "unpriced" rather than implying
    free.
    """
    return _resolve(model) is not None


def rewarm_usd(model: str, context_tokens: int, ttl_s: float) -> float | None:
    """Cost to rewrite a session's context after its prompt cache expires."""
    known = _resolve(model)
    if not known:
        return None
    input_rate, _, _ = RATES[known]
    multiplier = CACHE_WRITE_1H_MULTIPLIER if ttl_s >= 3600 else CACHE_WRITE_5M_MULTIPLIER
    return context_tokens * input_rate * multiplier / 1_000_000.0


def warm_usd(model: str, context_tokens: int) -> float | None:
    """Cost to read a session's context while its prompt cache is still warm."""
    known = _resolve(model)
    if not known:
        return None
    input_rate, _, _ = RATES[known]
    return context_tokens * input_rate * cache_read_multiplier(known) / 1_000_000.0


def cost_usd(model: str, tokens: dict) -> float:
    """Zero for an unknown model. Ask is_priced() before believing a zero."""
    known = _resolve(model)
    if not known:
        return 0.0
    inp, out, _ = RATES[known]
    per = 1_000_000.0

    write_total = tokens.get("cache_write", 0) or 0
    write_1h = tokens.get("cache_write_1h", 0) or 0
    # cache_write is the total; never let a malformed subset go negative.
    write_5m = max(0, write_total - write_1h)

    return (
        tokens.get("input", 0) * inp
        + tokens.get("output", 0) * out
        + tokens.get("cache_read", 0) * inp * cache_read_multiplier(known)
        + write_5m * inp * CACHE_WRITE_5M_MULTIPLIER
        + write_1h * inp * CACHE_WRITE_1H_MULTIPLIER
    ) / per
