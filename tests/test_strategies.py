"""Unit tests for the strategy registry that deploy/agent selects from."""

from __future__ import annotations

from arena.strategies import STRATEGIES


def test_expected_strategies_are_registered():
    assert set(STRATEGIES) == {
        "default",
        "momentum",
        "brainrot",
        "scalper",
        "scalper-perp",
        "swing-trend",
    }


def test_every_strategy_prompt_requests_reasoning():
    """Each prompt ends with the shared reasoning addendum so agents explain trades."""
    for name, prompt in STRATEGIES.items():
        assert prompt.strip(), name
        assert "Reasoning:" in prompt, name


def test_strategy_prompts_are_distinct():
    assert len(set(STRATEGIES.values())) == len(STRATEGIES)


# ── `default` is the risk-managed baseline ───────────────────────
# Its prompt has to stand on its own: every invocation is a separate turn, so
# the guards a trader would hold in memory must be written down.


def test_default_prompt_opens_with_a_portfolio_check():
    """Agents are stateless between ticks; the portfolio tool is their only memory."""
    prompt = STRATEGIES["default"]
    assert "get_portfolio" in prompt
    assert "fresh context" in prompt or "do not remember" in prompt


def test_default_prompt_gates_trades_on_the_disclosed_fee():
    """Fee is disclosed per turn by the connector, so the rule must reference it, not a constant."""
    prompt = STRATEGIES["default"]
    assert "fee" in prompt.lower()
    assert "round trip" in prompt.lower()
    assert "twice" in prompt.lower()
    # A hardcoded rate would silently drift from trading.fees.taker_bps.
    assert "bps" not in prompt


def test_default_prompt_caps_concentration():
    assert "25%" in STRATEGIES["default"]


def test_default_prompt_declares_a_horizon_matching_the_candles():
    prompt = STRATEGIES["default"]
    assert "1-min" in prompt and "15-min" in prompt
    assert "horizon" in prompt.lower()


def test_default_prompt_makes_holding_an_explicit_decision():
    prompt = STRATEGIES["default"].lower()
    assert "hold" in prompt
    assert "not trading" in prompt or "no edge" in prompt


# ── `swing-trend` is calibrated on 4-hour bars ──────────────────
# The prompt must name its timeframe and every lookback explicitly: the agent
# recomputes them from raw candles on each invocation, with no indicator tool.


def test_swing_trend_prompt_declares_the_4h_horizon():
    prompt = STRATEGIES["swing-trend"]
    assert "4-hour" in prompt
    assert "daily" in prompt.lower()  # daily bars are context, not the trigger


def test_swing_trend_prompt_states_the_optimised_lookbacks():
    prompt = STRATEGIES["swing-trend"]
    for token in ("20-bar", "50-bar", "55-bar", "4%"):
        assert token in prompt, token


def test_swing_trend_prompt_defines_regime_entries_and_exits():
    prompt = STRATEGIES["swing-trend"].lower()
    assert "flat" in prompt
    assert "55-bar" in prompt
    assert "pullback" in prompt
    assert "exit" in prompt
    assert "cash" in prompt


# ── `scalper-perp` trades both directions on 1:1 leverage ───────
# Every rule the agent cannot remember between invocations has to be written down,
# including the fact that `sell` opens a short and that leverage is capped at 1:1.


def test_scalper_perp_prompt_is_short_horizon_and_directional():
    prompt = STRATEGIES["scalper-perp"]
    assert "PERPETUAL FUTURES" in prompt
    assert "1-minute" in prompt and "5-minute" in prompt and "15-minute" in prompt
    lowered = prompt.lower()
    assert "long" in lowered and "short" in lowered


def test_scalper_perp_prompt_explains_net_position_semantics():
    """`sell` from flat must be documented as opening a short, not as a spot sale."""
    prompt = STRATEGIES["scalper-perp"]
    assert "flips you short" in prompt
    assert "flips you long" in prompt
    assert "net" in prompt.lower()


def test_scalper_perp_prompt_states_the_1to1_cap():
    prompt = STRATEGIES["scalper-perp"]
    assert "1:1" in prompt
    assert "equity" in prompt.lower()


def test_scalper_perp_prompt_only_trades_perp_products():
    assert "-PERP" in STRATEGIES["scalper-perp"]


def test_scalper_perp_prompt_gates_entries_on_the_disclosed_fee():
    """The fee is disclosed per turn by the connector, so the rule must not hardcode it."""
    prompt = STRATEGIES["scalper-perp"]
    assert "round-trip fee" in prompt.lower()
    assert "bps" not in prompt


def test_scalper_perp_prompt_has_a_stop_a_target_and_a_time_stop():
    prompt = STRATEGIES["scalper-perp"]
    for token in ("-0.6%", "+1.0%", "15 minutes"):
        assert token in prompt, token
    assert "never average a loser" in prompt.lower()
