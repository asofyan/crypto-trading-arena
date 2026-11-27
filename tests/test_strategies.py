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
    """The portfolio tool is the only *current* record; history is context, not memory.

    The prompt used to claim the agent has no memory at all. calfkit's state is an
    append-only message history, so that was false — and believing it is what let a stale
    position survive the fill that changed it (ADR-0011).
    """
    prompt = STRATEGIES["default"]
    assert "get_portfolio" in prompt
    assert "claim about the past" in prompt
    assert "may have landed since" in prompt
    assert "you do not remember" not in prompt
    assert "fresh context" not in prompt


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
# only the mechanics (net position, 1:1, product scope) are rules. The one exception is
# cost: after a measured run of 111 round trips made +$0.096 gross against $2.058 of
# fees, the fee floor went back in as a non-negotiable constraint, not as a rulebook.


def test_scalper_perp_prompt_is_a_probabilistic_both_ways_scalper():
    prompt = STRATEGIES["scalper-perp"]
    assert "PERPETUAL FUTURES" in prompt
    lowered = prompt.lower()
    assert "long" in lowered and "short" in lowered
    # It must say the choice is judgement, not a setup to satisfy.
    assert "no checklist to satisfy" in lowered


def test_scalper_perp_prompt_has_a_non_negotiable_cost_floor():
    """Direction is discretionary; the fee arithmetic is not."""
    prompt = STRATEGIES["scalper-perp"]
    lowered = prompt.lower()
    assert "cost floor" in lowered
    assert "not negotiable" in lowered


def test_scalper_perp_prompt_states_the_round_trip_cost():
    """The whole loss came from fees, so the fee has to be named, not implied."""
    lowered = STRATEGIES["scalper-perp"].lower()
    assert "round trip" in lowered
    assert "bps" in lowered
    # Two fills per round trip, and the per-fill rate comes from the market message.
    assert "two taker fills" in lowered
    assert "market message" in lowered


def test_scalper_perp_prompt_names_the_enforced_exit_band():
    """The engine refuses scratch exits, so the prompt must state the band it enforces.

    A 3x-round-trip entry bar did not stop the bleed: the agent still closed at ~0 bps,
    paying 10 bps to capture nothing. The exit floor is now a machine rule, and a prompt
    that contradicts it only manufactures rejected tool calls.
    """
    prompt = STRATEGIES["scalper-perp"]
    assert "+100 bps" in prompt
    assert "-50 bps" in prompt
    # The band is measured from the entry price, not from the last touch.
    assert "entry" in prompt.lower() and "price" in prompt.lower()
    # The refusal has to be said out loud, or the model keeps attempting the close.
    assert "refused" in prompt.lower()
    assert "do not try" in prompt.lower()


def test_scalper_perp_prompt_lists_exactly_three_ways_out():
    """Target, stop, or the 60-minute escape. A 2-minute time-stop is a 0 bps exit."""
    lowered = STRATEGIES["scalper-perp"].lower()
    assert "target" in lowered
    assert "stop" in lowered
    assert "60-minute hold limit" in lowered
    # The old time-stop produced exactly the closes the exit floor now rejects.
    assert "two minutes" not in lowered
    assert "2 minutes" not in lowered
    assert "close it" not in lowered


def test_scalper_perp_prompt_states_why_the_band_pays():
    """10 bps round trip against a 100 bps target: the fee share has to be spelled out."""
    lowered = STRATEGIES["scalper-perp"].lower()
    assert "fee math" in lowered
    assert "10%" in lowered
    assert "net loss" in lowered


def test_scalper_perp_band_matches_the_enforced_exit_floor():
    """The prompt names the band the engine enforces; a drift here manufactures rejects."""
    from arena.trade_policy import ExitFloor

    prompt = STRATEGIES["scalper-perp"]
    floor = ExitFloor()

    assert f"+{floor.min_profit_bps:.0f} bps" in prompt
    assert f"-{floor.stop_bps:.0f} bps" in prompt
    assert f"{floor.max_hold_seconds / 60:.0f}-minute hold limit" in prompt


def test_scalper_perp_is_told_to_read_the_lean_trend_context():
    """The lean payload carries a higher-timeframe line; the prompt has to explain it."""
    lowered = STRATEGIES["scalper-perp"].lower()
    assert "intraday candle tables" in lowered
    assert "trend line" in lowered
    assert "regime" in lowered
    assert "15-min" in lowered


def test_scalper_perp_prompt_restores_the_per_position_cap():
    """The 40% sizing rule was replaced by 'use the whole 1:1 allowance' — undo that."""
    prompt = STRATEGIES["scalper-perp"]
    assert "25%" in prompt
    assert "portfolio value" in prompt.lower()


def test_scalper_perp_prompt_bounds_turnover():
    """$4,116 of turnover on $100 of equity in two hours is the failure being fixed."""
    lowered = STRATEGIES["scalper-perp"].lower()
    assert "per hour" in lowered
    assert "per minute" in lowered
    assert "1:1 allowance" in lowered
    assert "realized p&l" in lowered


def test_scalper_perp_prompt_keeps_the_mechanics_that_are_not_strategy():
    """Net-position semantics and the 1:1 cap are facts about the executor, not rules.

    The flip sentence moved: ``execute_trade`` stopped crossing zero on 2026-11-27, so a
    prompt still promising a one-order flip would manufacture refused tool calls.
    """
    prompt = STRATEGIES["scalper-perp"]
    assert "never crosses zero" in prompt.lower()
    assert "close_position" in prompt
    assert "1:1" in prompt
    assert "equity" in prompt.lower()
    assert "get_portfolio" in prompt
    assert "-PERP" in prompt


def test_scalper_perp_prompt_has_no_directional_rulebook():
    """Cost numbers are allowed back; the entry/exit threshold checklist is not."""
    prompt = STRATEGIES["scalper-perp"]
    for token in ("-0.6%", "+1.0%", "0.8%", "10 closed 1-minute bars", "40%"):
        assert token not in prompt, token


def test_scalper_perp_prompt_leaves_sizing_and_exit_to_conviction():
    lowered = STRATEGIES["scalper-perp"].lower()
    assert "conviction" in lowered
    assert "prove you wrong" in lowered
    assert "cash is a position" in lowered


def test_scalper_perp_exits_by_naming_the_product_not_a_size():
    """An exit carries no quantity, so no stale number can become an opening order."""
    prompt = STRATEGIES["scalper-perp"]
    lowered = prompt.lower()

    assert "close_position" in prompt
    assert "never size a close yourself" in lowered
    # The band still decides *when* an exit is allowed; only the sizing moved to the server.
    assert "+100 bps" in prompt and "-50 bps" in prompt


def test_scalper_perp_prompt_does_not_claim_the_model_has_no_memory():
    """calfkit's state is an append-only message history, so the old claim was false — and
    believing it is what let a 0.62 read survive the fill that made it 0.32."""
    lowered = STRATEGIES["scalper-perp"].lower()

    assert "you do not remember earlier turns" not in lowered
    assert "may have landed since" in lowered
    assert "close_position" in lowered  # the remedy, not just the warning


def test_no_prompt_claims_the_agent_has_no_memory():
    """calfkit keeps an append-only message history, so the claim was false everywhere it
    appeared. A prompt that says it is amnesiac removes any reason to distrust an old read."""
    for name, prompt in STRATEGIES.items():
        lowered = prompt.lower()
        assert "you do not remember earlier turns" not in lowered, name
        assert "fresh context" not in lowered, name


def test_scalper_perp_warns_that_a_reversal_waits_for_the_band():
    """The flip refusal says 'close, then open'; inside the exit band the close is refused
    too, so the prompt must not sell a reversal as a way out (finding F2 from review)."""
    lowered = STRATEGIES["scalper-perp"].lower()
    assert "reversing is therefore two orders" in lowered
    assert "inside the exit band the close is refused" in lowered
    assert "until you have called get_portfolio" in lowered


def test_no_prompt_promises_more_turnover_than_the_churn_budget_allows():
    """6 fills/hour by default; 4 round trips is 8 fills, so the old guidance was
    unreachable by construction (finding F8 from review)."""
    from arena.trade_policy import ChurnLimits

    prompt = STRATEGIES["scalper-perp"]
    budget = ChurnLimits().max_fills_per_hour
    assert budget is not None
    assert "2-3 round trips per hour" in prompt
    assert f"{budget} fills" not in prompt  # the number lives in the guard, not the prompt
