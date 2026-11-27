"""Binance USDⓈ-M market execution for a dedicated one-way account."""

from __future__ import annotations

import logging
import math
import time
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Callable

from arena.execution import FillRequest, _default_client_order_id, clamp_flip_to_a_close
from arena.instruments import SymbolRules, is_perp_product, venue_symbol
from arena.models import OrderRecord, TradeResult
from arena.risk import PortfolioSnapshot, RiskGates
from arena.trade_policy import is_strict_reduce
from exchanges.binance_spot import BinanceApiError, IpBanned, RateLimited, UnknownOutcome, VenueFill

if TYPE_CHECKING:
    from arena.account_store import AccountStore
    from arena.ledger import Ledger
    from exchanges.binance_futures import BinanceFuturesClient

log = logging.getLogger(__name__)
_TERMINAL_STATES = frozenset({"FILLED", "EXPIRED", "EXPIRED_IN_MATCH", "CANCELED", "REJECTED"})
_POSITION_TOLERANCE = 1e-9


class LiveBinanceFuturesExecutor:
    """Serialise venue submissions and settlement with the ledger's position decisions."""

    def __init__(
        self,
        store: AccountStore,
        client: BinanceFuturesClient,
        ledger: Ledger,
        *,
        account: str,
        agent_accounts: dict[str, str],
        rules: dict[str, SymbolRules],
        gates: RiskGates,
        on_unresolved: Callable[[str], None],
        dry_run: bool = True,
        quote_asset: str = "USDT",
        max_price_slippage_bps: int = 50,
        on_fatal: Callable[[Exception], None] | None = None,
        now: Callable[[], float] = time.time,
        id_factory: Callable[[str], str] | None = None,
    ) -> None:
        self._agent, = (agent for agent, name in agent_accounts.items() if name == account)
        self._store = store
        self._client = client
        self._ledger = ledger
        self._account = account
        self._rules = dict(rules)
        self._gates = gates
        self._on_unresolved = on_unresolved
        self._dry_run = dry_run
        self._quote_asset = quote_asset
        self._slippage = 1 + max_price_slippage_bps / 10_000
        self._on_fatal = on_fatal
        self._now = now
        self._id_factory = id_factory or _default_client_order_id
        self._restart_required = False

    @property
    def client(self) -> BinanceFuturesClient:
        return self._client

    @property
    def rules(self) -> dict[str, SymbolRules]:
        return dict(self._rules)

    @property
    def dry_run(self) -> bool:
        return self._dry_run

    def execute(self, request: FillRequest) -> TradeResult:
        with self._store.write_lock:
            return self._execute(request)

    def _execute(self, request: FillRequest) -> TradeResult:
        agent = request.agent_id
        product = request.product_id.upper().strip()
        side = request.action.lower().strip()
        symbol = venue_symbol(product)
        if request.replay:
            return TradeResult(False, "Live execution cannot replay historical fills.")
        if agent != self._agent or not self._store.agent_allows(agent, "perp"):
            return TradeResult(False, f"Live perpetual trading is not enabled for '{agent}'.")
        if side not in {"buy", "sell"}:
            return TradeResult(False, "Action must be buy or sell.")
        rules = self._rules.get(product)
        if rules is None or not is_perp_product(product):
            return TradeResult(False, f"{product} is not enabled for live perpetual trading.")
        if self._restart_required or self._store.ledger_degraded:
            return TradeResult(False, "Restart from the persistent futures ledger before trading.")
        if self._ledger.dangling_orders():
            return TradeResult(False, "An earlier order remains unresolved. Reconcile before trading.")

        entry = self._store.price_book.get(product)
        touch = self._quote(entry, "best_ask" if side == "buy" else "best_bid")
        if touch is None:
            return TradeResult(False, f"No usable live {side} quote for {product}.")
        price, received_at = touch

        account = self._store.get_or_create(agent)
        current = account.positions.get(product, 0.0)
        quantity = self._quantize(rules, request.quantity, current, side)
        if quantity is None:
            ceiling = f", maximum {rules.max_qty:g}" if rules.max_qty is not None else ""
            return TradeResult(
                False, f"Quantity is outside {product}'s venue market lot "
                f"(step {rules.step_size:g}, minimum {rules.min_qty:g}{ceiling}).",
            )
        quantity_float = float(quantity)
        clamped = clamp_flip_to_a_close(
            quantity_float, position=current, action=side, price=price, rules=rules,
        )
        if clamped != quantity_float:
            quantity = self._quantize(rules, clamped, current, side)
            if quantity is None:
                return TradeResult(False, f"{product} position is below the venue quantity step.")
            quantity_float = float(quantity)

        reduce_only = is_strict_reduce(current, quantity_float, side)
        delta = quantity_float if side == "buy" else -quantity_float
        if current * delta < 0 and not reduce_only:
            return TradeResult(False, f"{product} order crosses zero. Close the position first.")

        # Recheck under the submission lock. Another fill may have changed the book
        # since the tool's first set of checks, including its portfolio read stamp.
        blocked = self._store.preadmission_rejection(
            agent, product, quantity_float, side, allow_flip=request.allow_flip,
        )
        if blocked is not None:
            return blocked[1]
        if not reduce_only and not rules.notional_ok(quantity, price):
            return TradeResult(False, f"{product} order is below the venue minimum notional.")
        resulting_qty = abs(current + delta)
        # An accumulated position must remain expressible by close_position's one
        # reduce-only order. Reductions may still shrink an existing oversized book.
        if not reduce_only and rules.max_qty is not None and (
            Decimal(str(resulting_qty)) - rules.max_qty > rules.step_size * Decimal("0.000001")
        ):
            return TradeResult(
                False, f"{product} would exceed the maximum single-order close "
                f"size ({rules.max_qty:g}). Reduce the quantity.",
            )

        notional = quantity_float * price
        order_margin = notional * self._slippage * (
            1 + self._store.fee_model.taker_bps_for(perp=True) / 10_000
        )
        equity = account.cash
        gross = 0.0
        for held_product, held_quantity in account.positions.items():
            mark = self._mark(held_product)
            if mark is None:
                if not reduce_only:
                    return TradeResult(False, f"No fresh mark for {held_product} to check 1× leverage.")
                continue
            equity += held_quantity * mark
            gross += abs(held_quantity) * mark
        if not reduce_only and gross + order_margin > equity:
            return TradeResult(False, "Order would exceed the 1× gross exposure cap.")

        session_start_equity = self._session_start_equity()
        snapshot = PortfolioSnapshot(
            equity_usdt=equity,
            session_start_equity_usdt=session_start_equity,
            position_notional_usdt=abs(current) * price,
            realised_pnl_usdt=equity - session_start_equity,
            unrealised_pnl_usdt=0,
        )
        gate_input = dict(
            symbol=product, side=side, quantity=quantity_float, price=price, portfolio=snapshot,
            reduce_only=reduce_only, resulting_position_notional=resulting_qty * price,
        )
        decision = self._gates.check(
            **gate_input, price_age_s=self._now() - received_at,
        )
        if not decision.allowed:
            return TradeResult(False, decision.reason)

        if not self._dry_run:
            venue_refusal = self._venue_refusal(
                symbol, current, order_margin=0.0 if reduce_only else order_margin,
            )
            if venue_refusal is not None:
                return TradeResult(False, venue_refusal)
            # The signed reads took time. In particular, honour a halt written during
            # those requests before issuing the money-moving POST.
            if entry is not self._store.price_book.get(product):
                return TradeResult(False, f"{product} quote changed during venue checks. Re-evaluate.")
            decision = self._gates.check(
                **gate_input, price_age_s=self._now() - received_at,
            )
            if not decision.allowed:
                return TradeResult(False, decision.reason)

        client_id = self._id_factory(agent)
        self._ledger.save_order(
            client_order_id=client_id, agent_id=agent, account=self._account, symbol=product,
            side=side, state="DRY_RUN" if self._dry_run else "PENDING",
            qty_requested=quantity_float, quote_qty_requested=notional,
        )
        if self._dry_run:
            self._ledger.update_order(client_id, price=price, error="dry run: venue not contacted")
            return TradeResult(False, f"DRY-RUN: {side} {quantity:g} {product} was not sent.")

        decision = self._gates.check(**gate_input, price_age_s=self._now() - received_at)
        price_unchanged = entry is self._store.price_book.get(product)
        if not decision.allowed or not price_unchanged:
            reason = decision.reason if not decision.allowed else f"{product} quote changed before submission."
            self._ledger.update_order(client_id, state="REJECTED", error=reason)
            return TradeResult(False, reason)

        self._gates.record_order()
        try:
            fill = self._client.place_market_order(
                symbol, side, quantity, client_id, reduce_only=reduce_only,
            )
        except UnknownOutcome as exc:
            return self._unresolved(client_id, str(exc))
        except (BinanceApiError, IpBanned, RateLimited) as exc:
            self._ledger.update_order(client_id, state="REJECTED", error=str(exc))
            if isinstance(exc, IpBanned) and self._on_fatal is not None:
                self._on_fatal(exc)
            return TradeResult(False, f"Venue rejected {product} order: {exc}.")
        except Exception as exc:  # noqa: BLE001 — the POST may already have arrived
            return self._unresolved(client_id, str(exc))

        order = self._ledger.get_order(client_id)
        if order is None:
            return self._unresolved(client_id, "Order journal row is missing.")
        return self._book(order, fill, latency=request.latency)[1]

    def _venue_refusal(
        self, symbol: str, current: float, *, order_margin: float,
    ) -> str | None:
        """Read the dedicated wallet before sending an intent based on its signed position."""
        try:
            account_config = self._client.account_config()
            if (
                account_config.get("canTrade") is not True
                or account_config.get("dualSidePosition") is not False
                or account_config.get("multiAssetsMargin") is not False
            ):
                raise ValueError("futures account must permit trading with one-way, single-asset margin")
            if self._client.bnb_fee_burn():
                raise ValueError("futures fees must be charged in USDT (BNB fee burn is enabled)")
            positions = self._client.position_risk(symbol)
            if any(row["symbol"] != symbol or row["positionSide"] != "BOTH" for row in positions):
                raise ValueError(f"{symbol} venue position is not one-way")
            venue_quantity = float(positions[0]["positionAmt"]) if positions else 0.0
            if len(positions) > 1 or not math.isclose(
                current, venue_quantity, rel_tol=0, abs_tol=_POSITION_TOLERANCE,
            ):
                raise ValueError(
                    f"{symbol} venue position {venue_quantity:g} differs from ledger {current:g}"
                )
            if order_margin:
                config, = self._client.symbol_config(symbol)
                if (
                    config["symbol"] != symbol or float(config["leverage"]) != 1
                    or config["marginType"] != "CROSSED"
                ):
                    raise ValueError(f"{symbol} venue margin configuration is not cross margin at 1×")
                balance, = (
                    row for row in self._client.futures_balances()
                    if row["asset"] == self._quote_asset
                )
                available = float(balance["availableBalance"])
                if not math.isfinite(available):
                    raise ValueError("venue available margin is invalid")
                if order_margin > available:
                    return (
                        f"Insufficient available venue margin ({available:.2f} {self._quote_asset}); "
                        "reduce the quantity."
                    )
        except Exception as exc:  # noqa: BLE001 — failed verification disarms before submission
            if isinstance(exc, IpBanned) and self._on_fatal is not None:
                self._on_fatal(exc)
            reason = f"Could not verify venue state: {exc}"
            self._on_unresolved(reason)
            return reason
        return None

    @staticmethod
    def _quantize(
        rules: SymbolRules, requested: float, current: float, side: str,
    ) -> Decimal | None:
        # A full close sized from the float ledger can be infinitesimally below
        # its intended decimal step after previous partial fills or additions.
        if not math.isfinite(requested) or requested <= 0:
            return None
        if is_strict_reduce(current, requested, side) and math.isclose(
            requested, abs(current), rel_tol=0, abs_tol=_POSITION_TOLERANCE,
        ):
            size = Decimal(str(abs(current)))
            nearest = (size / rules.step_size).to_integral_value() * rules.step_size
            if abs(nearest - size) <= rules.step_size * Decimal("0.000001"):
                return rules.quantize(nearest)
        return rules.quantize(requested)

    def settle_dangling(self) -> list[tuple[str, str]]:
        """Verify order IDs after failure without resubmitting possibly executed intents."""
        with self._store.write_lock:
            resolved = []
            for order in self._ledger.dangling_orders():
                if self._restart_required or self._store.ledger_degraded:
                    resolved.append((order.client_order_id, "restart-required"))
                    continue
                if order.account != self._account or order.agent_id != self._agent \
                        or order.symbol not in self._rules or not is_perp_product(order.symbol):
                    log.error("Futures journal contains a foreign intent %s", order.client_order_id)
                    resolved.append((order.client_order_id, "foreign-intent"))
                    continue
                try:
                    fill = self._client.query_order(venue_symbol(order.symbol), order.client_order_id)
                except Exception as exc:  # noqa: BLE001 — the intent must stay unresolved
                    log.warning("Cannot resolve futures order %s: %s", order.client_order_id, exc)
                    resolved.append((order.client_order_id, "lookup-failed"))
                    continue
                if fill is None:
                    # A query miss does not prove a timed-out POST never reached the venue.
                    resolved.append((order.client_order_id, "not-found"))
                    continue
                outcome, _ = self._book(order, fill)
                resolved.append((order.client_order_id, outcome))
            return resolved

    def _book(
        self, order: OrderRecord, fill: VenueFill, *, latency: float | None = None,
    ) -> tuple[str, TradeResult]:
        client_id = order.client_order_id
        if (
            fill.client_order_id != client_id or fill.symbol != venue_symbol(order.symbol)
            or fill.side.lower() != order.side.lower() or fill.status not in _TERMINAL_STATES
            or not math.isfinite(fill.executed_qty) or fill.executed_qty < 0
            or order.qty_requested is None
            or fill.executed_qty > order.qty_requested + _POSITION_TOLERANCE
            or (
                fill.status == "FILLED"
                and not math.isclose(
                    fill.executed_qty, order.qty_requested, rel_tol=0, abs_tol=_POSITION_TOLERANCE,
                )
            )
        ):
            return "invalid-fill", self._unresolved(client_id, "Venue order identity or result is unresolved.")
        if fill.executed_qty == 0 and fill.status != "FILLED":
            state = "REJECTED" if fill.status == "REJECTED" else "EXPIRED"
            self._ledger.update_order(
                client_id, state=state, venue_order_id=fill.venue_order_id,
                executed_qty=0, cum_notional=0, error=f"Venue status: {fill.status}",
            )
            return fill.status.lower(), TradeResult(False, f"Venue order {fill.status} without a fill.")
        if (
            fill.executed_qty <= 0 or not math.isfinite(fill.price) or fill.price <= 0
            or not math.isfinite(fill.fee) or fill.fee < 0 or fill.fee_asset != self._quote_asset
        ):
            return "invalid-fill", self._unresolved(client_id, "Venue fill lacks usable price or fee evidence.")

        try:
            result = self._store.apply_fill(
                order.agent_id, order.side.lower(), order.symbol, fill.executed_qty,
                fill.price, fill.fee, latency=latency, client_order_id=client_id,
                fee_asset=fill.fee_asset, venue_order_id=fill.venue_order_id,
                enforce_margin=False,
            )
        except Exception as exc:  # noqa: BLE001 — hold further orders even if booking failed
            return "unbookable", self._booking_failure(client_id, str(exc))
        if not result.success:
            return "unbookable", self._unresolved(client_id, result.message)
        if self._store.ledger_degraded or not self._ledger.has_fill(client_id):
            return "unbookable", self._booking_failure(client_id, "Fill was not persisted.")
        return "booked", result

    def _booking_failure(self, client_id: str, reason: str) -> TradeResult:
        # AccountStore may have already updated the in-memory book. Replaying a fill
        # on that book can apply it twice, so hydration from disk is required first.
        self._restart_required = True
        if self._ledger.has_fill(client_id):
            self._on_unresolved(reason)
            return TradeResult(False, f"Order {client_id} is booked. Restart before trading: {reason}")
        return self._unresolved(client_id, f"{reason} Restart before recovery.")

    def _unresolved(self, client_id: str, reason: str) -> TradeResult:
        message = f"Order {client_id} needs reconciliation: {reason}"
        log.error(message)
        try:
            self._ledger.update_order(client_id, state="UNKNOWN", error=reason)
        finally:
            self._on_unresolved(message)
        return TradeResult(False, message)

    def _quote(self, quote: dict | None, field: str) -> tuple[float, float] | None:
        if quote is None:
            return None
        try:
            price = float(quote[field])
            received_at = float(quote["_received_at"])
            if quote.get("time"):
                venue_time = datetime.fromisoformat(quote["time"])
                if venue_time.tzinfo is None:
                    return None
                received_at = min(received_at, venue_time.timestamp())
        except (KeyError, TypeError, ValueError):
            return None
        if not (math.isfinite(price) and price > 0 and math.isfinite(received_at) and received_at <= self._now()):
            return None
        return price, received_at

    def _mark(self, product: str) -> float | None:
        quote = self._quote(self._store.price_book.get(product), "price")
        if quote is None or self._now() - quote[1] > self._gates.limits.max_price_age_seconds:
            return None
        return quote[0]

    def _session_start_equity(self) -> float:
        # The session writes its baseline atomically with the dedicated account
        # binding. A rejected order must never initialise an unverified ledger.
        stored = self._ledger.get_meta("live_session_start_equity")
        try:
            return float(stored)
        except (TypeError, ValueError):
            return float("nan")
