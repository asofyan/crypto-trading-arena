"""Turnover guard: refuse trades whose fees the account cannot earn back.

One scalper-perp agent put 140 fills through a $100 wallet in two hours. The taker
fees came to $2.06 — 2% of equity — against $0.10 of gross P&L. Nothing in the order
path objected, and nothing *could*: every one of those fills was individually legal
(small, funded, inside the size caps). The bleed was in the **rate**, and a per-order
gate has no memory, so it cannot see a rate.

This module supplies the memory. It answers one question — "is this agent trading too
often, or paying too much, to keep what it is trying to make?" — over a rolling hour,
and it refuses only the trades that *add* exposure. Two rules are load-bearing:

* **Reducing a position is always allowed**, checked before anything else. Blocking an
  exit to save a fee is how a small loss becomes a liquidation; no fee is worth that.
* **The fill counter includes every fill, reduces included.** The budget protects the
  wallet, not the strategy: a round trip costs two slots because it costs two fees.

Budgets are per agent (the fee bleed is a property of the wallet). The re-entry
cooldown is per ``(agent, product)`` — it is about the same idea repeated, so a patient
agent may still open a different product.

Deliberately pure, like ``arena.risk``: limits are injected, the clock is injected,
nothing here imports ``arena.config`` or touches the network. The windows are then
testable to the second and behave identically in paper and live. The clock is
``time.monotonic`` because a rolling window must not jump when NTP steps the wall
clock.

The guard only narrows what an agent may do; it never widens it. Cash, holdings and the
1:1 cap stay where they are (``arena.account_store``), so there is exactly one owner
per rule.

``ExitFloor`` below is the second half of the same story. The churn guard stops the
*rate* of trading; the exit floor stops the *content*: a position closed while its
unrealised move sits between the stop and the target. 29 of 111 measured exit legs
closed at 0-10 bps of profit against a 10 bps round trip, median move 0.00 bps — two
fees paid to end up where the position started. Neither a per-order nor a per-hour gate
can see that, because the trade is legal and cheap on its own; only the distance from
the entry price can. The floor therefore owns no account state either: the store hands
it ``bps_move`` (from the account's cost basis) and ``held_seconds`` (from the account's
average entry time) and gets a verdict back.
"""

from __future__ import annotations

import math
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable

# Every rate limit below is measured over this window; named so the deny reasons and
# the pruning cannot drift apart.
_WINDOW_SECONDS = 3600.0

# The same epsilon the store uses to call a position flat: a float residue is not a
# position, and a fill that closes 1e-12 of a lot is not a reduce.
_QTY_EPSILON = 1e-9


def is_strict_reduce(position: float, quantity: float, action: str) -> bool:
    """Does this fill end smaller, on the same side it started?

    The one definition three gates need and used to compute three different ways. The
    churn guard asked ``abs(pos_after) < abs(pos_now)``, which is true for a *smaller*
    flip — selling 1.5 against a long of 1.0 closes 1.0 and opens a short of 0.5, and the
    guard waved that re-entry through one second after the entry, past the cooldown, the
    fill budget and the fee budget. A flip is a decision to be short, not an exit.

    Reducing is closing, trimming, or shrinking a short. Opening, adding, and any fill
    that crosses zero are not.
    """
    if not (math.isfinite(position) and math.isfinite(quantity)):
        return False
    if abs(position) <= _QTY_EPSILON:
        return False
    delta = quantity if action == "buy" else -quantity
    if (position > 0) == (delta > 0):
        return False  # same side: adding, not reducing
    return abs(delta) <= abs(position) + _QTY_EPSILON


@dataclass(frozen=True)
class ChurnLimits:
    """Operator-set turnover ceilings. ``None`` disables the corresponding limit.

    Defaults come from the incident above: six fills an hour, a minute between
    re-entries in the same product, and a quarter of a percent of equity per hour of
    fee spend (25 bp — the same order of magnitude as a single perp round trip, so the
    budget buys a few real attempts rather than unlimited ones).
    """

    max_fills_per_hour: int | None = 6
    cooldown_seconds: float | None = 60.0
    max_fee_burn_pct_per_hour: float | None = 0.25


@dataclass(frozen=True)
class ChurnVerdict:
    """``allowed=False`` is always a refusal the agent can act on: it names the number."""

    allowed: bool
    reason: str


_ALLOW = ChurnVerdict(allowed=True, reason="ok")


def _deny(reason: str) -> ChurnVerdict:
    return ChurnVerdict(allowed=False, reason=reason)


class ChurnGuard:
    """Rolling-hour turnover guard for every fill an account store books.

    ``check()`` is a question asked before an order goes out; ``record()`` is the
    answer for a fill that really happened. A denial records nothing, so an order the
    account gates or the venue refused can never consume the budget — the counter
    tracks money actually paid, not intent.
    """

    def __init__(
        self,
        limits: ChurnLimits,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.limits = limits
        self._clock = clock
        # agent_id → (fill time, fee paid), pruned to the last hour on every touch.
        self._fills: dict[str, deque[tuple[float, float]]] = {}
        # (agent_id, product_id) → when that product last filled. Not tied to the
        # window: a cooldown longer than an hour must still hold.
        self._last_fill_at: dict[tuple[str, str], float] = {}

    def check(
        self,
        *,
        agent_id: str,
        product_id: str,
        fee_estimate: float,
        equity: float,
        reduces_exposure: bool,
    ) -> ChurnVerdict:
        """May this fill go out? Ordering: exit, money, rate, re-entry.

        The most consequential reason wins. A blocked exit is a safety question, so it
        is answered first and generously; the fee budget next, because "you have spent
        your hour's fees" is the refusal an agent can act on directly; the fill count
        after that; then the per-product cooldown, which is the most local of the four.
        """
        limits = self.limits

        # Unconditional, and before any bookkeeping: an exit is never the trade that
        # ruins the account, and a fee saved on the way out is not worth a trapped loss.
        if reduces_exposure:
            return _ALLOW

        now = self._clock()
        fills = self._prune(agent_id, now)
        spent = sum(fee for _, fee in fills)

        # The projection matters: a budget that only fires once the hour is *already*
        # overspent has already lost the money it was meant to protect.
        if limits.max_fee_burn_pct_per_hour is not None and math.isfinite(equity) and equity > 0:
            budget = equity * limits.max_fee_burn_pct_per_hour / 100.0
            if spent + fee_estimate > budget:
                addition = f" (this fill adds {_usdt(fee_estimate)})" if fee_estimate > 0 else ""
                return _deny(
                    f"Fee budget spent: {_usdt(spent)} of {_usdt(budget)} allowed per "
                    f"hour on {_usdt(equity)} equity{addition}. Stand down or close, "
                    "do not open."
                )

        if limits.max_fills_per_hour is not None and len(fills) >= limits.max_fills_per_hour:
            wait_s = fills[0][0] + _WINDOW_SECONDS - now
            return _deny(
                f"{len(fills)} fills already in the last hour (limit "
                f"{limits.max_fills_per_hour}); every fill pays the taker fee whether it "
                f"wins or not. Close instead, or wait {wait_s:.0f}s for the window to "
                "roll over."
            )

        if limits.cooldown_seconds is not None:
            last = self._last_fill_at.get(_key(agent_id, product_id))
            if last is not None:
                quiet_for = now - last
                if quiet_for < limits.cooldown_seconds:
                    wait_s = limits.cooldown_seconds - quiet_for
                    return _deny(
                        f"Re-entry cooldown: the last {product_id.strip().upper()} fill "
                        f"was {_seconds(quiet_for)} ago and the cooldown is "
                        f"{_seconds(limits.cooldown_seconds)}. Wait {wait_s:.0f}s before "
                        "opening again, or reduce the position instead — exits are never "
                        "blocked."
                    )

        return _ALLOW

    def record(self, *, agent_id: str, product_id: str, fee: float) -> None:
        """Book a fill that actually happened. Call once per booked fill, never per intent."""
        now = self._clock()
        self._prune(agent_id, now).append((now, fee))
        self._last_fill_at[_key(agent_id, product_id)] = now

    def _prune(self, agent_id: str, now: float) -> deque[tuple[float, float]]:
        """Drop everything that fell out of the hour; the head is always the oldest."""
        fills = self._fills.setdefault(agent_id, deque())
        while fills and fills[0][0] <= now - _WINDOW_SECONDS:
            fills.popleft()
        return fills


def _key(agent_id: str, product_id: str) -> tuple[str, str]:
    """Upper-case the product so `` sol-usd `` and ``SOL-USD`` are one cooldown."""
    return agent_id, product_id.strip().upper()


def _usdt(amount: float) -> str:
    return f"${amount:,.2f}"


def _seconds(value: float) -> str:
    return f"{value:g}s"


@dataclass(frozen=True)
class ExitFloor:
    """The band a close may not land in, in basis points of the position's entry price.

    A close is allowed at or beyond the target, at or beyond the stop, and after the
    hold limit has passed. Everything in between is a scratch exit: the position has
    neither earned its round trip nor admitted the idea was wrong, so the two fees buy
    nothing and the account bleeds at a rate no single fill looks responsible for.

    ``None`` disables a limit, and a missing bound disables the *band* rather than
    inventing one: with no target or no stop there is nothing to sit between, so the
    check allows. ``max_hold_seconds=None`` means no timeout at all — the escape hatch
    is gone, not the floor; an operator who wants the floor gone sets it to ``None`` on
    the store (``AccountStore.set_exit_floor``).

    Pure like ``ChurnGuard``: no config import, no clock, no account state. The caller
    measures the distance from the entry in bps and the holding time in seconds.
    """

    # Calibrated to what a 10 bps round trip can actually earn: a 50/25 band needs ~15
    # points of hit-rate edge over a random walk to break even, a 100/50 band needs ~7.
    min_profit_bps: float | None = 100.0
    stop_bps: float | None = 50.0
    max_hold_seconds: float | None = 3600.0

    def check(self, *, bps_move: float, held_seconds: float) -> ChurnVerdict:
        """May this close go out? Ordering: unreadable, timeout, target, stop, band.

        The timeout is answered before the band, because a position that has sat past
        the limit is stale: refusing its exit forever is how a guard turns a scratch
        into a trapped loser. The target and the stop then answer first among the
        numbers, so the one case that must never be refused — cutting a loss — cannot
        be reached by a rounding surprise in the band comparison.
        """
        target = self.min_profit_bps
        stop = self.stop_bps
        # Fail open on anything we cannot read or compare: a NaN move is a missing
        # quote, not proof of a scratch exit, and one bound alone has no band to sit in.
        if not (math.isfinite(bps_move) and math.isfinite(held_seconds)):
            return _ALLOW
        if target is None or stop is None:
            return _ALLOW

        if self.max_hold_seconds is not None and held_seconds >= self.max_hold_seconds:
            return _ALLOW
        if bps_move >= target:
            return _ALLOW
        if bps_move <= -stop:
            return _ALLOW

        escape = (
            f"or wait until the {_seconds(self.max_hold_seconds)} hold limit"
            if self.max_hold_seconds is not None
            else "or hold it"
        )
        return _deny(
            f"Exit floor: this position is {bps_move:+.1f} bps against a "
            f"+{target:.1f} bps target and a -{stop:.1f} bps stop. Closes between those "
            f"two numbers pay the round trip for nothing. Hold to target, cut at the "
            f"stop, {escape}."
        )
