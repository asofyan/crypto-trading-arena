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


# ── `scalper-perp` reads the market, it does not follow a checklist ─
# The operator asked for probability, not determinism: entry is the model's read, and
# only the mechanics (net position, 1:1, product scope) are rules.


def test_scalper_perp_prompt_is_a_probabilistic_both_ways_scalper():
    prompt = STRATEGIES["scalper-perp"]
    assert "PERPETUAL FUTURES" in prompt
    lowered = prompt.lower()
    assert "long" in lowered and "short" in lowered
    # It must say the choice is judgement, not a setup to satisfy.
    assert "no checklist to satisfy" in lowered


def test_scalper_perp_prompt_keeps_the_mechanics_that_are_not_strategy():
    """Net-position semantics and the 1:1 cap are facts about the executor, not rules."""
    prompt = STRATEGIES["scalper-perp"]
    assert "flips you short" in prompt
    assert "flips you long" in prompt
    assert "1:1" in prompt
    assert "equity" in prompt.lower()
    assert "get_portfolio" in prompt
    assert "-PERP" in prompt


def test_scalper_perp_prompt_has_no_fixed_entry_or_exit_thresholds():
    """A hard percentage would put the rulebook back; the operator removed it on purpose."""
    prompt = STRATEGIES["scalper-perp"]
    for token in ("-0.6%", "+1.0%", "0.8%", "three times", "10 closed 1-minute bars"):
        assert token not in prompt, token


def test_scalper_perp_prompt_leaves_sizing_and_exit_to_conviction():
    lowered = STRATEGIES["scalper-perp"].lower()
    assert "conviction" in lowered
    assert "prove you wrong" in lowered
    assert "cash is a position" in lowered
