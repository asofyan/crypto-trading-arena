"""Unit tests for arena.trade_policy.ChurnGuard.

The guard exists because of a measured incident: 140 fills in two hours on a $100
wallet cost $2.06 in taker fees (2% of equity) to earn $0.10 of gross P&L. Every one
of those fills was individually legal, so the properties worth pinning down here are
the *rate* ones: a rolling window that forgets, a cooldown that only blocks re-entry,
and a hard rule that an exit is never refused.
"""

from __future__ import annotations

from arena.trade_policy import (
    ChurnGuard,
    ChurnLimits,
    ChurnVerdict,
    ExitFloor,
    is_strict_reduce,
)

PERP = "SOLUSDT-PERP"
SPOT = "SOL-USD"
AGENT = "scalper-perp"
EQUITY = 100.0


class FakeClock:
    """Injectable monotonic clock: the windows are measured in exact seconds."""

    def __init__(self, start: float = 1_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make_guard(**limits) -> tuple[ChurnGuard, FakeClock]:
    clock = FakeClock()
    return ChurnGuard(ChurnLimits(**limits), clock=clock), clock


def check(
    guard: ChurnGuard,
    *,
    product_id: str = PERP,
    fee: float = 0.05,
    equity: float = EQUITY,
    reduces_exposure: bool = False,
) -> ChurnVerdict:
    return guard.check(
        agent_id=AGENT,
        product_id=product_id,
        fee_estimate=fee,
        equity=equity,
        reduces_exposure=reduces_exposure,
    )


# ── Cooldown: blocks the re-open, never the exit ────────────────


def test_cooldown_blocks_reopening_the_same_product():
    guard, clock = make_guard(
        max_fills_per_hour=None, max_fee_burn_pct_per_hour=None, cooldown_seconds=60.0
    )
    guard.record(agent_id=AGENT, product_id=PERP, fee=0.05)

    clock.advance(30.0)
    verdict = check(guard)

    assert verdict.allowed is False
    assert "cooldown" in verdict.reason.lower()
    # Actionable: it must say how long the agent still has to wait.
    assert "30s" in verdict.reason


def test_cooldown_never_blocks_a_reduce():
    guard, _ = make_guard(
        max_fills_per_hour=None, max_fee_burn_pct_per_hour=None, cooldown_seconds=60.0
    )
    guard.record(agent_id=AGENT, product_id=PERP, fee=0.05)

    # Same instant, same product: the exit must still go through.
    closing = check(guard, reduces_exposure=True)

    assert closing.allowed is True


def test_cooldown_expires_on_the_next_growing_fill():
    guard, clock = make_guard(
        max_fills_per_hour=None, max_fee_burn_pct_per_hour=None, cooldown_seconds=60.0
    )
    guard.record(agent_id=AGENT, product_id=PERP, fee=0.05)

    clock.advance(59.9)
    assert check(guard).allowed is False

    clock.advance(0.1)
    assert check(guard).allowed is True


def test_cooldown_is_per_product():
    guard, _ = make_guard(
        max_fills_per_hour=None, max_fee_burn_pct_per_hour=None, cooldown_seconds=60.0
    )
    guard.record(agent_id=AGENT, product_id=PERP, fee=0.05)

    # A different product is a different idea, not the same one repeated.
    assert check(guard, product_id=SPOT).allowed is True


def test_cooldown_matches_whatever_case_the_callers_use():
    guard, _ = make_guard(
        max_fills_per_hour=None, max_fee_burn_pct_per_hour=None, cooldown_seconds=60.0
    )
    guard.record(agent_id=AGENT, product_id=" solusdt-perp ", fee=0.05)

    assert check(guard, product_id=PERP).allowed is False


# ── Fill rate: every fill counts, reduced ones included ─────────


def test_max_fills_per_hour_counts_all_fills():
    guard, _ = make_guard(
        max_fills_per_hour=3, cooldown_seconds=None, max_fee_burn_pct_per_hour=None
    )
    for _ in range(3):
        assert check(guard).allowed is True
        guard.record(agent_id=AGENT, product_id=PERP, fee=0.0)

    verdict = check(guard, reduces_exposure=False)

    assert verdict.allowed is False
    assert "3" in verdict.reason
    assert "fills" in verdict.reason


def test_max_fills_per_hour_counts_reduces_too():
    """The cap is about fee spend, so a round trip costs two slots, not one."""
    guard, _ = make_guard(
        max_fills_per_hour=2, cooldown_seconds=None, max_fee_burn_pct_per_hour=None
    )
    guard.record(agent_id=AGENT, product_id=PERP, fee=0.0)  # open
    guard.record(agent_id=AGENT, product_id=PERP, fee=0.0)  # close

    assert check(guard).allowed is False


def test_max_fills_per_hour_is_per_agent():
    guard, _ = make_guard(
        max_fills_per_hour=1, cooldown_seconds=None, max_fee_burn_pct_per_hour=None
    )
    guard.record(agent_id=AGENT, product_id=PERP, fee=0.0)

    other = guard.check(
        agent_id="scalper-spot",
        product_id=SPOT,
        fee_estimate=0.05,
        equity=EQUITY,
        reduces_exposure=False,
    )
    assert other.allowed is True


# ── Fee burn: the number that paid for this module ──────────────


def test_fee_burn_cap_denies_a_fill_that_would_overspend_the_hour():
    guard, _ = make_guard(
        max_fills_per_hour=None, cooldown_seconds=None, max_fee_burn_pct_per_hour=0.25
    )
    # 0.25% of $100 = $0.25 of fees per hour; $0.30 spent already blew it.
    guard.record(agent_id=AGENT, product_id=PERP, fee=0.30)

    verdict = check(guard, fee=0.01)

    assert verdict.allowed is False
    assert "fee budget spent" in verdict.reason.lower()
    assert "$0.30" in verdict.reason  # what was spent
    assert "$0.25" in verdict.reason  # what the hour allowed
    assert "$100.00" in verdict.reason  # the equity it was measured against


def test_fee_burn_cap_counts_the_projected_fill():
    """A budget that only fires *after* the overspend has already lost the money."""
    guard, _ = make_guard(
        max_fills_per_hour=None, cooldown_seconds=None, max_fee_burn_pct_per_hour=0.25
    )
    guard.record(agent_id=AGENT, product_id=PERP, fee=0.20)

    # $0.20 + $0.06 = $0.26 > $0.25: this fill is the one that tips it over.
    assert check(guard, fee=0.06).allowed is False
    # $0.20 + $0.04 = $0.24: still inside the hour's budget.
    assert check(guard, fee=0.04).allowed is True


def test_fee_burn_cap_never_blocks_a_reduce():
    guard, _ = make_guard(
        max_fills_per_hour=None, cooldown_seconds=None, max_fee_burn_pct_per_hour=0.25
    )
    guard.record(agent_id=AGENT, product_id=PERP, fee=5.0)

    assert check(guard, fee=1.0, reduces_exposure=True).allowed is True


def test_fee_burn_cap_needs_a_measurable_equity():
    """A zero or broken equity feed cannot divide — and must not crash the order path."""
    guard, _ = make_guard(
        max_fills_per_hour=None, cooldown_seconds=None, max_fee_burn_pct_per_hour=0.25
    )
    guard.record(agent_id=AGENT, product_id=PERP, fee=5.0)

    assert check(guard, equity=0.0).allowed is True
    assert check(guard, equity=float("nan")).allowed is True


# ── The rolling hour ────────────────────────────────────────────


def test_window_expiry_frees_the_budget():
    guard, clock = make_guard(
        max_fills_per_hour=2, cooldown_seconds=None, max_fee_burn_pct_per_hour=0.25
    )
    guard.record(agent_id=AGENT, product_id=PERP, fee=0.5)
    guard.record(agent_id=AGENT, product_id=PERP, fee=0.5)
    assert check(guard).allowed is False

    clock.advance(3_599.0)
    assert check(guard).allowed is False

    clock.advance(1.0)
    # Both the count and the fees fell out of the window together.
    assert check(guard).allowed is True


def test_window_prunes_old_fills_without_forgetting_the_new_ones():
    guard, clock = make_guard(
        max_fills_per_hour=2, cooldown_seconds=None, max_fee_burn_pct_per_hour=None
    )
    guard.record(agent_id=AGENT, product_id=PERP, fee=0.0)
    clock.advance(3_600.0)
    guard.record(agent_id=AGENT, product_id=PERP, fee=0.0)

    assert check(guard).allowed is True  # only one fill is inside the hour

    guard.record(agent_id=AGENT, product_id=PERP, fee=0.0)
    assert check(guard).allowed is False


# ── Defaults, disables, and a guard that has seen nothing ───────


def test_check_without_any_record_allows():
    guard, _ = make_guard()  # shipped defaults: 6 fills, 60s cooldown, 0.25%

    assert check(guard, fee=0.01).allowed is True


def test_a_fee_larger_than_the_whole_hour_budget_is_refused():
    """$1 of fees against a $0.25 hourly budget cannot be earned back by any P&L."""
    guard, _ = make_guard()

    verdict = check(guard, fee=1.0)

    assert verdict.allowed is False
    assert "fee budget spent" in verdict.reason.lower()


def test_every_limit_can_be_disabled():
    guard, clock = make_guard(
        max_fills_per_hour=None, cooldown_seconds=None, max_fee_burn_pct_per_hour=None
    )
    for _ in range(50):
        guard.record(agent_id=AGENT, product_id=PERP, fee=1.0)
        clock.advance(1.0)

    assert check(guard, fee=1.0).allowed is True


def test_shipped_defaults_match_the_incident_analysis():
    limits = ChurnLimits()

    assert limits.max_fills_per_hour == 6
    assert limits.cooldown_seconds == 60.0
    assert limits.max_fee_burn_pct_per_hour == 0.25


# ── Exit floor: no scratch exits between the stop and the target ─
#
# 29 of 111 measured exit legs closed at 0-10 bps of profit, against a 10 bps round
# trip: the median leg moved 0.00 bps and paid two fees to get there. The floor names
# the band a close may not land in; both edges stay open, and so does the exit of a
# position that has simply sat too long.


def check_floor(floor: ExitFloor, *, bps: float, held: float = 10.0) -> ChurnVerdict:
    return floor.check(bps_move=bps, held_seconds=held)


def test_exit_floor_allows_a_close_at_the_target():
    assert check_floor(ExitFloor(), bps=100.0).allowed is True
    assert check_floor(ExitFloor(), bps=250.0).allowed is True


def test_exit_floor_allows_a_close_at_the_stop():
    """The stop is the other edge of the band: cutting a loss is never refused."""
    assert check_floor(ExitFloor(), bps=-50.0).allowed is True
    assert check_floor(ExitFloor(), bps=-500.0).allowed is True


def test_exit_floor_refuses_a_close_inside_the_band():
    for bps in (-49.9, -25.0, 0.0, 7.3, 50.0, 99.9):
        assert check_floor(ExitFloor(), bps=bps).allowed is False, bps


def test_exit_floor_reason_is_actionable():
    verdict = check_floor(ExitFloor(), bps=7.3, held=12.0)

    assert verdict.allowed is False
    assert "exit floor" in verdict.reason.lower()
    # Every number the agent needs to act on: where it is, where the target is, where
    # the stop is, and how long the timeout buys it.
    assert "7.3" in verdict.reason
    assert "100.0" in verdict.reason
    assert "50.0" in verdict.reason
    assert "3600" in verdict.reason


def test_exit_floor_lets_a_stale_position_go_at_the_hold_limit():
    assert check_floor(ExitFloor(), bps=0.0, held=3600.0).allowed is True
    assert check_floor(ExitFloor(), bps=0.0, held=20_000.0).allowed is True
    # One second short of the limit is still inside it: the escape is exact, not fuzzy.
    assert check_floor(ExitFloor(), bps=0.0, held=3599.0).allowed is False


def test_hold_limit_none_means_no_escape():
    """``None`` removes the timeout, so the band keeps applying to an old position."""
    open_ended = ExitFloor(max_hold_seconds=None)

    assert check_floor(open_ended, bps=0.0, held=1e9).allowed is False
    assert "3600" not in open_ended.check(bps_move=0.0, held_seconds=1.0).reason


def test_exit_floor_needs_both_bounds_to_deny():
    """One bound missing leaves no band to sit in — fail open, never trap a position."""
    assert check_floor(ExitFloor(min_profit_bps=None), bps=5.0).allowed is True
    assert check_floor(ExitFloor(stop_bps=None), bps=-5.0).allowed is True


def test_every_exit_floor_field_none_allows_everything():
    dead = ExitFloor(min_profit_bps=None, stop_bps=None, max_hold_seconds=None)

    for bps in (-1000.0, -25.0, 0.0, 7.3, 50.0, 1000.0):
        assert check_floor(dead, bps=bps, held=1.0).allowed is True


def test_exit_floor_fails_open_on_a_number_it_cannot_read():
    """A missing or non-finite move is not evidence of a scratch exit."""
    assert check_floor(ExitFloor(), bps=float("nan")).allowed is True
    assert check_floor(ExitFloor(), bps=float("inf")).allowed is True
    assert check_floor(ExitFloor(), bps=0.0, held=float("nan")).allowed is True


def test_shipped_exit_floor_defaults_cover_the_measured_scratch_exits():
    floor = ExitFloor()

    assert floor.min_profit_bps == 100.0
    assert floor.stop_bps == 50.0
    assert floor.max_hold_seconds == 3600.0


# ── is_strict_reduce: the one definition of "this fill closes" ───
#
# Three gates need the same answer and used to compute it three different ways: the
# churn guard (`abs(pos_after) < abs(pos_now)`, which is true for a *smaller* flip and
# waved it through), the exit floor (sign test + closes an old lot), and the venue
# minimum notional. A flip that shrinks the book is a re-entry, not an exit, and only
# one predicate should decide that.


def test_strict_reduce_accepts_closing_and_trimming():
    assert is_strict_reduce(8.8, 8.8, "sell") is True  # close a long
    assert is_strict_reduce(8.8, 3.0, "sell") is True  # trim a long
    assert is_strict_reduce(-8.8, 8.8, "buy") is True  # close a short
    assert is_strict_reduce(-8.8, 2.0, "buy") is True  # trim a short


def test_strict_reduce_rejects_any_flip():
    """Past zero the fill opens a position, however much smaller it leaves the book."""
    assert is_strict_reduce(8.8, 11.0, "sell") is False
    assert is_strict_reduce(-8.8, 11.0, "buy") is False


def test_strict_reduce_rejects_adding_and_opening():
    assert is_strict_reduce(8.8, 1.0, "buy") is False
    assert is_strict_reduce(0.0, 1.0, "sell") is False


def test_strict_reduce_treats_dust_as_flat():
    assert is_strict_reduce(1e-12, 1e-12, "sell") is False
    assert is_strict_reduce(float("nan"), 1.0, "sell") is False
