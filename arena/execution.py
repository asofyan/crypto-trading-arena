"""Execution seam: who decides *that* a trade filled, and at what price.

Bookkeeping and fill source are separate concerns. ``PaperExecutor`` fills
immediately against the shared price book (best ask on a buy, best bid on a sell)
and charges the configured taker fee; a live executor will ask the venue and hand
back what actually happened. Both call ``AccountStore.apply_fill``, so cash, cost
basis, realised P&L, the trade log and the dashboard stay identical across modes —
the only difference is where the price and the fee come from.

Perpetual-futures products (``*-PERP``) are priced the same way — a sell still
crosses to the bid — but they are charged the futures taker rate, and ``sell`` opens
a short rather than selling inventory (the account store branches on the product id).
"""

from __future__ import annotations

import logging
import math
import re
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Protocol
from uuid import uuid4

from arena.instruments import SymbolRules, is_perp_product
from arena.models import TradeResult
from arena.risk import PortfolioSnapshot, RiskGates
from exchanges.binance_spot import (
    BinanceApiError,
    BinanceSpotClient,
    InsufficientBalance,
    IpBanned,
    OrderFilterError,
    RateLimited,
    UnknownOutcome,
)

# The executor reads its collaborators off the store, so only the annotation needs
# the store type — no runtime import cycle between the two modules.
if TYPE_CHECKING:
    from arena.account_store import AccountStore
    from arena.ledger import Ledger

log = logging.getLogger(__name__)

# Below this, a position is float dust rather than a holding.
_QUANTITY_DUST = 1e-9


@dataclass(frozen=True)
class FillRequest:
    """One trade intent, before anything is priced or booked."""

    agent_id: str
    product_id: str
    quantity: float
    action: str
    latency: float | None = None
    # A replay books a fill that already happened (the monitor's CSV): it must not be
    # re-gated against a price book the replay only partially knows.
    replay: bool = False


class Executor(Protocol):
    """Turns an intent into a fill: priced locally (paper) or by the venue (live)."""

    def execute(self, request: FillRequest) -> TradeResult: ...


class PaperExecutor:
    """Immediate fill at the current touch, charged the configured taker fee.

    The store's write lock is held across validation *and* booking so two concurrent
    tool calls cannot both pass the cash check against the same starting balance.
    """

    def __init__(self, store: AccountStore) -> None:
        self._store = store

    def execute(self, request: FillRequest) -> TradeResult:
        with self._store.write_lock:
            return self._execute(request)

    def _execute(self, request: FillRequest) -> TradeResult:
        product_id = request.product_id.upper().strip()
        action = request.action.lower().strip()
        quantity = request.quantity
        log.debug(
            "paper_executor.execute: agent=%s action=%s product=%s qty=%s latency=%s",
            request.agent_id, action, product_id, quantity, request.latency,
        )

        if action not in ("buy", "sell"):
            log.debug("paper_executor.execute REJECT: invalid action=%s", action)
            return TradeResult(False, f"Invalid action '{action}'. Must be 'buy' or 'sell'.")

        entry = self._store.price_book.get(product_id)
        if entry is None:
            available = ", ".join(sorted(self._store.price_book.snapshot().keys()))
            log.debug(
                "paper_executor.execute REJECT: no price for %s (available: %s)",
                product_id, available,
            )
            return TradeResult(
                False,
                f"No live price for '{product_id}'. "
                f"Available: {available or 'none (waiting for price data)'}",
            )

        if not math.isfinite(quantity) or quantity <= 0:
            log.debug("paper_executor.execute REJECT: non-positive qty=%s", quantity)
            return TradeResult(False, "Quantity must be positive.")

        # Market scope: a strategy written for spot has no vocabulary for a short, and a
        # perp strategy reasons about leverage a spot agent cannot see. Enforced here so
        # a confused model cannot act outside its mandate (see AccountStore agent_markets).
        market = "perp" if is_perp_product(product_id) else "spot"
        if not self._store.agent_allows(request.agent_id, market):
            allowed = self._store.agent_markets(request.agent_id) or ()
            return TradeResult(
                False,
                f"{request.agent_id} may only trade {', '.join(allowed) or 'no markets'}; "
                f"{product_id} is a {market} product. Leave it to the agent that owns "
                f"that market.",
            )

        fee_model = self._store.fee_model
        # Perps pay the futures taker rate; spot keeps the configured spot rate. The
        # product id is the only input, so ledger replay and the monitor agree too.
        perp = is_perp_product(product_id)
        price = float(entry["best_ask"] if action == "buy" else entry["best_bid"])

        # Venue rules when the symbol is known (Binance), else the coarse legacy rule:
        # a deployment trading Coinbase products, or one started without an instrument
        # cache, must keep working exactly as before.
        rules = self._store.instrument_rules.get(product_id)
        if rules is not None:
            quantized = rules.quantize(quantity)
            if quantized is None:
                return TradeResult(
                    False,
                    f"{product_id} trades in steps of {rules.step_size:g} with a minimum of "
                    f"{rules.min_qty:g}; {quantity:g} is not orderable. "
                    f"Round down to a multiple of {rules.step_size:g}.",
                )
            if not rules.notional_ok(quantized, price):
                return TradeResult(
                    False,
                    f"Order notional ${float(quantized) * price:,.2f} is below the venue "
                    f"minimum of {rules.min_notional:g} USDT; increase the quantity.",
                )
            order_qty = float(quantized)
        else:
            order_qty = round(quantity, 1)
            if abs(quantity - order_qty) > 1e-9:
                log.debug("paper_executor.execute REJECT: qty precision qty=%s", quantity)
                return TradeResult(
                    False, "Quantity must have at most 1 decimal place (e.g., 0.5, 1.2)."
                )
            if order_qty == 0:
                log.debug("paper_executor.execute REJECT: qty rounds to zero qty=%s", quantity)
                return TradeResult(False, "Quantity must be positive.")

        if action == "buy":
            fee = fee_model.buy_cost(price * order_qty, perp=perp).fee
        else:
            fee = fee_model.sell_proceeds(price * order_qty, perp=perp).fee

        return self._store.apply_fill(
            request.agent_id,
            action,
            product_id,
            order_qty,
            price,
            fee,
            latency=request.latency,
            fee_bps=fee_model.taker_bps_for(perp=perp),
            enforce_margin=not request.replay,
        )


class LiveBinanceExecutor:
    """Places real Binance Spot market orders, in this order: journal, venue, numbers.

    The venue's own answer is what gets booked — ``executedQty``, ``cummulativeQuoteQty``
    and ``commission`` from the fill response — so the ledger describes what actually
    happened rather than what was intended. A MARKET order is immediate-or-nothing,
    which is what lets this skip a user-data stream: there are no resting orders to
    leak into the next session.
    """

    def __init__(
        self,
        store: AccountStore,
        client: BinanceSpotClient,
        ledger: Ledger,
        *,
        account: str,
        agent_accounts: dict[str, str],
        rules: dict[str, SymbolRules],
        gates: RiskGates,
        dry_run: bool = True,
        quote_asset: str = "USDT",
        max_price_slippage_bps: int = 50,
        on_fatal: Callable[[Exception], None] | None = None,
        now: Callable[[], float] = time.time,
        id_factory: Callable[[str], str] | None = None,
    ) -> None:
        self._store = store
        self._client = client
        self._ledger = ledger
        self._account = account
        self._mapped = frozenset(
            agent for agent, mapped_account in agent_accounts.items() if mapped_account == account
        )
        self._rules = dict(rules)
        self._gates = gates
        self._dry_run = dry_run
        self._quote_asset = quote_asset
        # The pre-check must leave room for the venue filling a little above the touch,
        # otherwise a real fill gets refused after the money already moved.
        self._slippage = 1.0 + max_price_slippage_bps / 10_000.0
        self._on_fatal = on_fatal
        self._now = now
        self._id_factory = id_factory or _default_client_order_id

    @property
    def dry_run(self) -> bool:
        return self._dry_run

    @property
    def client(self) -> BinanceSpotClient:
        return self._client

    @property
    def rules(self) -> dict[str, SymbolRules]:
        return dict(self._rules)

    def execute(self, request: FillRequest) -> TradeResult:
        agent_id = request.agent_id
        symbol = request.product_id.upper().strip()
        side = request.action.lower().strip()

        if side not in ("buy", "sell"):
            return TradeResult(False, f"Invalid action '{side}'. Must be 'buy' or 'sell'.")
        if agent_id not in self._mapped:
            return TradeResult(
                False,
                f"Live trading is not enabled for '{agent_id}'. Only "
                f"{', '.join(sorted(self._mapped)) or 'no agent'} may place real orders.",
            )

        rules = self._rules.get(symbol)
        if rules is None:
            return TradeResult(
                False,
                f"{symbol} is not enabled for live trading (configured symbols: "
                f"{', '.join(sorted(self._rules)) or 'none'}).",
            )

        entry = self._store.price_book.get(symbol)
        if entry is None:
            available = ", ".join(sorted(self._store.price_book.snapshot().keys()))
            return TradeResult(
                False, f"No live price for '{symbol}' yet (available: {available or 'none'})."
            )
        price = float(entry["best_ask"] if side == "buy" else entry["best_bid"])

        received_at = entry.get("_received_at")
        if received_at is None:
            # Refusing is the only safe answer: a quote whose age cannot be measured may
            # be minutes old, and a market order on it is a market order on a stale price.
            return TradeResult(
                False,
                f"No quote age for {symbol}; refusing to trade on a price of unknown age.",
            )
        price_age_s = max(0.0, self._now() - float(received_at))

        quantized = rules.quantize(request.quantity)
        if quantized is None:
            return TradeResult(
                False,
                f"{symbol} trades in steps of {rules.step_size:g} with a minimum of "
                f"{rules.min_qty:g}; {request.quantity:g} is not orderable.",
            )
        quantity = float(quantized)
        if not rules.notional_ok(quantized, price):
            return TradeResult(
                False,
                f"Order notional ${quantity * price:,.2f} is below the venue minimum of "
                f"{rules.min_notional:g} {self._quote_asset}; increase the quantity.",
            )

        decision = self._gates.check(
            symbol=symbol,
            side=side,
            quantity=quantity,
            price=price,
            price_age_s=price_age_s,
            portfolio=self._portfolio(agent_id, symbol, price),
        )
        if not decision.allowed:
            # Deliberately not journaled: a denied order never reached the venue, and the
            # journal is a record of money-moving intents. The agent gets the reason.
            log.info("live_executor DENY: agent=%s %s %s %s — %s",
                     agent_id, side, quantity, symbol, decision.reason)
            return TradeResult(False, decision.reason)

        notional = quantity * price

        # The account must be able to book what the venue is about to do: a fill the
        # ledger later refuses would leave the venue and the ledger permanently apart.
        # Buys are checked with slippage headroom (the venue may fill above the touch),
        # sells against the held quantity (the venue would happily sell what we cannot
        # account for).
        account = self._store.get_or_create(agent_id)
        if side == "buy":
            cash_out, _ = self._store.fee_model.buy_cost(notional * self._slippage)
            if cash_out > account.cash:
                return TradeResult(
                    False,
                    f"Insufficient cash. Need ${cash_out:,.2f} ({self._slippage - 1:.2%} "
                    f"slippage buffer included) but only have ${account.cash:,.2f}.",
                )
        else:
            held = account.positions.get(symbol, 0.0)
            if quantity > held + _QUANTITY_DUST:
                return TradeResult(
                    False,
                    f"Insufficient holdings. Want to sell {quantity:g} {symbol} but only "
                    f"hold {held:g}.",
                )

        client_order_id = self._id_factory(agent_id)
        self._ledger.save_order(
            client_order_id=client_order_id,
            agent_id=agent_id,
            account=self._account,
            symbol=symbol,
            side=side,
            state="DRY_RUN" if self._dry_run else "PENDING",
            qty_requested=quantity,
            quote_qty_requested=notional,
        )

        if self._dry_run:
            self._ledger.update_order(
                client_order_id, price=price, error="dry run: venue not contacted"
            )
            return TradeResult(
                False,
                f"DRY-RUN: {side} {quantity:g} {symbol} (~${notional:,.2f}) was not sent; "
                f"no position changed.",
            )

        try:
            if side == "buy":
                fill = self._client.place_market_buy(symbol, notional, client_order_id)
            else:
                fill = self._client.place_market_sell(symbol, quantity, client_order_id)
        except UnknownOutcome as exc:
            # The POST may or may not have landed. Retrying could double the position,
            # so the intent stays resolvable by id and the next startup settles it.
            self._ledger.update_order(client_order_id, state="UNKNOWN", error=str(exc))
            log.error("live_executor: outcome unknown for %s: %s", client_order_id, exc)
            return TradeResult(
                False,
                f"Order outcome unknown ({exc}). Do NOT retry: it will be reconciled "
                f"against the venue on the next start (id {client_order_id}).",
            )
        except (RateLimited, IpBanned) as exc:
            self._ledger.update_order(client_order_id, state="REJECTED", error=str(exc))
            log.error("live_executor: %s for %s", type(exc).__name__, client_order_id)
            if isinstance(exc, IpBanned) and self._on_fatal is not None:
                self._on_fatal(exc)
            return TradeResult(False, f"Venue refused the request: {exc}. No order was filled.")
        except BinanceApiError as exc:
            self._ledger.update_order(
                client_order_id, state="REJECTED", error=f"[{exc.code}] {exc.message}"
            )
            log.warning(
                "live_executor: rejected %s %s %s (%s)",
                side, quantity, symbol, exc,
            )
            return TradeResult(False, _explain_rejection(exc))

        self._gates.record_order()

        if not fill.is_filled:
            # Accepted then killed by the venue (EXPIRED, or fully canceled): nothing or
            # only part of it traded, and the partial part is booked below.
            self._ledger.update_order(
                client_order_id,
                state=fill.status or "EXPIRED",
                venue_order_id=fill.venue_order_id,
                executed_qty=fill.executed_qty,
                cum_notional=fill.cum_notional,
                price=fill.price,
            )
            if not fill.is_filled:
                return TradeResult(
                    False,
                    f"Order {fill.status or 'EXPIRED'} at the venue: "
                    f"{fill.executed_qty:g} {symbol} filled — no position change.",
                )

        booked_quantity = fill.credited_qty if side == "buy" else fill.executed_qty
        if booked_quantity <= 0 or fill.price <= 0:
            self._ledger.update_order(
                client_order_id,
                state="UNKNOWN",
                venue_order_id=fill.venue_order_id,
                executed_qty=fill.executed_qty,
                cum_notional=fill.cum_notional,
                error="venue reported a fill with no usable quantity or price",
            )
            log.error(
                "live_executor: unusable fill for %s (qty=%s price=%s)",
                client_order_id, fill.executed_qty, fill.price,
            )
            return TradeResult(
                False,
                f"The venue reported {fill.executed_qty:g} {symbol} at {fill.price:g}, which "
                f"is not bookable; the ledger will reconcile on the next start.",
            )

        result = self._store.apply_fill(
            agent_id,
            side,
            symbol,
            booked_quantity,
            fill.price,
            fill.fee,
            latency=request.latency,
            fee_bps=None,  # the venue's commission is the truth; no estimate to quote
            client_order_id=client_order_id,
            fee_asset=fill.fee_asset,
            venue_order_id=fill.venue_order_id,
        )
        log.info(
            "live_executor: %s %s %s @ %.8f fee=%.6f %s → %s",
            side, booked_quantity, symbol, fill.price, fill.fee,
            fill.fee_asset or "?", result.success,
        )
        return result

    # ── crash recovery ──────────────────────────────────────────

    def settle_dangling(self) -> list[tuple[str, str]]:
        """Resolve every order whose outcome this process never learned.

        Runs before the first comparison at startup, so a crash between "POST sent" and
        "fill booked" cannot leave the ledger and the wallet disagreeing. Booking is
        idempotent per ``client_order_id``, so re-running this is free.

        Returns ``(client_order_id, outcome)`` pairs for the log.
        """
        resolved: list[tuple[str, str]] = []
        for order in self._ledger.dangling_orders():
            try:
                fill = self._client.get_order(order.symbol, order.client_order_id)
            except Exception as exc:  # noqa: BLE001 — a lookup failure must not book anything
                log.error(
                    "live_executor: could not resolve %s: %s", order.client_order_id, exc
                )
                resolved.append((order.client_order_id, "lookup-failed"))
                continue

            if fill is None:
                # The venue has never seen this id: the POST never landed. Nothing to
                # book, and marking it expired frees the journal for the next start.
                self._ledger.update_order(
                    order.client_order_id,
                    state="EXPIRED",
                    error="venue has no record of this order; the POST never landed",
                )
                resolved.append((order.client_order_id, "never-landed"))
                continue

            if not fill.is_filled:
                self._ledger.update_order(
                    order.client_order_id,
                    state=fill.status or "EXPIRED",
                    venue_order_id=fill.venue_order_id,
                    executed_qty=fill.executed_qty,
                    cum_notional=fill.cum_notional,
                    price=fill.price,
                )
                resolved.append((order.client_order_id, fill.status.lower() or "expired"))
                continue

            booked_quantity = fill.credited_qty if order.side.upper() == "BUY" else fill.executed_qty
            if booked_quantity <= 0 or fill.price <= 0:
                self._ledger.update_order(
                    order.client_order_id,
                    state="UNKNOWN",
                    venue_order_id=fill.venue_order_id,
                    executed_qty=fill.executed_qty,
                    cum_notional=fill.cum_notional,
                    error="fill has no usable quantity or price",
                )
                resolved.append((order.client_order_id, "unusable-fill"))
                continue

            result = self._store.apply_fill(
                order.agent_id,
                order.side.lower(),
                order.symbol,
                booked_quantity,
                fill.price,
                fill.fee,
                client_order_id=order.client_order_id,
                fee_asset=fill.fee_asset,
                venue_order_id=fill.venue_order_id,
            )
            if result.success:
                resolved.append((order.client_order_id, "booked"))
                continue

            # The fill happened at the venue but the ledger refuses it (a sell of a
            # position the ledger no longer holds, say). Leave the row unresolved so the
            # next comparison reports the shortfall and the session stays disarmed.
            self._ledger.update_order(
                order.client_order_id,
                state="UNKNOWN",
                venue_order_id=fill.venue_order_id,
                executed_qty=fill.executed_qty,
                cum_notional=fill.cum_notional,
                error=f"venue filled but the ledger refused it: {result.message}",
            )
            log.error(
                "live_executor: %s filled at the venue but could not be booked: %s",
                order.client_order_id, result.message,
            )
            resolved.append((order.client_order_id, "unbookable"))

        if resolved:
            log.warning("live_executor: settled dangling orders: %s", resolved)
        return resolved

    # ── helpers ─────────────────────────────────────────────────

    def _portfolio(self, agent_id: str, symbol: str, price: float) -> PortfolioSnapshot:
        """Account-level equity plus this agent's position, marked at the live price."""
        equity = 0.0
        for mapped_agent in self._mapped:
            account = self._store.get_or_create(mapped_agent)
            equity += account.cash
            for product_id, quantity in account.positions.items():
                entry = self._store.price_book.get(product_id)
                mark = float(entry["price"]) if entry is not None else account.avg_cost_per_unit(
                    product_id
                )
                equity += quantity * mark

        session_start = self._session_start_equity(equity)
        position = self._store.get_or_create(agent_id).positions.get(symbol, 0.0)
        return PortfolioSnapshot(
            equity_usdt=equity,
            session_start_equity_usdt=session_start,
            position_notional_usdt=position * price,
            # The gate sums these two; account-level equity delta is the honest figure
            # for "how much has this session lost" (it already includes unrealised P&L).
            realised_pnl_usdt=equity - session_start,
            unrealised_pnl_usdt=0.0,
        )

    def _session_start_equity(self, equity: float) -> float:
        """Stamped once, so the daily-loss budget survives restarts within the session."""
        stored = self._ledger.get_meta("live_session_start_equity")
        if stored is not None:
            try:
                value = float(stored)
                if value > 0:
                    return value
            except ValueError:
                log.warning("live_session_start_equity is not a number (%r); restamping", stored)
        self._ledger.set_meta("live_session_start_equity", repr(equity))
        return equity


def _default_client_order_id(agent_id: str) -> str:
    """A durable, venue-legal id (<=36 chars, [A-Za-z0-9-]) that is ours to look up."""
    slug = re.sub(r"[^A-Za-z0-9-]", "", agent_id).strip("-")[:10] or "agent"
    return f"arena-{slug}-{uuid4().hex[:12]}"


def _explain_rejection(exc: BinanceApiError) -> str:
    """Turn a venue error into something the agent can act on immediately."""
    if isinstance(exc, InsufficientBalance):
        return (
            "The venue rejected the order: insufficient balance for this size. "
            "Reduce the quantity (fees are charged on top of the notional)."
        )
    if isinstance(exc, OrderFilterError):
        return (
            f"The venue refused the size: {exc.message}. Check the symbol's step size "
            "and minimum notional, then retry with a legal quantity."
        )
    return f"The venue rejected the order [{exc.code}]: {exc.message}"
