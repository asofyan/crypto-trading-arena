"""In-memory trading account store, keyed by agent_id.

Two instrument shapes share this store:

* **spot** (``SOLUSDT``) — long only, cash funded. The original arithmetic: a buy
  capitalises its fee into the cost basis, a sell realises against it.
* **perpetual futures** (``SOLUSDT-PERP``) — a *signed net position* (``+q`` long,
  ``-q`` short), 1:1 leverage (gross notional may never exceed equity), and the fee
  expensed against the wallet on every fill, the way the venue charges it.

Which branch a fill takes is decided by the product id alone (see
``arena.instruments.is_perp_product``), so every caller — paper executor, ledger
replay, portfolio monitor — agrees without extra wiring.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from datetime import datetime, timezone

from arena.execution import Executor, FillRequest, PaperExecutor, clamp_flip_to_a_close
from arena.fees import FeeModel
from arena.hold_watchdog import StalePosition
from arena.hold_watchdog import stale_positions as _stale_positions
from arena.instruments import SymbolRules, is_perp_product
from arena.ledger import Ledger
from arena.models import AgentAccount, TradeLogEntry, TradeRecorder, TradeResult
from arena.price_book import PriceBook
from arena.trade_policy import ChurnGuard, ChurnLimits, ExitFloor, is_strict_reduce

log = logging.getLogger(__name__)

# A closed position must vanish even when float subtraction leaves dust
# (0.1+0.1+0.7−0.9 = 1.1e−16), and venue steps go far finer than the old 1-decimal
# rounding. Smaller than any venue's minimum step, so no real quantity is swallowed.
_QTY_EPSILON = 1e-9

# Margin comparisons at 1:1 work in quote currency, where a float round-trip is good to
# ~1e-13. A micro-dollar of tolerance absorbs that dust and nothing an operator would
# ever call a position: never a fee, never a venue step, never a real allowance.
_MARGIN_EPSILON = 1e-6

# A quote older than this cannot be trusted to size a leveraged position: the connector
# republishes every 15s (perp) or 60s (spot), so a minute of silence means the feed is
# broken, not that the market stood still.
_MARK_STALENESS_SECONDS = 60.0

# The markets a product can belong to. The executor uses these names to keep an agent
# inside the scope its strategy was written for (a spot agent must never open a short).
SPOT_MARKET = "spot"
PERP_MARKET = "perp"


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
        # Turnover guard for the order path. ``None`` (the default) keeps the pre-guard
        # behaviour so a deployment that never opts in — and every existing test — is
        # unchanged; deploy wires it from ``trading.churn``.
        self._churn_guard: ChurnGuard | None = None
        # Exit floor for the order path: refuses a close that lands between the position's
        # stop and its target (see arena.trade_policy.ExitFloor). ``None`` (the default)
        # keeps the pre-guard behaviour; deploy wires it from ``trading.exit_floor``.
        self._exit_floor: ExitFloor | None = None
        self._ledger = ledger
        # One fill at a time: tool calls arrive from worker threads, and the
        # read-modify-write below plus ledger.save_fill must see a settled state.
        # Re-entrant because an executor holds it across "validate, then apply_fill"
        # (the nested acquire in apply_fill is a no-op on the same thread).
        self._write_lock = threading.RLock()
        # Default fill source: simulate against the price book. A live run swaps this
        # at startup (set_executor), so every caller keeps using execute_trade.
        self._executor: Executor = PaperExecutor(self)
        # Per-symbol venue rules (see arena.instruments). Empty means "no venue data":
        # every symbol keeps the coarse 1-decimal paper rule, which is what keeps the
        # unit suite and a Coinbase/offline deployment working unchanged.
        self._instrument_rules: dict[str, SymbolRules] = {}
        # agent_id → markets it may trade (e.g. {"scalper-perp": ("perp",)}). Agents
        # absent here keep the pre-existing behaviour and may trade anything: this is a
        # guard for strategies that cannot mean both markets, not a permission system.
        self._agent_markets: dict[str, tuple[str, ...]] = {}
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
            len(self._accounts),
            len(self._trade_log),
            ledger.path,
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

    def set_churn_limits(self, limits: ChurnLimits | None) -> None:
        """Install the turnover guard, or ``None`` to disable it (the default).

        Installs a fresh ``ChurnGuard``, so call it at startup: re-calling it resets the
        rolling hour, and a guard with no memory would let the first fill of every
        restart through a budget the previous process had already spent.
        """
        self._churn_guard = None if limits is None else ChurnGuard(limits)

    def set_exit_floor(self, floor: ExitFloor | None) -> None:
        """Install the no-scratch-exit gate, or ``None`` to disable it (the default).

        Stateless, unlike ``set_churn_limits``: the floor holds no history, so it can be
        swapped at any point without forgetting anything a restart would have kept.
        """
        self._exit_floor = floor

    def close_position(
        self, agent_id: str, product_id: str, *, latency: float | None = None
    ) -> TradeResult:
        """Flatten whatever is open right now, sized under the same lock that books it.

        The watchdog knows *which* position is stale; it must not decide the *size*.
        A read that is one fill out of date turns a close into an opening order on the
        other side — a long of 8.0 read at 8.0 and closed by the agent while the
        watchdog was between calls becomes a short of 8.0, with a fresh hold clock and
        the exit floor unable to touch it. Re-reading here, inside the lock the fill is
        booked under, is what makes "close" mean close.

        Nothing to close is a refusal, not a success: the caller asked for a fill and
        none went out. It is also the only safe answer — an order that opens a position
        because the book was already flat is the bug this method exists to prevent.

        In live mode the venue call happens while this lock is held, which is the price
        of sizing and booking under one writer; the hold watchdog calls this from a
        worker thread so the event loop is not the one waiting on the order.
        """
        product_id = product_id.upper().strip()
        with self._write_lock:
            account = self._accounts.get(agent_id)
            position = account.positions.get(product_id, 0.0) if account is not None else 0.0
            if not math.isfinite(position) or abs(position) <= _QTY_EPSILON:
                return TradeResult(
                    False,
                    f"Nothing to close: {agent_id} is flat on {product_id}.",
                )
            return self.execute_trade(
                agent_id,
                product_id,
                abs(position),
                "sell" if position > 0 else "buy",
                latency,
            )

    def stale_positions(
        self, hold_limit_s: float | None, *, now: float | None = None
    ) -> list[StalePosition]:
        """The positions past ``hold_limit_s``, as the closes that would end them.

        The quantity on each result is **diagnostic**: the close is sized again by
        :meth:`close_position` under the booking lock, because this read releases it.
        """
        timestamp = datetime.now().timestamp() if now is None else now
        with self._write_lock:
            return _stale_positions(
                self._accounts, hold_limit_s=hold_limit_s, now=timestamp
            )

    @property
    def instrument_rules(self) -> dict[str, SymbolRules]:
        """Venue rules per product id; a symbol absent here trades on the legacy rule."""
        return self._instrument_rules

    def set_instrument_rules(self, rules: dict[str, SymbolRules]) -> None:
        """Install venue rules at startup, before the first fill can arrive."""
        self._instrument_rules = dict(rules)

    def set_agent_markets(self, markets: dict[str, tuple[str, ...] | list[str]]) -> None:
        """Restrict each agent to the markets its strategy was written for.

        A spot prompt has no vocabulary for a short position, and a perp prompt reasons
        about leverage a spot agent cannot see. Keeping the two apart here — not only in
        the system prompt — means a confused model cannot invent the other instrument.
        """
        self._agent_markets = {
            agent: tuple(m.lower() for m in allowed) for agent, allowed in markets.items()
        }

    def agent_markets(self, agent_id: str) -> tuple[str, ...] | None:
        """Markets `agent_id` may trade, or ``None`` when it is unrestricted."""
        return self._agent_markets.get(agent_id)

    def agent_allows(self, agent_id: str, market: str) -> bool:
        allowed = self._agent_markets.get(agent_id)
        return allowed is None or market in allowed

    @property
    def fee_model(self) -> FeeModel:
        return self._fee_model

    def get_or_create(self, agent_id: str) -> AgentAccount:
        if agent_id not in self._accounts:
            log.debug("account_store.get_or_create: new account for agent=%s", agent_id)
            self._accounts[agent_id] = AgentAccount()
        return self._accounts[agent_id]

    def apply_equity_adjustment(self, agent_id: str, amount: float) -> tuple[float, float]:
        """Move cash without a fill (deposit adoption, operator write-down).

        Returns ``(equity_before, equity_after)`` so the caller can journal the event
        with the numbers the operator will later use to explain the equity curve.
        Refuses to push cash below zero: writing down more than the agent holds is a
        mistake, not an instruction.
        """
        with self._write_lock:
            account = self.get_or_create(agent_id)
            before = account.portfolio_value(self._price_book)
            if account.cash + amount < 0:
                raise ValueError(
                    f"cannot adjust {agent_id} by {amount:,.2f}: it holds only "
                    f"{account.cash:,.2f} cash"
                )
            account.cash += amount
            if self._ledger is not None:
                self._ledger.persist_account(agent_id, account)
            after = account.portfolio_value(self._price_book)
        log.info(
            "account_store: equity adjustment agent=%s amount=%+.2f cash=%.2f",
            agent_id,
            amount,
            account.cash,
        )
        return before, after

    @property
    def accounts(self) -> dict[str, AgentAccount]:
        return self._accounts

    @property
    def price_book(self) -> PriceBook:
        return self._price_book

    @property
    def trade_log(self) -> list[TradeLogEntry]:
        return self._trade_log

    # ── Execution seam ───────────────────────────────────────────

    @property
    def write_lock(self) -> threading.RLock:
        """Held across any read-modify-write of an account; executors take it too."""
        return self._write_lock

    @property
    def executor(self) -> Executor:
        return self._executor

    def set_executor(self, executor: Executor) -> None:
        """Install the fill source. Paper by default; a live run swaps it at startup."""
        self._executor = executor

    def execute_trade(
        self,
        agent_id: str,
        product_id: str,
        quantity: float,
        action: str,
        latency: float | None = None,
        *,
        replay: bool = False,
    ) -> TradeResult:
        """Route one trade intent through the active executor.

        ``replay`` books a fill that already happened elsewhere (the CSV the arena wrote)
        and therefore skips the leverage gate: the recorded fill is the truth, and
        re-deriving margin from a partially reconstructed price book would reject it.
        A replay skips the churn guard and the exit floor for the same reason — it is
        history, not a new decision, and refusing it would silently drop a fill from the
        ledger.
        """
        if not replay:
            blocked = self._preadmission_rejection(agent_id, product_id, quantity, action)
            if blocked is not None:
                gate, rejection = blocked
                log.info(
                    "account_store.execute_trade REJECT (%s): agent=%s action=%s "
                    "product=%s qty=%s — %s",
                    gate,
                    agent_id,
                    action,
                    product_id,
                    quantity,
                    rejection.message,
                )
                return rejection
        return self._executor.execute(
            FillRequest(agent_id, product_id, quantity, action, latency, replay=replay)
        )

    def _preadmission_rejection(
        self, agent_id: str, product_id: str, quantity: float, action: str
    ) -> tuple[str, TradeResult] | None:
        """Ask every order-path gate about this fill; ``None`` means it may go out.

        Both gates are asked under one lock acquisition, before the executor runs and
        not for it: a gate must never hold every other agent behind a venue round-trip,
        so two intents racing the same window can both pass. The fill after them is the
        one a stateful gate stops; the exit floor has no state and is decided by the
        account, which only one writer can touch at a time anyway.

        Churn is asked first because its refusals are the ones an agent addresses by
        trading less, and it never refuses a reduce — so in practice each gate owns a
        disjoint set of fills and only a flip is judged by both.
        """
        with self._write_lock:
            if self._churn_guard is not None:
                rejection = self._churn_rejection(agent_id, product_id, quantity, action)
                if rejection is not None:
                    return "churn", rejection
            if self._exit_floor is not None:
                rejection = self._exit_floor_rejection(agent_id, product_id, quantity, action)
                if rejection is not None:
                    return "exit floor", rejection
        return None

    def _churn_rejection(
        self, agent_id: str, product_id: str, quantity: float, action: str
    ) -> TradeResult | None:
        """The guard's verdict as a refusal, or ``None`` when the fill may go out.

        The store owns this translation because only it knows the position the fill
        would change and the fee the fee model would charge: the guard is told both, so
        it can keep judging *rate* instead of re-deriving account state.

        An action other than buy/sell is not judged here — the executor already owns
        that message, and a churn complaint about a typo would only confuse the agent.
        """
        guard = self._churn_guard
        if guard is None:
            return None
        action = action.lower().strip()
        if action not in ("buy", "sell"):
            return None
        product_id = product_id.upper().strip()
        perp = is_perp_product(product_id)

        account = self._accounts.get(agent_id)
        pos_now = account.positions.get(product_id, 0.0) if account is not None else 0.0

        # ``is_strict_reduce`` is the definition of an exit for a signed net position:
        # closing, trimming and shrinking a short all qualify, while a flip past zero
        # does not — however much smaller it leaves the book, it is a decision to be
        # short (or long), so it pays for the cooldown and the fill budget like the
        # new position it is. Spot has no short, so a sell is always an exit there and a
        # buy can never be one.
        if perp:
            # The executor may clamp an overshooting close down to the close it mostly
            # is, and the gate has to judge the same order the book will see: reading
            # the raw 8.9 against a long of 8.8 calls a close a flip and refuses it.
            reduces = is_strict_reduce(
                pos_now,
                self._bookable_quantity(product_id, pos_now, quantity, action),
                action,
            )
        else:
            reduces = action == "sell"

        # No mark means no fee estimate, and the guard falls back to the fill count and
        # the cooldown: refusing to quote a fee is not a reason to skip the rate limits.
        fee_estimate = 0.0
        entry = self._price_book.get(product_id)
        quote = None
        if entry is not None:
            quote = entry.get("best_ask" if action == "buy" else "best_bid")
            if quote is None:
                quote = entry.get("price")
        if (
            quote is not None
            and math.isfinite(quantity)
            and quantity > 0
            and math.isfinite(float(quote))
            and float(quote) > 0
        ):
            fee_estimate = self._fee_model.fee(float(quote) * quantity, perp=perp)

        equity = account.portfolio_value(self._price_book) if account is not None else 0.0
        verdict = guard.check(
            agent_id=agent_id,
            product_id=product_id,
            fee_estimate=fee_estimate,
            equity=equity,
            reduces_exposure=reduces,
        )
        return None if verdict.allowed else TradeResult(False, verdict.reason)

    def _exit_floor_rejection(
        self, agent_id: str, product_id: str, quantity: float, action: str
    ) -> TradeResult | None:
        """The exit floor's verdict as a refusal, or ``None`` when the fill may go out.

        The store answers the two questions only it can: how far the position has moved
        from its entry, and how long it has held. It measures the move at the price the
        fill would actually get (the bid for a sell, the ask for a buy) so the number the
        floor judges is the number the close would realise, not a mid-price fantasy.

        **A fill is judged when it closes an old lot, not when it shrinks the position.**
        ``pos_old != 0 and (pos_old > 0) != (delta > 0)`` is the whole rule, and it is
        deliberately wider than ``reduces_exposure``: selling *more* than a long closes
        the old lot and opens a short, so a flip at +5 bps is a scratch exit dressed up
        as a new position. Opening, adding, and anything that leaves every old lot
        untouched are never judged.

        Fails open on every input it cannot trust — a zero/missing cost basis, a flat
        position, a missing entry time (a legacy row the ledger has no timestamp for; a
        hydrated position keeps the one it was saved with), a missing or non-finite
        quote. Refusing a close because a record is
        incomplete is how a guard turns a guard into a trap.
        """
        floor = self._exit_floor
        if floor is None:
            return None
        action = action.lower().strip()
        if action not in ("buy", "sell"):
            return None  # the executor owns the message for a typo
        product_id = product_id.upper().strip()

        account = self._accounts.get(agent_id)
        if account is None:
            return None
        pos_old = account.positions.get(product_id, 0.0)
        if abs(pos_old) <= _QTY_EPSILON:
            return None
        delta = quantity if action == "buy" else -quantity
        if (pos_old > 0) == (delta > 0):
            return None  # opening or adding on the same side: no old lot is closed

        basis = account.cost_basis.get(product_id, 0.0)
        if not math.isfinite(basis) or basis == 0.0:
            return None
        entry = basis / pos_old  # both signed, so the entry price is positive
        if not math.isfinite(entry) or entry <= 0:
            return None

        entry_ts = account.avg_entry_ts.get(product_id)
        if entry_ts is None or not math.isfinite(entry_ts):
            return None

        quote = self._exit_quote(product_id, action)
        if quote is None:
            return None
        bps_move = (
            (quote - entry) / entry * 10_000.0
            if pos_old > 0
            else (entry - quote) / entry * 10_000.0
        )
        held_seconds = datetime.now().timestamp() - entry_ts

        verdict = floor.check(bps_move=bps_move, held_seconds=held_seconds)
        return None if verdict.allowed else TradeResult(False, verdict.reason)

    def _bookable_quantity(
        self, product_id: str, position: float, quantity: float, action: str
    ) -> float:
        """The size the executor will book, so a gate can judge that and not the intent.

        Today the one rewrite is a perp flip whose opening leg is under the venue's
        minimum notional: the executor books it as a close (``clamp_flip_to_a_close``).
        A gate that skipped this would see a flip where the book sees an exit.
        """
        rules = self._instrument_rules.get(product_id)
        if rules is None:
            return quantity
        price = self._exit_quote(product_id, action)
        if price is None:
            return quantity
        return clamp_flip_to_a_close(
            quantity, position=position, action=action, price=price, rules=rules
        )

    def _exit_quote(self, product_id: str, action: str) -> float | None:
        """The touch this fill would cross, or ``None`` when the book cannot say."""
        entry = self._price_book.get(product_id)
        if entry is None:
            return None
        quote = entry.get("best_bid" if action == "sell" else "best_ask")
        if quote is None:
            quote = entry.get("price")
        try:
            quote = float(quote)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(quote) or quote <= 0:
            return None
        return quote

    def apply_fill(
        self,
        agent_id: str,
        action: str,
        product_id: str,
        quantity: float,
        price: float,
        fee: float,
        latency: float | None = None,
        fee_bps: int | None = None,
        client_order_id: str | None = None,
        fee_asset: str | None = None,
        venue_order_id: str | None = None,
        enforce_margin: bool = True,
    ) -> TradeResult:
        """Book one already-priced fill: cash, cost basis, P&L, trade log, ledger.

        The account's own constraints still decide whether the fill may be booked
        (cash on a buy, holdings on a sell, the 1:1 cap on leverage) — the executor
        already decided *what* filled and at what price. ``fee_bps`` only shapes the
        confirmation text: paper passes the configured rate, a live fill passes the rate
        implied by the venue's commission (or ``None`` when it cannot be stated cleanly).

        ``client_order_id`` switches persistence from "append a fill" to "settle this
        journal entry", which is what makes a post-crash replay a no-op: the check runs
        *before* the in-memory account moves, so memory and disk cannot disagree.

        ``enforce_margin=False`` is for replaying a fill the arena already accepted (see
        ``execute_trade(replay=True)``); it never skips the cash/holdings checks.
        """
        with self._write_lock:
            return self._apply_fill_locked(
                agent_id,
                action,
                product_id,
                quantity,
                price,
                fee,
                latency,
                fee_bps,
                client_order_id,
                fee_asset,
                venue_order_id,
                enforce_margin,
            )

    def _apply_fill_locked(
        self,
        agent_id: str,
        action: str,
        product_id: str,
        quantity: float,
        price: float,
        fee: float,
        latency: float | None = None,
        fee_bps: int | None = None,
        client_order_id: str | None = None,
        fee_asset: str | None = None,
        venue_order_id: str | None = None,
        enforce_margin: bool = True,
    ) -> TradeResult:
        product_id = product_id.upper().strip()
        action = action.lower().strip()

        if (
            client_order_id is not None
            and self._ledger is not None
            and self._ledger.has_fill(client_order_id)
        ):
            log.warning(
                "account_store.apply_fill: %s is already booked; ignoring replay",
                client_order_id,
            )
            return TradeResult(False, f"Order {client_order_id} was already booked; nothing to do.")
        log.debug(
            "account_store.apply_fill ENTER: agent=%s action=%s product=%s qty=%s price=%s fee=%s",
            agent_id,
            action,
            product_id,
            quantity,
            price,
            fee,
        )

        if action not in ("buy", "sell"):
            log.debug("account_store.apply_fill REJECT: invalid action=%s", action)
            return TradeResult(False, f"Invalid action '{action}'. Must be 'buy' or 'sell'.")

        account = self.get_or_create(agent_id)
        # Only the confirmation text needs the rate: a live fill's commission may not
        # reduce to a clean bps figure (base-asset or BNB-denominated fees).
        fee_rate = f" @ {fee_bps} bps" if fee_bps is not None else ""

        if is_perp_product(product_id):
            # A perp is a signed net position, not inventory: the spot rules below
            # (no selling what you do not hold, fees capitalised into basis) would
            # read a short as a negative holding and reject every close.
            return self._apply_perp_fill_locked(
                agent_id,
                action,
                product_id,
                quantity,
                price,
                fee,
                latency,
                fee_rate,
                client_order_id,
                fee_asset,
                venue_order_id,
                enforce_margin,
            )

        if action == "buy":
            notional = price * quantity
            cash_out = notional + fee
            if cash_out > account.cash:
                log.debug(
                    "account_store.apply_fill REJECT: insufficient cash "
                    "need=%.2f (incl fee=%.2f) have=%.2f agent=%s",
                    cash_out,
                    fee,
                    account.cash,
                    agent_id,
                )
                return TradeResult(
                    False,
                    f"Insufficient cash. Need ${cash_out:,.2f} "
                    f"(${notional:,.2f} + ${fee:,.2f} fee) "
                    f"but only have ${account.cash:,.2f}.",
                )
            if enforce_margin and any(is_perp_product(pid) for pid in account.positions):
                # A perp short credits its notional to cash, so "can I afford it" no
                # longer implies "is it 1:1": this buy could be funded by borrow-shaped
                # proceeds and push gross notional past equity. A pure spot book needs no
                # check (cash >= notional already implies gross <= equity), so this
                # leaves spot-only deployments — and their quotes — untouched.
                positions_after = dict(account.positions)
                positions_after[product_id] = account.positions.get(product_id, 0.0) + quantity
                rejection = self._leverage_rejection(
                    account,
                    positions_after,
                    account.cash - cash_out,
                    product_id=product_id,
                    price=price,
                )
                if rejection is not None:
                    return rejection
            account.cash -= cash_out
            existing_qty = account.positions.get(product_id, 0)
            now_ts = datetime.now().timestamp()
            # An add keeps the clock the position already had: the hold limit measures
            # how long the exposure has existed, and a dust add that renewed it would
            # make the limit optional. A fresh position starts here.
            account.avg_entry_ts.setdefault(product_id, now_ts)
            account.positions[product_id] = existing_qty + quantity
            # Capitalize the fee into cost basis: cost basis = notional + fee = cash_out
            account.cost_basis[product_id] = account.cost_basis.get(product_id, 0.0) + cash_out
            account.total_fees_paid += fee
            account.trade_count += 1
            self._record_trade(
                agent_id,
                action,
                product_id,
                quantity,
                price,
                fee,
                latency,
                client_order_id,
                fee_asset,
                venue_order_id,
                replay=not enforce_margin,
            )
            log.debug(
                "account_store.apply_fill BUY OK: agent=%s %s %s @ $%.2f "
                "notional=$%.2f fee=$%.2f cash_after=$%.2f positions=%s trade_count=%d",
                agent_id,
                quantity,
                product_id,
                price,
                notional,
                fee,
                account.cash,
                dict(account.positions),
                account.trade_count,
            )
            return TradeResult(
                True,
                f"Bought {quantity} {product_id} @ ${price:,.2f} for ${cash_out:,.2f} "
                f"(${notional:,.2f} + ${fee:,.2f} fee{fee_rate}). "
                f"Cash remaining: ${account.cash:,.2f}.",
            )

        # sell
        held = account.positions.get(product_id, 0)
        # Tolerance, not equality: accumulating 0.1+0.1+0.7 leaves the position a hair
        # under 0.9, and refusing the closing sell would strand the agent in a position
        # it asked to exit.
        if quantity > held + _QTY_EPSILON:
            log.debug(
                "account_store.apply_fill REJECT: insufficient holdings "
                "want=%s held=%s product=%s agent=%s",
                quantity,
                held,
                product_id,
                agent_id,
            )
            return TradeResult(
                False,
                f"Insufficient holdings. Want to sell {quantity} {product_id} "
                f"but only hold {held}.",
            )
        notional = price * quantity
        cash_in = notional - fee
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
        new_qty = held - quantity
        if new_qty <= _QTY_EPSILON:
            del account.positions[product_id]
            del account.cost_basis[product_id]
            account.avg_entry_ts.pop(product_id, None)
        else:
            account.positions[product_id] = new_qty
        account.trade_count += 1
        self._record_trade(
            agent_id,
            action,
            product_id,
            quantity,
            price,
            fee,
            latency,
            client_order_id,
            fee_asset,
            venue_order_id,
            replay=not enforce_margin,
        )
        log.debug(
            "account_store.apply_fill SELL OK: agent=%s %s %s @ $%.2f "
            "notional=$%.2f fee=$%.2f cash_after=$%.2f positions=%s trade_count=%d",
            agent_id,
            quantity,
            product_id,
            price,
            notional,
            fee,
            account.cash,
            dict(account.positions),
            account.trade_count,
        )
        return TradeResult(
            True,
            f"Sold {quantity} {product_id} @ ${price:,.2f} for ${cash_in:,.2f} "
            f"(${notional:,.2f} − ${fee:,.2f} fee{fee_rate}). "
            f"Cash remaining: ${account.cash:,.2f}.",
        )

    # ── perpetual futures ───────────────────────────────────────

    def _apply_perp_fill_locked(
        self,
        agent_id: str,
        action: str,
        product_id: str,
        quantity: float,
        price: float,
        fee: float,
        latency: float | None,
        fee_rate: str,
        client_order_id: str | None,
        fee_asset: str | None,
        venue_order_id: str | None,
        enforce_margin: bool = True,
    ) -> TradeResult:
        """Book one perpetual-futures fill as a change to a signed net position.

        ``buy`` adds ``+qty`` and ``sell`` adds ``-qty``, so one rule covers opening,
        adding, reducing, closing and flipping. Cash moves by ``-delta*price - fee``
        (opening a short credits its notional, as the venue's wallet does), and
        ``equity = cash + Σ qty*mark`` therefore needs no separate margin account.

        The 1:1 leverage cap is enforced here, not in the executor, because the spot
        and perp paths disagree about what "enough money" means: a perp may always be
        closed (even into a negative wallet), while opening more gross notional than
        equity is what the cap forbids. See design D3/D4.
        """
        account = self.get_or_create(agent_id)
        delta = quantity if action == "buy" else -quantity

        pos_old = account.positions.get(product_id, 0.0)
        if abs(pos_old) <= _QTY_EPSILON:
            pos_old = 0.0
        # A missing basis next to a live position means the row predates this model or
        # was hand-edited; adopt this fill's price as the entry so a close cannot book
        # the whole notional as profit, and let the next fill rebuild the average.
        basis_old = account.cost_basis.get(product_id, 0.0) if pos_old else 0.0
        if pos_old and basis_old == 0.0:
            basis_old = pos_old * price
        entry_old = basis_old / pos_old if pos_old else 0.0

        pos_new = pos_old + delta
        if abs(pos_new) <= _QTY_EPSILON:
            pos_new = 0.0

        positions_after = dict(account.positions)
        if pos_new == 0.0:
            positions_after.pop(product_id, None)
        else:
            positions_after[product_id] = pos_new

        cash_after = account.cash - delta * price - fee
        # Growing exposure is exactly "this fill leaves a bigger net position in this
        # contract": opening, adding on the same side, or flipping past the old size.
        # Reducing and closing never grow it, which is what keeps an underwater
        # position exitable no matter what the marks say.
        grows = abs(pos_new) > abs(pos_old) + _QTY_EPSILON

        if grows and enforce_margin:
            rejection = self._leverage_rejection(
                account,
                positions_after,
                cash_after,
                product_id=product_id,
                price=price,
            )
            if rejection is not None:
                return rejection

        # Realise only the part of the fill that closes the existing lot; the rest is
        # a new position at this fill price (a flip passes through both steps at once).
        closing = pos_old != 0.0 and (pos_old > 0) != (delta > 0)
        closed_qty = min(abs(delta), abs(pos_old)) if closing else 0.0
        closed_signed = (-closed_qty if pos_old > 0 else closed_qty) if closing else 0.0
        closed_pnl = (
            closed_qty * (price - entry_old) * (1.0 if pos_old > 0 else -1.0) if closed_qty else 0.0
        )

        remainder = delta - closed_signed
        basis_new = (pos_old + closed_signed) * entry_old + remainder * price

        account.cash = cash_after
        account.realized_pnl += closed_pnl - fee
        account.total_fees_paid += fee
        account.trade_count += 1

        now_ts = datetime.now().timestamp()
        if pos_new == 0.0:
            account.positions.pop(product_id, None)
            account.cost_basis.pop(product_id, None)
            account.avg_entry_ts.pop(product_id, None)
        else:
            account.positions[product_id] = pos_new
            account.cost_basis[product_id] = basis_new
            if pos_old and closed_qty and closed_qty >= abs(pos_old) - _QTY_EPSILON:
                # The old lot is fully gone (opened fresh or flipped): this is a new
                # position, so its holding time starts now.
                account.avg_entry_ts[product_id] = now_ts
            else:
                # Partial reduce, opening, or adding: the clock is the age of the
                # *exposure*, not the average age of its lots. Averaging let a 0.01 add
                # renew the hold, so a position could sit inside the floor's band
                # forever and never reach its escape hatch.
                account.avg_entry_ts.setdefault(product_id, now_ts)

        self._record_trade(
            agent_id,
            action,
            product_id,
            quantity,
            price,
            fee,
            latency,
            client_order_id,
            fee_asset,
            venue_order_id,
            replay=not enforce_margin,
        )

        gross_now = self._gross_notional(account.positions)
        equity_now = account.portfolio_value(self._price_book)
        gross_text = f"${gross_now:,.2f}" if gross_now is not None else "unpriced"
        side_label = "long" if pos_new > 0 else "short" if pos_new < 0 else "flat"
        if pos_old == 0.0:
            action_note = f"opened {side_label}"
        elif pos_new == 0.0:
            action_note = "closed"
        elif (pos_old > 0) != (pos_new > 0):
            # Crossing zero is a flip whatever the resulting size — the old lot is gone.
            action_note = f"flipped to {side_label}"
        elif abs(pos_new) > abs(pos_old):
            action_note = "added to"
        else:
            action_note = "reduced"

        notional = abs(delta) * price
        cash_note = (
            f"paid ${notional + fee:,.2f} (${notional:,.2f} + ${fee:,.2f} fee{fee_rate})"
            if delta > 0
            else f"received ${notional - fee:,.2f} (${notional:,.2f} − ${fee:,.2f} fee{fee_rate})"
        )
        log.debug(
            "account_store.apply_fill PERP OK: agent=%s %s %s %s @ $%.6f "
            "pos %.6f -> %.6f cash=%.2f gross=%s equity=%.2f realized=%+.2f",
            agent_id,
            action,
            quantity,
            product_id,
            price,
            pos_old,
            pos_new,
            account.cash,
            gross_text,
            equity_now,
            closed_pnl - fee,
        )
        return TradeResult(
            True,
            f"{action.capitalize()} {quantity:g} {product_id} @ ${price:,.2f}: "
            f"{cash_note}. Position {action_note}: net {pos_new:g} ({side_label}). "
            f"Cash ${account.cash:,.2f}, equity ${equity_now:,.2f}, "
            f"gross {gross_text} (1:1 cap ${equity_now:,.2f}). "
            f"Realized P&L on this fill: ${closed_pnl - fee:+,.2f}.",
        )

    def _leverage_rejection(
        self,
        account: AgentAccount,
        positions_after: dict[str, float],
        cash_after: float,
        *,
        product_id: str,
        price: float,
    ) -> TradeResult | None:
        """The 1:1 cap, as a refusal message: ``None`` means the fill may proceed.

        Called on every path that can *grow* exposure. Missing or stale marks fail
        closed — an unpriced book cannot prove the account is not levered — but only
        here, so closing a position is never blocked by a bad feed.
        """
        gross_after = self._gross_notional(positions_after, fill=(product_id, price))
        equity_after = self._equity_after(
            account, positions_after, cash_after, fill=(product_id, price)
        )
        if gross_after is None or equity_after is None:
            return TradeResult(
                False,
                f"No live mark for every held product, so the 1:1 leverage cap cannot "
                f"be checked for {product_id}. Refusing to add exposure on a "
                f"partially-priced or stale book; wait for the next price update.",
            )
        if equity_after <= 0:
            return TradeResult(
                False,
                f"Account equity is ${equity_after:,.2f} — there is no margin left to "
                f"open {product_id}. Close a position instead.",
            )
        if gross_after > equity_after + _MARGIN_EPSILON:
            held_here = abs(positions_after.get(product_id, 0.0)) * price
            allowed = max(0.0, equity_after - (gross_after - held_here))
            return TradeResult(
                False,
                f"This order would exceed the 1:1 leverage cap: gross notional "
                f"${gross_after:,.2f} against equity ${equity_after:,.2f}. "
                f"At most ${allowed:,.2f} of {product_id} can be held at this "
                f"equity — reduce the quantity.",
            )
        return None

    def _mark_price(self, product_id: str) -> float | None:
        """The live mark for one product, or ``None`` when it is missing or stale.

        No entry-price fallback: a mark that is really the cost basis would make the
        leverage gate look satisfied while the position is actually underwater.
        """
        entry = self._price_book.get(product_id)
        if entry is None:
            return None
        received_at = entry.get("_received_at")
        if received_at is not None:
            age = time.time() - float(received_at)
            if age > _MARK_STALENESS_SECONDS:
                log.warning(
                    "account_store: refusing to size leverage on a %.0fs-old %s quote",
                    age,
                    product_id,
                )
                return None
        return float(entry["price"])

    def _gross_notional(
        self,
        positions: dict[str, float],
        *,
        fill: tuple[str, float] | None = None,
    ) -> float | None:
        """Σ |qty| × mark — the exposure the 1:1 cap measures; ``None`` if unpriced.

        ``fill`` supplies the mark for the product being traded before the price book
        has learned this fill's price, so the gate never prices a fresh position at 0.
        """
        total = 0.0
        for product_id, quantity in positions.items():
            if fill is not None and product_id == fill[0]:
                mark = fill[1]
            else:
                mark = self._mark_price(product_id)
            if mark is None:
                return None
            total += abs(quantity) * mark
        return total

    def _equity_after(
        self,
        account: AgentAccount,
        positions: dict[str, float],
        cash: float,
        *,
        fill: tuple[str, float] | None = None,
    ) -> float | None:
        """Mark-to-market equity if this fill were booked; ``None`` when a mark is missing."""
        equity = cash
        for product_id, quantity in positions.items():
            if fill is not None and product_id == fill[0]:
                mark = fill[1]
            else:
                mark = self._mark_price(product_id)
            if mark is None:
                return None
            equity += quantity * mark
        return equity

    def _record_trade(
        self,
        agent_id: str,
        action: str,
        product_id: str,
        quantity: float,
        price: float,
        fee: float,
        latency: float | None = None,
        client_order_id: str | None = None,
        fee_asset: str | None = None,
        venue_order_id: str | None = None,
        replay: bool = False,
    ) -> None:
        # Every freshly booked fill arrives here — spot buy, spot sell, perp — and a
        # rejected one never does. That is exactly the population the churn guard
        # budgets: money actually paid for turnover, not intent.
        #
        # ``replay`` fills are excluded. They were paid by an earlier process and were
        # already budgeted then; stamping them with the current clock would spend a fresh
        # hour's budget on history and freeze an agent that has not traded yet.
        if self._churn_guard is not None and not replay:
            self._churn_guard.record(agent_id=agent_id, product_id=product_id, fee=fee)
        now = datetime.now()
        self._trade_log.append(
            TradeLogEntry(
                now.strftime("%H:%M:%S"),
                agent_id,
                action,
                product_id,
                quantity,
                price,
                fee,
                latency,
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
            stamp = datetime.now(timezone.utc).isoformat()
            try:
                if client_order_id is None:
                    self._ledger.save_fill(
                        agent_id=agent_id,
                        account=account,
                        timestamp=stamp,
                        action=action,
                        product_id=product_id,
                        quantity=quantity,
                        price=price,
                        fee=fee,
                        cash_after=account.cash,
                        latency=latency,
                    )
                else:
                    # A venue fill closes its own journal row in the same transaction,
                    # so "booked" and "journal closed" can never disagree.
                    self._ledger.settle_fill(
                        client_order_id=client_order_id,
                        agent_id=agent_id,
                        account=account,
                        timestamp=stamp,
                        action=action,
                        product_id=product_id,
                        quantity=quantity,
                        price=price,
                        fee=fee,
                        fee_asset=fee_asset,
                        cash_after=account.cash,
                        latency=latency,
                        venue_order_id=venue_order_id,
                        executed_qty=quantity,
                        cum_notional=quantity * price,
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
                    agent_id,
                    action,
                    product_id,
                    quantity,
                )
