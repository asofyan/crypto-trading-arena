"""Unit tests for arena.risk — the pre-trade guardrail policy.

The gates are pure (limits and the clock are injected), so these tests need no
venue, no account store and no config. They pin the two properties that matter for
real money: a percentage cap scales with equity (D11), and a denial names the limit
and the attempted value so the agent can fix its own next attempt.
"""

from __future__ import annotations

import pytest

from arena.risk import LiveLimits, PortfolioSnapshot, RiskGates

SOL = 100.0  # round price keeps the notional arithmetic obvious in the deny reasons

# 0.1 SOL @ 100 = a 10 USDT order, comfortably inside the default caps.
DEFAULT_ORDER: dict[str, object] = {
    "symbol": "SOLUSDT",
    "side": "buy",
    "quantity": 0.1,
    "price": SOL,
    "price_age_s": 0.5,
}


def make_limits(**overrides: object) -> LiveLimits:
    """Defaults mirror the week-1 live config: 25/50 percent, 10% daily loss, 2/min."""
    base: dict[str, object] = {
        "allow_symbols": ("SOLUSDT",),
        "max_order_pct_of_equity": 25.0,
        "max_position_pct_of_equity": 50.0,
        "max_order_notional_usdt": None,
        "max_daily_loss_pct": 10.0,
        "max_orders_per_minute": 2,
        "max_price_age_seconds": 5.0,
    }
    base.update(overrides)
    return LiveLimits(**base)  # type: ignore[arg-type]


def make_snapshot(**overrides: object) -> PortfolioSnapshot:
    base: dict[str, object] = {
        "equity_usdt": 95.0,
        "session_start_equity_usdt": 95.0,
        "position_notional_usdt": 0.0,
        "realised_pnl_usdt": 0.0,
        "unrealised_pnl_usdt": 0.0,
    }
    base.update(overrides)
    return PortfolioSnapshot(**base)  # type: ignore[arg-type]


def check(
    gates: RiskGates,
    portfolio: PortfolioSnapshot | None = None,
    **overrides: object,
):
    """Run DEFAULT_ORDER through the gates, with per-test overrides."""
    return gates.check(
        portfolio=portfolio if portfolio is not None else make_snapshot(),
        **{**DEFAULT_ORDER, **overrides},  # type: ignore[arg-type]
    )


# ── Hard invariants: state, allowlist, quote freshness, order shape ──


def test_not_armed_is_denied_and_names_the_state():
    decision = check(RiskGates(make_limits()), armed=False)
    assert not decision.allowed
    assert "ARMED" in decision.reason


def test_halt_flag_denies():
    decision = check(RiskGates(make_limits(), is_halted=lambda: True))
    assert not decision.allowed
    assert "HALTED" in decision.reason


def test_empty_allowlist_denies_every_symbol():
    # An operator who forgets the allowlist must get a closed door, not an open one.
    decision = check(RiskGates(make_limits(allow_symbols=())))
    assert not decision.allowed
    assert "allowlist is empty" in decision.reason


def test_symbol_outside_the_allowlist_is_denied():
    decision = check(RiskGates(make_limits()), symbol="BTCUSDT")
    assert not decision.allowed
    assert "BTCUSDT" in decision.reason
    assert "SOLUSDT" in decision.reason  # tells the agent what it may trade instead


def test_symbol_matching_ignores_case_and_whitespace():
    gates = RiskGates(make_limits(allow_symbols=(" solusdt ",)))
    assert check(gates, symbol=" SolUsdt ").allowed


def test_stale_price_is_denied():
    decision = check(RiskGates(make_limits()), price_age_s=12.0)
    assert not decision.allowed
    assert "12s" in decision.reason
    assert "5s" in decision.reason
    assert "re-quote" in decision.reason


def test_unknown_price_age_is_denied():
    decision = check(RiskGates(make_limits()), price_age_s=float("nan"))
    assert not decision.allowed
    assert "price age is unknown" in decision.reason


@pytest.mark.parametrize("quantity", [0.0, -0.5, float("nan"), float("inf")])
def test_bad_quantity_is_denied(quantity):
    decision = check(RiskGates(make_limits()), quantity=quantity)
    assert not decision.allowed
    assert "quantity must be a positive finite number" in decision.reason


@pytest.mark.parametrize("price", [0.0, -1.0, float("nan"), float("inf")])
def test_bad_price_is_denied(price):
    decision = check(RiskGates(make_limits()), price=price)
    assert not decision.allowed
    assert "price must be a positive finite number" in decision.reason


# ── Percentage sizing caps (the part that must scale) ──


def test_percentage_caps_scale_with_equity():
    """The whole point of D11: the same 25 USDT order flips as the balance grows."""
    gates = RiskGates(make_limits())  # 25% of equity, no absolute ceiling

    small = check(gates, make_snapshot(equity_usdt=95.0), quantity=0.25, price=SOL)
    assert not small.allowed, "25 USDT is 26.3% of a 95 USDT balance"
    assert "$25.00" in small.reason
    assert "26.3% of equity" in small.reason
    assert "25% cap ($23.75)" in small.reason
    assert "reduce size" in small.reason

    large = check(gates, make_snapshot(equity_usdt=195.0), quantity=0.25, price=SOL)
    assert large.allowed, "the same 25 USDT order is only 12.8% of a 195 USDT balance"


def test_order_exactly_at_the_cap_is_allowed():
    # The cap is the maximum, not the first refusal.
    decision = check(RiskGates(make_limits()), make_snapshot(equity_usdt=100.0), quantity=0.25)
    assert decision.allowed


def test_position_cap_counts_the_existing_position():
    gates = RiskGates(make_limits())  # 50% of equity = 50 USDT
    snapshot = make_snapshot(equity_usdt=100.0, position_notional_usdt=45.0)

    decision = check(gates, snapshot, quantity=0.1, price=SOL)  # 45 + 10 = 55
    assert not decision.allowed
    assert "$55.00" in decision.reason
    assert "$50.00" in decision.reason
    assert "reduce size" in decision.reason


def test_position_cap_is_disabled_when_null():
    # `null` is the documented way to switch this gate off; it must not be read as 0.
    gates = RiskGates(make_limits(max_position_pct_of_equity=None))
    snapshot = make_snapshot(equity_usdt=100.0, position_notional_usdt=45.0)
    assert check(gates, snapshot, quantity=0.1, price=SOL).allowed


def test_sell_that_reduces_a_position_is_not_blocked_by_the_position_cap():
    # Exiting must always be possible: a sell can only lower exposure, so counting
    # it as new exposure would trap the agent in a losing position.
    gates = RiskGates(make_limits())
    snapshot = make_snapshot(equity_usdt=100.0, position_notional_usdt=45.0)
    decision = check(gates, snapshot, side="sell", quantity=0.2, price=SOL)  # 45 - 20
    assert decision.allowed


# ── Absolute per-order backstop ──


def test_absolute_notional_ceiling_denies():
    # Equity is large enough that the percentage cap passes; only the ceiling trips.
    gates = RiskGates(make_limits(max_order_notional_usdt=25.0))
    decision = check(
        gates,
        make_snapshot(equity_usdt=1000.0),
        quantity=0.3,
        price=SOL,  # 30 USDT
    )
    assert not decision.allowed
    assert "$30.00" in decision.reason
    assert "$25.00" in decision.reason
    assert "ceiling" in decision.reason


def test_absolute_ceiling_is_off_when_unset():
    gates = RiskGates(make_limits(max_order_notional_usdt=None))
    decision = check(gates, make_snapshot(equity_usdt=1000.0), quantity=2.0, price=SOL)
    assert decision.allowed  # 200 USDT is 20% of equity, inside the percentage cap


# ── Daily-loss halt, as a share of session-start equity ──


def test_daily_loss_halt_fires_on_a_realised_loss():
    gates = RiskGates(make_limits())  # 10% of 95 = 9.50
    decision = check(gates, make_snapshot(realised_pnl_usdt=-10.0))
    assert not decision.allowed
    assert "$-10.00" in decision.reason
    assert "10% daily-loss limit ($9.50 of session-start equity)" in decision.reason
    assert "no further orders" in decision.reason


def test_daily_loss_counts_unrealised_pnl():
    # A dump that never realised is still a loss the operator must be protected from.
    gates = RiskGates(make_limits())
    decision = check(gates, make_snapshot(realised_pnl_usdt=-4.0, unrealised_pnl_usdt=-6.0))
    assert not decision.allowed
    assert "daily-loss limit" in decision.reason


def test_daily_loss_limit_scales_with_a_top_up():
    # D10/D11: a top-up raises the loss budget in step with the balance.
    gates = RiskGates(make_limits())
    tiny = check(gates, make_snapshot(equity_usdt=95.0, realised_pnl_usdt=-12.0))
    assert not tiny.allowed, "-12 is past 10% of a 95 USDT session"

    topped_up = check(
        gates,
        make_snapshot(equity_usdt=195.0, session_start_equity_usdt=195.0, realised_pnl_usdt=-12.0),
    )
    assert topped_up.allowed, "-12 is only 6.2% of a 195 USDT session"


@pytest.mark.parametrize("equity", [0.0, -5.0])
def test_non_positive_equity_is_denied(equity):
    decision = check(RiskGates(make_limits()), make_snapshot(equity_usdt=equity))
    assert not decision.allowed
    assert "non-positive balance" in decision.reason


def test_unmeasurable_session_start_is_denied():
    # Fail closed: without a positive session-start equity the loss cap is undefined.
    gates = RiskGates(make_limits())
    decision = check(
        gates,
        make_snapshot(equity_usdt=50.0, session_start_equity_usdt=0.0),
    )
    assert not decision.allowed
    assert "cannot be measured" in decision.reason


def test_non_finite_snapshot_is_denied():
    # A NaN position would make every later `>` comparison pass; refuse it up front.
    gates = RiskGates(make_limits())
    decision = check(gates, make_snapshot(position_notional_usdt=float("nan")))
    assert not decision.allowed
    assert "non-finite" in decision.reason


# ── Order rate, with an injected clock ──


def test_third_order_within_the_window_is_denied():
    clock = [1000.0]
    gates = RiskGates(make_limits(max_orders_per_minute=2), now=lambda: clock[0])

    gates.record_order()
    clock[0] += 1.0
    gates.record_order()

    decision = check(gates)
    assert not decision.allowed
    assert "2 orders already submitted in the last 60s" in decision.reason
    assert "wait 59.0s" in decision.reason


def test_rate_window_reopens_after_a_minute():
    clock = [1000.0]
    gates = RiskGates(make_limits(max_orders_per_minute=2), now=lambda: clock[0])
    gates.record_order()
    clock[0] += 1.0
    gates.record_order()

    clock[0] = 1059.0  # still inside the window opened at t=1000
    assert not check(gates).allowed

    clock[0] = 1061.0  # both submissions are now older than 60s
    assert check(gates).allowed


def test_only_submitted_orders_consume_the_rate_budget():
    gates = RiskGates(make_limits(max_orders_per_minute=2))
    for _ in range(5):
        assert check(gates).allowed, "a check is not an order"

    gates.record_order()
    gates.record_order()
    assert not check(gates).allowed
