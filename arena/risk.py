"""Pre-trade guardrail policy for real-money orders.

Sizing stays the agent's decision: this module only refuses what is impossible or
reckless, and each refusal is worded so the agent can fix its own next attempt.
Percentage caps scale with equity (decision D11), so a deposit raises every limit
without a config edit.

Deliberately pure — limits are injected, nothing here imports ``arena.config`` or
touches the network — so the gates are cheap to unit test and behave identically
in paper and live mode. Cash and holdings validation is *not* duplicated here:
``arena.account_store`` owns it, and two copies of "can this account afford it"
could disagree about the same fill.
"""

from __future__ import annotations

import math
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

# Operator kill switch. A file, not a database row: the engine holds the ledger's
# exclusive writer lock, so a second process cannot write the DB but *can* touch a file,
# and ``Path.exists`` costs nothing on the order path.
DEFAULT_HALT_PATH = Path("data/LIVE_HALT")

# The rolling window for max_orders_per_minute; named so the deny reason and the
# pruning logic can never drift apart.
_ORDERS_WINDOW_SECONDS = 60.0


@dataclass(frozen=True)
class LiveLimits:
    """Operator-set ceilings. Only max_order_notional_usdt is an absolute amount."""

    allow_symbols: tuple[str, ...] = ()
    max_order_pct_of_equity: float = 25.0
    max_position_pct_of_equity: float | None = 50.0
    max_order_notional_usdt: float | None = None
    max_daily_loss_pct: float = 10.0
    max_orders_per_minute: int = 2
    max_price_age_seconds: float = 5.0


@dataclass(frozen=True)
class PortfolioSnapshot:
    """Everything the gates need to know about the account, frozen at quote time."""

    equity_usdt: float
    session_start_equity_usdt: float
    position_notional_usdt: float
    realised_pnl_usdt: float
    unrealised_pnl_usdt: float


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str


_ALLOW = Decision(allowed=True, reason="ok")


def _deny(reason: str) -> Decision:
    return Decision(allowed=False, reason=reason)


class RiskGates:
    """Pre-trade checks. One order is described by one ``check()`` call.

    The checks are ordered so the most actionable reason wins: a session-ending
    condition (halt, daily loss) is reported before a size complaint, because
    telling an agent to "reduce size" during a halted session is useless advice.
    """

    def __init__(
        self,
        limits: LiveLimits,
        *,
        is_halted: Callable[[], bool] = lambda: False,
        now: Callable[[], float] = time.time,
    ) -> None:
        self.limits = limits
        self._is_halted = is_halted
        self._now = now
        # Normalised once so a lowercase config entry still matches the agent's
        # symbol, and the per-order lookup stays O(1).
        self._allow = frozenset(s.strip().upper() for s in limits.allow_symbols)
        self._orders: deque[float] = deque()

    def check(
        self,
        *,
        symbol: str,
        side: str,
        quantity: float,
        price: float,
        price_age_s: float,
        portfolio: PortfolioSnapshot,
        armed: bool = True,
    ) -> Decision:
        limits = self.limits

        # ── Session state: nothing else matters while trading is off ──
        if not armed:
            return _deny(
                "trading is not ARMED; an operator must arm the bot before any order "
                "can be placed"
            )
        if self._is_halted():
            return _deny(
                "trading is HALTED (halt flag set); an operator must clear the halt "
                "before any order can be placed"
            )

        # A snapshot we cannot trust would make every later comparison fail open,
        # so refuse to size against it at all.
        if not _finite_snapshot(portfolio):
            return _deny(
                "the portfolio snapshot contains a non-finite value "
                f"(equity={portfolio.equity_usdt!r}, "
                f"position={portfolio.position_notional_usdt!r}); "
                "refusing to size an order against it"
            )
        if portfolio.equity_usdt <= 0:
            return _deny(
                f"account equity is {_usdt(portfolio.equity_usdt)}; refusing to size an "
                "order against a non-positive balance"
            )
        if portfolio.session_start_equity_usdt <= 0:
            return _deny(
                f"session-start equity is {_usdt(portfolio.session_start_equity_usdt)}, so the "
                f"{_pct(limits.max_daily_loss_pct)} daily-loss limit cannot be measured; "
                "check the portfolio snapshot"
            )

        loss_cap = portfolio.session_start_equity_usdt * limits.max_daily_loss_pct / 100.0
        session_pnl = portfolio.realised_pnl_usdt + portfolio.unrealised_pnl_usdt
        if session_pnl <= -loss_cap:
            return _deny(
                f"session P&L is {_usdt(session_pnl)}, at the "
                f"{_pct(limits.max_daily_loss_pct)} daily-loss limit "
                f"({_usdt(loss_cap)} of session-start equity); no further orders this session"
            )

        # ── Instrument allowlist (empty means deny everything, panic-safe) ──
        wanted = symbol.strip().upper()
        if not self._allow:
            return _deny(
                "the live allowlist is empty, so every symbol is denied; an operator must "
                "add a symbol before trading"
            )
        if wanted not in self._allow:
            return _deny(
                f"{wanted} is not in the live allowlist "
                f"(allowed: {', '.join(sorted(self._allow))}); pick a symbol from that list"
            )

        # ── Order shape ──
        if not math.isfinite(quantity) or quantity <= 0:
            return _deny(
                f"quantity must be a positive finite number (got {quantity!r}); recompute the "
                "size from the notional you want"
            )
        if not math.isfinite(price) or price <= 0:
            return _deny(
                f"price must be a positive finite number (got {price!r}); re-quote the symbol "
                "and retry"
            )

        # ── Freshness: a stale quote is a fill at a price that no longer exists ──
        if not math.isfinite(price_age_s):
            return _deny("price age is unknown (non-finite); re-quote the symbol and retry")
        if price_age_s > limits.max_price_age_seconds:
            return _deny(
                f"price is {_seconds(price_age_s)} old, above the "
                f"{_seconds(limits.max_price_age_seconds)} freshness limit; "
                "re-quote the symbol and retry"
            )

        notional = quantity * price

        # ── Sizing caps, as a share of equity so they scale with a top-up ──
        order_cap = portfolio.equity_usdt * limits.max_order_pct_of_equity / 100.0
        if notional > order_cap:
            share = notional / portfolio.equity_usdt * 100.0
            return _deny(
                f"order notional {_usdt(notional)} ({share:.1f}% of equity) exceeds the "
                f"{_pct(limits.max_order_pct_of_equity)} cap ({_usdt(order_cap)}); reduce size"
            )
        if limits.max_order_notional_usdt is not None and (
            notional > limits.max_order_notional_usdt
        ):
            return _deny(
                f"order notional {_usdt(notional)} exceeds the "
                f"{_usdt(limits.max_order_notional_usdt)} per-order ceiling; reduce size"
            )

        if limits.max_position_pct_of_equity is not None:
            position_cap = portfolio.equity_usdt * limits.max_position_pct_of_equity / 100.0
            # A sell can only shrink exposure, and blocking it would trap the agent in
            # a losing position — the one denial that must never fire on an exit.
            if side.strip().lower() == "sell":
                resulting = max(0.0, portfolio.position_notional_usdt - notional)
            else:
                resulting = portfolio.position_notional_usdt + notional
            if resulting > position_cap:
                return _deny(
                    f"position after this order would be {_usdt(resulting)}, above the "
                    f"{_pct(limits.max_position_pct_of_equity)} of equity cap "
                    f"({_usdt(position_cap)}); reduce size"
                )

        # ── Venue-link backstop, checked last: it says nothing about the trade ──
        now = self._now()
        while self._orders and self._orders[0] <= now - _ORDERS_WINDOW_SECONDS:
            self._orders.popleft()
        if len(self._orders) >= limits.max_orders_per_minute:
            wait_s = self._orders[0] + _ORDERS_WINDOW_SECONDS - now
            return _deny(
                f"{len(self._orders)} orders already submitted in the last "
                f"{_seconds(_ORDERS_WINDOW_SECONDS)} (limit {limits.max_orders_per_minute}); "
                f"wait {wait_s:.1f}s before the next one"
            )

        return _ALLOW

    def record_order(self) -> None:
        """Call once an order was actually submitted; denials never consume budget."""
        self._orders.append(self._now())


def _finite_snapshot(portfolio: PortfolioSnapshot) -> bool:
    return all(
        math.isfinite(value)
        for value in (
            portfolio.equity_usdt,
            portfolio.session_start_equity_usdt,
            portfolio.position_notional_usdt,
            portfolio.realised_pnl_usdt,
            portfolio.unrealised_pnl_usdt,
        )
    )


def _usdt(amount: float) -> str:
    return f"${amount:,.2f}"


def _pct(value: float) -> str:
    return f"{value:g}%"


def _seconds(value: float) -> str:
    return f"{value:g}s"
