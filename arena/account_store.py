"""In-memory trading account store, keyed by agent_id."""

from __future__ import annotations

import logging
import threading
from datetime import datetime

from arena.fees import FeeModel
from arena.ledger import Ledger
from arena.models import AgentAccount, TradeLogEntry, TradeRecorder, TradeResult
from arena.price_book import PriceBook

log = logging.getLogger(__name__)


class AccountStore:
    """In-memory trading account store, keyed by agent_id."""

    def __init__(
        self,
        price_book: PriceBook,
        fee_model: FeeModel | None = None,
        ledger: Ledger | None = None,
    ) -> None:
        self._accounts: dict[str, AgentAccount] = {}
        self._trade_log: list[TradeLogEntry] = []
        self._price_book = price_book
        self._data_recorder: TradeRecorder | None = None
        self._fee_model = fee_model or FeeModel()
        self._ledger = ledger
        # One fill at a time: tool calls arrive from worker threads, and the
        # read-modify-write below plus ledger.save_fill must see a settled state.
        self._write_lock = threading.Lock()
        # Set when a ledger write fails: the fill still counted in memory, so the
        # disk is now behind. Surfaced by PortfolioView's header badge and by the
        # durable meta row that `manage_bot.py status` reads from another process.
        self._ledger_degraded = False

    @property
    def ledger_degraded(self) -> bool:
        """True once a fill could not be persisted (memory and disk have diverged)."""
        return self._ledger_degraded

    def _mark_ledger_degraded(self, agent_id: str, moment: datetime) -> None:
        """Record the divergence durably; best-effort, since the write path is broken."""
        if self._ledger is None:
            return
        try:
            self._ledger.mark_degraded(agent_id, moment.isoformat())
        except Exception as exc:
            # The in-memory flag still works, so the dashboard badge shows up even
            # when the marker cannot be written — only the cross-process view is lost.
            log.warning("account_store: could not persist the degraded marker: %s", exc)

    def attach_recorder(self, recorder: TradeRecorder) -> None:
        self._data_recorder = recorder

    def attach_ledger(self, ledger: Ledger, *, clear_marker: bool = False) -> None:
        """Adopt a ledger and hydrate accounts/fills from it (startup path).

        ``clear_marker`` is opt-in because hydrating from disk is not proof that this
        startup succeeds: the deployment attaches the ledger before the broker is even
        reached, so a refused Kafka bootstrap would die right after erasing the
        operator's record of an earlier divergence. The deploy entrypoint clears the
        marker from its ``after_startup`` hook (broker up) instead.
        """
        if self._accounts or self._trade_log:
            raise RuntimeError(
                "account_store.attach_ledger: this store already holds "
                f"{len(self._accounts)} account(s) and {len(self._trade_log)} fill(s). "
                "Attaching another ledger would discard them — attach the ledger "
                "before any trade is executed."
            )
        self._ledger = ledger
        self._ledger_degraded = False
        self._accounts = ledger.load_accounts()
        self._trade_log = ledger.recent_fills()
        if clear_marker:
            self.mark_ledger_healthy()
        log.info(
            "account_store.attach_ledger: hydrated %d account(s) and %d recent fill(s) from %s",
            len(self._accounts), len(self._trade_log), ledger.path,
        )

    def mark_ledger_healthy(self) -> None:
        """Clear the durable degraded marker; call only once the engine really serves.

        Best-effort like the write side: the marker shares the ledger's connection, so
        when it cannot be written the dashboard badge and the logs are what remain.
        """
        if self._ledger is None:
            return
        try:
            self._ledger.mark_healthy()
        except Exception as exc:
            log.warning("account_store: could not clear the degraded marker: %s", exc)

    def set_fee_model(self, fee_model: FeeModel) -> None:
        """Replace the active fee model. Used by deploy entrypoints after CLI/config resolution."""
        self._fee_model = fee_model

    @property
    def fee_model(self) -> FeeModel:
        return self._fee_model

    def get_or_create(self, agent_id: str) -> AgentAccount:
        if agent_id not in self._accounts:
            log.debug("account_store.get_or_create: new account for agent=%s", agent_id)
            self._accounts[agent_id] = AgentAccount()
        return self._accounts[agent_id]

    @property
    def accounts(self) -> dict[str, AgentAccount]:
        return self._accounts

    @property
    def price_book(self) -> PriceBook:
        return self._price_book

    @property
    def trade_log(self) -> list[TradeLogEntry]:
        return self._trade_log

    def execute_trade(
        self,
        agent_id: str,
        product_id: str,
        quantity: float,
        action: str,
        latency: float | None = None,
    ) -> TradeResult:
        """Execute one fill, serialised so overlapping tool calls cannot interleave."""
        with self._write_lock:
            return self._execute_trade_locked(agent_id, product_id, quantity, action, latency)

    def _execute_trade_locked(
        self,
        agent_id: str,
        product_id: str,
        quantity: float,
        action: str,
        latency: float | None = None,
    ) -> TradeResult:
        product_id = product_id.upper().strip()
        action = action.lower().strip()
        log.debug(
            "account_store.execute_trade ENTER: agent=%s action=%s product=%s qty=%s",
            agent_id, action, product_id, quantity,
        )

        if action not in ("buy", "sell"):
            log.debug("account_store.execute_trade REJECT: invalid action=%s", action)
            return TradeResult(False, f"Invalid action '{action}'. Must be 'buy' or 'sell'.")

        entry = self._price_book.get(product_id)
        if entry is None:
            available = ", ".join(sorted(self._price_book.snapshot().keys()))
            log.debug(
                "account_store.execute_trade REJECT: no price for %s (available: %s)",
                product_id, available,
            )
            return TradeResult(
                False,
                f"No live price for '{product_id}'. "
                f"Available: {available or 'none (waiting for price data)'}",
            )

        if quantity <= 0:
            log.debug("account_store.execute_trade REJECT: non-positive qty=%s", quantity)
            return TradeResult(False, "Quantity must be positive.")

        rounded = round(quantity, 1)
        if abs(quantity - rounded) > 1e-9:
            log.debug("account_store.execute_trade REJECT: qty precision qty=%s", quantity)
            return TradeResult(
                False, "Quantity must have at most 1 decimal place (e.g., 0.5, 1.2)."
            )
        quantity = rounded

        account = self.get_or_create(agent_id)

        if action == "buy":
            price = float(entry["best_ask"])
            notional = price * quantity
            cash_out, fee = self._fee_model.buy_cost(notional)
            if cash_out > account.cash:
                log.debug(
                    "account_store.execute_trade REJECT: insufficient cash "
                    "need=%.2f (incl fee=%.2f) have=%.2f agent=%s",
                    cash_out, fee, account.cash, agent_id,
                )
                return TradeResult(
                    False,
                    f"Insufficient cash. Need ${cash_out:,.2f} "
                    f"(${notional:,.2f} + ${fee:,.2f} fee) "
                    f"but only have ${account.cash:,.2f}.",
                )
            account.cash -= cash_out
            existing_qty = account.positions.get(product_id, 0)
            now_ts = datetime.now().timestamp()
            existing_ts = account.avg_entry_ts.get(product_id, now_ts)
            account.avg_entry_ts[product_id] = (existing_qty * existing_ts + quantity * now_ts) / (
                existing_qty + quantity
            )
            account.positions[product_id] = existing_qty + quantity
            # Capitalize the fee into cost basis: cost basis = notional + fee = cash_out
            account.cost_basis[product_id] = account.cost_basis.get(product_id, 0.0) + cash_out
            account.total_fees_paid += fee
            account.trade_count += 1
            self._record_trade(agent_id, action, product_id, quantity, price, fee, latency)
            log.debug(
                "account_store.execute_trade BUY OK: agent=%s %s %s @ $%.2f "
                "notional=$%.2f fee=$%.2f cash_after=$%.2f positions=%s trade_count=%d",
                agent_id, quantity, product_id, price,
                notional, fee, account.cash, dict(account.positions), account.trade_count,
            )
            return TradeResult(
                True,
                f"Bought {quantity} {product_id} @ ${price:,.2f} for ${cash_out:,.2f} "
                f"(${notional:,.2f} + ${fee:,.2f} fee @ {self._fee_model.taker_bps} bps). "
                f"Cash remaining: ${account.cash:,.2f}.",
            )

        # sell
        price = float(entry["best_bid"])
        held = account.positions.get(product_id, 0)
        if quantity > held:
            log.debug(
                "account_store.execute_trade REJECT: insufficient holdings "
                "want=%s held=%s product=%s agent=%s",
                quantity, held, product_id, agent_id,
            )
            return TradeResult(
                False,
                f"Insufficient holdings. Want to sell {quantity} {product_id} "
                f"but only hold {held}.",
            )
        notional = price * quantity
        cash_in, fee = self._fee_model.sell_proceeds(notional)
        account.cash += cash_in
        # Reduce cost basis proportionally (average cost method). avg_cost includes
        # the capitalized buy fee, so realized P&L nets both the entry and exit fees:
        # (notional − sell_fee) − avg_cost·qty.
        avg_cost = account.avg_cost_per_unit(product_id)
        account.realized_pnl += cash_in - (avg_cost * quantity)
        account.total_fees_paid += fee
        account.cost_basis[product_id] = account.cost_basis.get(product_id, 0.0) - (
            avg_cost * quantity
        )
        new_qty = round(held - quantity, 1)
        if new_qty <= 0:
            del account.positions[product_id]
            del account.cost_basis[product_id]
            account.avg_entry_ts.pop(product_id, None)
        else:
            account.positions[product_id] = new_qty
        account.trade_count += 1
        self._record_trade(agent_id, action, product_id, quantity, price, fee, latency)
        log.debug(
            "account_store.execute_trade SELL OK: agent=%s %s %s @ $%.2f "
            "notional=$%.2f fee=$%.2f cash_after=$%.2f positions=%s trade_count=%d",
            agent_id, quantity, product_id, price,
            notional, fee, account.cash, dict(account.positions), account.trade_count,
        )
        return TradeResult(
            True,
            f"Sold {quantity} {product_id} @ ${price:,.2f} for ${cash_in:,.2f} "
            f"(${notional:,.2f} − ${fee:,.2f} fee @ {self._fee_model.taker_bps} bps). "
            f"Cash remaining: ${account.cash:,.2f}.",
        )

    def _record_trade(
        self,
        agent_id: str,
        action: str,
        product_id: str,
        quantity: float,
        price: float,
        fee: float,
        latency: float | None = None,
    ) -> None:
        now = datetime.now()
        self._trade_log.append(
            TradeLogEntry(
                now.strftime("%H:%M:%S"),
                agent_id, action, product_id, quantity, price, fee, latency,
            )
        )

        # Resolved once: both the CSV recorder and the ledger need the post-fill state.
        account = self._accounts.get(agent_id)

        if self._data_recorder is not None:
            self._data_recorder.record_trade(
                agent_id=agent_id,
                action=action,
                product_id=product_id,
                quantity=quantity,
                price=price,
                fee=fee,
                cash_after=account.cash if account else 0.0,
                latency=latency,
            )

        if self._ledger is not None and account is not None:
            try:
                self._ledger.save_fill(
                    agent_id=agent_id,
                    account=account,
                    timestamp=now.isoformat(),
                    action=action,
                    product_id=product_id,
                    quantity=quantity,
                    price=price,
                    fee=fee,
                    cash_after=account.cash,
                    latency=latency,
                )
            except Exception:
                # The fill already happened in memory; failing the trade because the
                # disk hiccuped would break the agent mid-session. Flag the divergence
                # instead (dashboard badge + `manage_bot.py status`), and write the
                # durable marker only on the transition: it means "disk is behind from
                # this instant on", so rewriting it on every later failure would move
                # the timestamp past fills already missing and hammer a broken database.
                first_failure = not self._ledger_degraded
                self._ledger_degraded = True
                if first_failure:
                    self._mark_ledger_degraded(agent_id, now)
                log.exception(
                    "account_store: ledger write FAILED for agent=%s %s %s qty=%s — "
                    "this fill is in memory only; the ledger is now behind",
                    agent_id, action, product_id, quantity,
                )
