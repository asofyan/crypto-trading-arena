"""Unit tests for the guard-experiment scorecard and its stop/continue rules.

The verdict is what an operator acts on, so each branch is pinned: a wrong `continue`
costs another run, a wrong `stop` throws away a working strategy.
"""

from __future__ import annotations

from arena.experiment import (
    RoundTrip,
    Scorecard,
    classify_exit,
    excursion,
    sharpe_like,
    verdict,
)


POSITION_USD = 25.0  # the 25% per-product cap on a $100 wallet
ROUND_TRIP_FEE = POSITION_USD * 10 / 1e4


def trip(bps: float, *, hold_s: float = 60.0, mfe: float | None = None, net: float = 0.0):
    """A round trip at ``bps`` of a $100 entry, with an optional best-moment excursion."""
    return RoundTrip(
        product_id="SOLUSDT-PERP",
        entry_ts=0.0,
        exit_ts=hold_s,
        direction=1,
        entry_price=100.0,
        exit_price=100.0 * (1 + bps / 1e4),
        net_pnl=net if net else bps / 1e4 * POSITION_USD,
        fees=ROUND_TRIP_FEE,  # 10 bps on the 25% position the band sizes
        hold_s=hold_s,
        mfe_bps=bps if mfe is None else mfe,
        mae_bps=0.0,
    )


def card(trips, *, window_h: float = 2.0, fees_note: float = 0.0, equity: float = 100.0):
    return Scorecard(
        window_s=window_h * 3600.0,
        equity=equity,
        fills=len(trips) * 2,
        round_trips=list(trips),
    )


# ── exit classification ─────────────────────────────────────────


def test_exit_at_the_target_is_a_target():
    assert classify_exit(100.0, 60.0) == "target"
    assert classify_exit(140.0, 60.0) == "target"


def test_exit_at_the_stop_is_a_stop():
    assert classify_exit(-50.0, 60.0) == "stop"
    assert classify_exit(-90.0, 60.0) == "stop"


def test_exit_past_the_hold_limit_is_an_escape():
    assert classify_exit(5.0, 3600.0) == "escape"


def test_exit_inside_the_band_before_the_limit_is_named():
    """The one outcome that must never happen while the floor is armed."""
    assert classify_exit(0.0, 60.0) == "inside-band"
    assert classify_exit(99.9, 3599.0) == "inside-band"
    assert classify_exit(-49.9, 10.0) == "inside-band"


# ── excursion ───────────────────────────────────────────────────


def test_excursion_measures_both_sides_for_a_long():
    mfe, mae = excursion([101.0, 99.0, 100.5], 100.0, 1)
    assert round(mfe) == 100  # +1% = 100 bps
    assert round(mae) == 100


def test_excursion_inverts_for_a_short():
    mfe, mae = excursion([99.0, 101.0], 100.0, -1)
    assert round(mfe) == 100
    assert round(mae) == 100


def test_excursion_without_prices_is_zero():
    assert excursion([], 100.0, 1) == (0.0, 0.0)
    assert excursion([101.0], 0.0, 1) == (0.0, 0.0)


# ── verdict: mechanics first ────────────────────────────────────


def test_a_close_inside_the_band_stops_the_run_as_a_guard_failure():
    decision, reasons = verdict(card([trip(10.0) for _ in range(8)]))
    assert decision == "stop"
    assert "did not fire" in reasons[0]


def test_a_trapped_position_stops_the_run():
    c = card([trip(110.0, hold_s=3600.0 + 400.0) for _ in range(8)])
    decision, reasons = verdict(c)
    assert decision == "stop"
    assert "escape hatch" in reasons[0]


def test_fee_burn_above_the_budget_stops_the_run():
    c = card([trip(110.0) for _ in range(8)], window_h=0.5)
    c.round_trips = [trip(110.0) for _ in range(8)]
    c.round_trips[0] = RoundTrip(
        "SOLUSDT-PERP", 0.0, 110.0, 1, 100.0, 101.1, net_pnl=0.5, fees=5.0, hold_s=60.0,
        mfe_bps=110.0,
    )
    decision, reasons = verdict(c)
    assert decision == "stop"
    assert "budget" in reasons[0]


# ── verdict: the strategy question ──────────────────────────────


def test_too_few_round_trips_is_inconclusive_not_a_stop():
    decision, reasons = verdict(card([trip(110.0, mfe=110.0) for _ in range(3)]))
    assert decision == "inconclusive"
    assert "at least" in reasons[0]


def test_never_reaching_the_target_stops_the_run():
    """The question a short run can answer: is +50 bps reachable at all?"""
    c = card([trip(5.0, hold_s=3600.0, mfe=18.0) for _ in range(7)])
    decision, reasons = verdict(c)
    assert decision == "stop"
    assert "not reachable" in reasons[0]


def test_losing_more_than_the_fees_cost_stops_the_run():
    c = card(
        [
            trip(110.0, mfe=110.0, net=0.02),
            *[trip(-60.0, mfe=110.0, net=-0.10) for _ in range(5)],
        ]
    )
    decision, reasons = verdict(c)
    assert decision == "stop"
    assert "larger than the cost" in reasons[0]


def test_two_target_hits_and_no_loss_keeps_going():
    c = card(
        [
            trip(110.0, mfe=120.0, net=0.10),
            trip(110.0, mfe=110.0, net=0.10),
            *[trip(-60.0, mfe=110.0, net=-0.03) for _ in range(4)],
        ]
    )
    decision, reasons = verdict(c)
    assert decision == "continue"
    assert "reachable" in reasons[0]


def test_some_reach_with_a_loss_is_inconclusive():
    c = card(
        [
            trip(110.0, mfe=110.0, net=0.02),
            *[trip(-60.0, mfe=110.0, net=-0.04) for _ in range(5)],
        ]
    )
    decision, _ = verdict(c)
    assert decision == "inconclusive"


# ── descriptive ratio ───────────────────────────────────────────


def test_ratio_needs_three_trips():
    assert sharpe_like(card([trip(10.0), trip(10.0)])) is None


def test_ratio_is_none_when_there_is_no_variance():
    assert sharpe_like(card([trip(10.0, net=0.1) for _ in range(5)])) is None
