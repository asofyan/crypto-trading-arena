"""Scorecard for a guard experiment, and the verdict that ends it.

The guards in ``arena.trade_policy`` cap what an agent may spend. They do not make a
strategy profitable, and they are not supposed to. So a run with the guards on needs a
different question than "did it make money" — over two or three hours the answer to
that question is statistical noise, and pretending otherwise is how an operator kills a
working idea (or keeps a dead one) on a coin flip.

What a short run *can* answer is whether the target is reachable at all. A position
either touches its +50 bps target before it times out or it does not, and that is a
property of the price path during the hold, not of the agent's luck. Six round trips
that never come within shouting distance of the target is evidence; six coin flips
that broke 40/60 is not.

Two numbers frame everything below:

* Break-even on the 100/50 band with a 10 bps round trip is **40.0%** (the exit floor's
  own defaults; a narrower band needs far more edge, a wider one less). At n=60 a Wilson
  95% interval around 40% still spans roughly 28-53%, so the break-even sits *inside*
  it: a two-hour run cannot confirm profitability, and cannot refute it either.
* Three round trips an hour (the 6-fill cap over two fills) yields about six to nine
  observations in that window.

Hence the split: :func:`verdict` screens for **broken mechanics** and an
**unreachable target**. Profitability is a later gate, and the report says so rather
than inventing a p-value it does not have.

Pure module: no config import, no clock, no I/O. The CLI in
``scripts/experiment_report.py`` does the reading.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

# The band the exit floor enforces, repeated here rather than imported so a scorecard
# always states the numbers it judged — a report that silently followed a config edit
# would mislabel yesterday's fills.
DEFAULT_TARGET_BPS = 100.0
DEFAULT_STOP_BPS = 50.0
DEFAULT_HOLD_LIMIT_S = 3600.0

# A position this far past the hold limit means the escape hatch did not fire.
TRAPPED_SLACK_S = 300.0

# Six round trips is the smallest n where "none of them reached the target" still says
# something: at p=0.5 that is 1.6% likely to happen by chance alone.
MIN_ROUND_TRIPS = 6


@dataclass(frozen=True)
class RoundTrip:
    """One closed position, measured against the band it had to clear."""

    product_id: str
    entry_ts: float
    exit_ts: float
    direction: int  # +1 long, -1 short
    entry_price: float
    exit_price: float
    net_pnl: float
    fees: float
    hold_s: float
    mfe_bps: float = 0.0  # best the price ever got, in our favour
    mae_bps: float = 0.0  # worst it ever got, against us (positive number)

    @property
    def bps(self) -> float:
        """Realised move, in bps of the position's entry price."""
        if self.entry_price <= 0:
            return 0.0
        return (self.exit_price - self.entry_price) / self.entry_price * 1e4 * self.direction


@dataclass
class Scorecard:
    """Everything a stop/continue decision needs, measured once."""

    window_s: float = 0.0
    equity: float = 0.0
    fills: int = 0
    round_trips: list[RoundTrip] = field(default_factory=list)
    denials_churn: int = 0
    denials_exit_floor: int = 0
    target_bps: float = DEFAULT_TARGET_BPS
    stop_bps: float = DEFAULT_STOP_BPS
    hold_limit_s: float = DEFAULT_HOLD_LIMIT_S
    fee_budget_pct_per_hour: float = 0.25

    @property
    def total_fees(self) -> float:
        return sum(t.fees for t in self.round_trips) + self._open_fees

    # Filled in by the caller when it knows about fees that never closed.
    _open_fees: float = 0.0

    @property
    def net_pnl(self) -> float:
        return sum(t.net_pnl for t in self.round_trips)

    @property
    def target_hits(self) -> int:
        return sum(1 for t in self.round_trips if t.mfe_bps >= self.target_bps)

    @property
    def stop_hits(self) -> int:
        return sum(1 for t in self.round_trips if t.bps <= -self.stop_bps)

    @property
    def inside_band(self) -> int:
        """Closes that landed between the stop and the target. Must stay zero."""
        return sum(
            1
            for t in self.round_trips
            if -self.stop_bps < t.bps < self.target_bps and t.hold_s < self.hold_limit_s
        )

    @property
    def trapped(self) -> int:
        return sum(1 for t in self.round_trips if t.hold_s > self.hold_limit_s + TRAPPED_SLACK_S)

    @property
    def fill_rate_per_hour(self) -> float:
        return self.fills / (self.window_s / 3600.0) if self.window_s > 0 else 0.0

    @property
    def fee_pct_per_hour(self) -> float:
        if self.window_s <= 0 or self.equity <= 0:
            return 0.0
        return self.total_fees / self.equity * 100.0 / (self.window_s / 3600.0)


def classify_exit(
    bps: float,
    hold_s: float,
    *,
    target_bps: float = DEFAULT_TARGET_BPS,
    stop_bps: float = DEFAULT_STOP_BPS,
    hold_limit_s: float = DEFAULT_HOLD_LIMIT_S,
) -> str:
    """Why this close was allowed: ``target``, ``stop``, ``escape``, or ``inside-band``.

    ``inside-band`` is the one that should be impossible with the exit floor armed, so
    it is named rather than folded into "other": a report that hides it cannot tell a
    working guard from a bypassed one.
    """
    if bps >= target_bps:
        return "target"
    if bps <= -stop_bps:
        return "stop"
    if hold_s >= hold_limit_s:
        return "escape"
    return "inside-band"


def excursion(
    prices: list[float],
    entry_price: float,
    direction: int,
) -> tuple[float, float]:
    """Best and worst move in bps seen during the hold: ``(mfe, mae)``.

    ``mfe`` is how close the position came to its target; ``mae`` is how close it came
    to its stop, reported as a positive distance. Both are relative to the entry price
    so they are comparable across products.
    """
    if not prices or entry_price <= 0:
        return 0.0, 0.0
    moves = [(p - entry_price) / entry_price * 1e4 * direction for p in prices]
    return max(moves), -min(moves)


def verdict(card: Scorecard) -> tuple[str, list[str]]:
    """``(decision, reasons)`` where decision is stop / continue / inconclusive.

    Ordering matters and is deliberate: a leak in the guard is reported as a stop
    before any strategy question is asked, because a run with a broken guard measures
    nothing — its numbers are not evidence about the strategy, they are evidence about
    the code.
    """
    reasons: list[str] = []
    target = card.target_bps

    # 1. Mechanics. These are binary facts, not statistics.
    if card.inside_band:
        reasons.append(
            f"{card.inside_band} close(s) landed inside the {card.stop_bps:g}/"
            f"{card.target_bps:g} band while the hold limit had not passed — the exit "
            "floor did not fire, so this run measures nothing."
        )
    if card.trapped:
        reasons.append(
            f"{card.trapped} position(s) sat more than "
            f"{TRAPPED_SLACK_S:.0f}s past the {card.hold_limit_s:g}s hold limit — the "
            "escape hatch is not reached."
        )
    if card.fee_pct_per_hour > card.fee_budget_pct_per_hour:
        reasons.append(
            f"Fee burn {card.fee_pct_per_hour:.3f}%/hour exceeds the "
            f"{card.fee_budget_pct_per_hour:g}%/hour budget — the churn guard is not "
            "binding."
        )
    if reasons:
        return "stop", reasons

    # 2. Enough observations to say anything at all.
    n = len(card.round_trips)
    if n < MIN_ROUND_TRIPS:
        return "inconclusive", [
            f"Only {n} round trip(s) in {card.window_s / 3600:.1f}h; this needs at least "
            f"{MIN_ROUND_TRIPS} before the target-reach question has an answer. Let it "
            "run longer rather than deciding on noise."
        ]

    # 3. Reachability of the target — the question a short run can actually answer.
    if card.target_hits == 0:
        reasons.append(
            f"None of {n} round trip(s) came within {target:g} bps of profit, even at "
            f"their best moment. The target is not reachable on this horizon, so no win "
            "rate can rescue it."
        )
        return "stop", reasons

    # 4. Loss beyond what the fees explain. Below that, the strategy is merely paying
    #    the venue; beyond it, the calls themselves are wrong.
    if card.net_pnl <= -2.0 * max(card.total_fees, 1e-9):
        reasons.append(
            f"Net ${card.net_pnl:+.2f} against ${card.total_fees:.2f} of fees — the loss "
            "is larger than the cost of trading, so the entries and exits are the "
            "problem, not the venue."
        )
        return "stop", reasons

    if card.target_hits >= 2 and card.net_pnl >= 0:
        reasons.append(
            f"{card.target_hits}/{n} round trip(s) reached the {target:g} bps target and "
            f"net P&L is ${card.net_pnl:+.2f}. The mechanics hold and the target is "
            "reachable — keep going, but profitability needs far more samples than this."
        )
        return "continue", reasons

    reasons.append(
        f"{card.target_hits}/{n} reached the target, net ${card.net_pnl:+.2f}. Not enough "
        "to stop on and not enough to continue on: run it longer."
    )
    return "inconclusive", reasons


def sharpe_like(card: Scorecard) -> float | None:
    """Mean net P&L per round trip over its standard deviation.

    Reported for context only and never used by :func:`verdict`: at these sample sizes
    it is a description of the run, not an estimate of an edge. ``None`` when the run
    has too few trips or no variance to divide by.
    """
    pnls = [t.net_pnl for t in card.round_trips]
    if len(pnls) < 3:
        return None
    mean = sum(pnls) / len(pnls)
    var = sum((x - mean) ** 2 for x in pnls) / (len(pnls) - 1)
    if var <= 0 or not math.isfinite(var):
        return None
    return mean / math.sqrt(var)
