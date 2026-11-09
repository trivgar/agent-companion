from agent_companion.pricing import (
    cache_read_multiplier, context_limit, cost_usd, is_priced, rewarm_usd,
    warm_usd,
)


# --- rates, straight from the API reference -------------------------------

def test_opus_input_and_output():
    c = cost_usd("claude-opus-5", {"input": 1_000_000, "output": 1_000_000})
    assert round(c, 2) == 30.00


def test_sonnet_is_cheaper_than_opus():
    t = {"input": 1_000_000}
    assert cost_usd("claude-sonnet-5", t) < cost_usd("claude-opus-5", t)


def test_sonnet_4_6_is_priced_apart_from_sonnet_5():
    """3/15, not Sonnet 5's 2/10 -- and not silently free."""
    assert cost_usd("claude-sonnet-4-6", {"input": 1_000_000}) == 3.00
    assert cost_usd("claude-sonnet-4-6", {"output": 1_000_000}) == 15.00


def test_model_id_with_suffix_still_matches():
    """Claude Code reports ids like claude-opus-5[1m]."""
    assert cost_usd("claude-opus-5[1m]", {"input": 1_000_000}) == 5.00


def test_longest_prefix_wins():
    """claude-fable-5-1 must not be billed as claude-fable-5."""
    assert cache_read_multiplier("claude-fable-5-1[1m]") == 0.025
    assert cache_read_multiplier("claude-fable-5") == 0.1


def test_unknown_model_costs_zero_not_raises():
    assert cost_usd("some-future-model", {"input": 1_000_000}) == 0.0


def test_opus_5_5_context_rewarm_and_warm_costs():
    context_tokens = 500_000
    assert rewarm_usd("claude-opus-5-5", context_tokens, 3600) == 4.00
    assert warm_usd("claude-opus-5-5", context_tokens) == 0.10


def test_context_cache_estimates_are_none_for_an_unpriced_model():
    assert rewarm_usd("some-future-model", 500_000, 3600) is None
    assert warm_usd("some-future-model", 500_000) is None


# --- caching ---------------------------------------------------------------

def test_cache_read_is_a_tenth_of_input():
    assert round(cost_usd("claude-opus-5", {"cache_read": 1_000_000}), 2) == 0.50


def test_fable_5_1_cache_read_is_cheaper_still():
    """Fable 5.1 reads at 0.025x ($0.25/MTok), not the usual 0.1x."""
    assert round(cost_usd("claude-fable-5-1", {"cache_read": 1_000_000}), 2) == 0.25


def test_opus_5_5_is_not_billed_as_opus_5():
    """$4/$20, not Opus 5's $5/$25: until 2026-10-18 claude-opus-5-5 fell
    through to the claude-opus-5 row by prefix, and every main session cost
    a quarter too much before its cache reads were counted."""
    assert cost_usd("claude-opus-5-5[1m]", {"input": 1_000_000}) == 4.00
    assert cost_usd("claude-opus-5-5", {"output": 1_000_000}) == 20.00
    assert cost_usd("claude-opus-5", {"input": 1_000_000}) == 5.00


def test_opus_and_sonnet_5_5_read_cache_at_a_twentieth():
    """0.05x (pricing page footnote 2): $0.20 and $0.10 per MTok, where the
    usual 0.1x read Opus 5.5's cache at $0.50 through the Opus 5 row."""
    assert round(cost_usd("claude-opus-5-5", {"cache_read": 1_000_000}), 2) == 0.20
    assert round(cost_usd("claude-sonnet-5-5", {"cache_read": 1_000_000}), 2) == 0.10
    assert round(cost_usd("claude-sonnet-5", {"cache_read": 1_000_000}), 2) == 0.20


def test_opus_5_5_writes_cache_at_the_usual_multipliers():
    tokens = {"cache_write": 1_000_000, "cache_write_1h": 1_000_000}
    assert round(cost_usd("claude-opus-5-5", tokens), 2) == 8.00
    assert round(cost_usd("claude-opus-5-5", {"cache_write": 1_000_000}), 2) == 5.00


def test_mythos_5_1_reads_cache_like_fable_5_1():
    assert cache_read_multiplier("claude-mythos-5-1") == 0.025


def test_haiku_5_5_is_priced_at_its_first_tier():
    """$0.10/$0.50 and a $0.01 cache read for prompts up to 100K tokens. Over
    that a request pays 5x, but a total of many replies cannot say which were
    over, so this is the floor (see pricing.py)."""
    assert is_priced("claude-haiku-5-5")
    assert round(cost_usd("claude-haiku-5-5", {"input": 1_000_000}), 2) == 0.10
    assert round(cost_usd("claude-haiku-5-5", {"output": 1_000_000}), 2) == 0.50
    assert round(cost_usd("claude-haiku-5-5", {"cache_read": 1_000_000}), 3) == 0.010
    assert context_limit("claude-haiku-5-5") == 1_000_000


def test_cache_write_defaults_to_5m_rate():
    """No TTL breakdown recorded -> assume the API default, 1.25x."""
    assert round(cost_usd("claude-opus-5", {"cache_write": 1_000_000}), 2) == 6.25


def test_one_hour_cache_write_is_2x_input():
    """Claude Code uses the 1-hour TTL, which bills at 2x, not 1.25x."""
    tokens = {"cache_write": 1_000_000, "cache_write_1h": 1_000_000}
    assert round(cost_usd("claude-opus-5", tokens), 2) == 10.00


def test_mixed_ttl_writes_split_correctly():
    """cache_write is the total; cache_write_1h is the subset billed at 2x."""
    tokens = {"cache_write": 1_000_000, "cache_write_1h": 400_000}
    # 600k at 1.25x + 400k at 2x, times $5/MTok
    assert round(cost_usd("claude-opus-5", tokens), 2) == round(
        0.6 * 1.25 * 5 + 0.4 * 2 * 5, 2)


def test_1h_larger_than_total_does_not_go_negative():
    """Malformed input must not produce a credit."""
    tokens = {"cache_write": 100, "cache_write_1h": 500}
    assert cost_usd("claude-opus-5", tokens) > 0


# --- context windows -------------------------------------------------------

def test_context_limits():
    assert context_limit("claude-opus-5") == 1_000_000
    assert context_limit("claude-haiku-4-5") == 200_000
    assert context_limit("unknown") == 1_000_000


def test_fable_5_2_and_mythos_5_2_have_their_own_rows():
    # Released 2026-11-09 at $8/$40, cache reads $0.20/MTok. Without a row,
    # claude-fable-5-2 matched the claude-fable-5 prefix: $10/$50, reads at 0.1x.
    for m in ("claude-fable-5-2", "claude-fable-5-2[1m]", "claude-mythos-5-2"):
        assert is_priced(m)
        assert round(cost_usd(m, {"input": 1_000_000}), 2) == 8.00
        assert round(cost_usd(m, {"output": 1_000_000}), 2) == 40.00
        assert round(cost_usd(m, {"cache_read": 1_000_000}), 2) == 0.20
