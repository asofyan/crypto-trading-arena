"""Arming and reconciliation for a dedicated Binance USD-M futures wallet."""

from __future__ import annotations

import json
import logging
import math
import threading
import time
from dataclasses import dataclass, replace
from typing import Callable

from arena.instruments import perp_product_id, venue_symbol
from arena.ledger import (
    FUTURES_FUNDING_CHECKPOINT_META_KEY,
    FUTURES_FUNDING_START_META_KEY,
    FuturesFunding,
)
from arena.ledger import FUTURES_LEDGER_META_KEY as FUTURES_LEDGER_META_KEY
from arena.live_session import ARMED, HALTED, RECONCILING, LiveState
from arena.models import AgentAccount
from arena.reconcile import DEFAULT_QUOTE_ASSET, Drift, ReconcileReport

log = logging.getLogger(__name__)

# Buffer for roundoff in reported commission, execution prices, and realized P&L.
_WALLET_TOLERANCE = 0.001
_DAY_MS = 86_400_000
_FUNDING_WINDOW_MS = 7 * _DAY_MS
_INCOME_LIMIT = 1000
_RECONCILIATION_RETRY_SECONDS = 5.0


@dataclass(frozen=True)
class _Wallet:
    alias: str
    balance: float
    available: float


def _finite(value) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("non-finite futures balance or position")
    return result


def _unix_ms() -> int:
    return time.time_ns() // 1_000_000


class FuturesLiveSession:
    """Prove balances, positions and venue settings before arming and while serving."""

    def __init__(
        self,
        client,
        store,
        ledger,
        executor,
        rules,
        agent_id: str,
        *,
        environment: str,
        reserve_usdt: float = 0.0,
        refresh_seconds: float = 30.0,
        state: LiveState | None = None,
        start_refresher: bool = True,
        now_ms: Callable[[], int] = _unix_ms,
    ) -> None:
        self._client = client
        self._store = store
        self._ledger = ledger
        self._executor = executor
        self._venue_symbols = frozenset(venue_symbol(product) for product in rules)
        if not self._venue_symbols:
            raise ValueError("live futures requires at least one allowed symbol")
        self._agent_id = agent_id
        self._environment = environment
        self._reserve = reserve_usdt
        self._refresh_seconds = refresh_seconds
        self.state = state or LiveState(ledger)
        self.last_report: ReconcileReport | None = None
        self._startup_pending = False
        self._reconciliation_halt_reason: str | None = None
        self._refresher: threading.Thread | None = None
        self._stop = threading.Event()
        self._start_refresher = start_refresher
        self._now_ms = now_ms

    @property
    def ledger_reload_required(self) -> bool:
        return self._executor is not None and self._executor.restart_required

    def start(self) -> ReconcileReport:
        with self._store.write_lock:
            self.state.set(RECONCILING, "startup")
            report = self._check(bootstrap=True)
            self._startup_pending = not report.armed
            self._reconciliation_halt_reason = None
            halt_reason = self.state.halt_file_reason()
            if halt_reason is not None:
                self.state.set(HALTED, f"operator halt remains in effect ({halt_reason})")
            elif self.state.state == RECONCILING:
                if report.armed:
                    self.state.set(ARMED)
                else:
                    self._halt_for_reconciliation(report)

        if self._start_refresher and self._refresher is None:
            self._refresher = threading.Thread(
                target=self._refresh_loop, name="futures-reconcile", daemon=True
            )
            self._refresher.start()
        return report

    def stop(self) -> None:
        self._stop.set()
        if self._refresher is not None and self._refresher.is_alive():
            self._refresher.join(timeout=self._refresh_seconds + 5)

    def _refresh_loop(self) -> None:
        while True:
            report = self.last_report
            delay = (
                _RECONCILIATION_RETRY_SECONDS
                if report is not None and (not report.armed or self.state.state == HALTED)
                else self._refresh_seconds
            )
            if self._stop.wait(delay):
                return
            try:
                self.recheck()
            except Exception:
                # LiveState sets its in-memory state before persisting. A ledger
                # write failure here leaves the session disarmed as well.
                log.exception("futures live session could not persist reconciliation state")

    def recheck(self) -> ReconcileReport:
        """Recover a halted Demo session once the wallet is verified again."""
        with self._store.write_lock:
            report = self._check(bootstrap=self._startup_pending)
            # Order settlement during the check can issue its own halt.
            halted_by_reconciliation = (
                self.state.state == HALTED
                and self.state.reason == self._reconciliation_halt_reason
            )
            if report.armed:
                self._startup_pending = False
                can_resume = halted_by_reconciliation or (
                    self._environment == "demo" and self.state.state == HALTED
                )
                if can_resume and self.state.halt_file_reason() is None:
                    self.state.set(ARMED)
                    self._reconciliation_halt_reason = None
            elif self.state.state != HALTED or halted_by_reconciliation:
                self._halt_for_reconciliation(report)
            return report

    def _halt_for_reconciliation(self, report: ReconcileReport) -> None:
        reason = report.first_blocker()
        self._reconciliation_halt_reason = reason
        self.state.set(HALTED, reason)

    def _check(self, *, bootstrap: bool) -> ReconcileReport:
        try:
            report = self._reconcile(bootstrap=bootstrap)
        except Exception as exc:
            detail = f"futures reconciliation failed: {exc}"
            log.warning("%s", detail)
            report = ReconcileReport(blocking=(Drift("reconcile_failed", "venue", 0, 0, detail),))
        self.last_report = report
        return report

    def _wallet(self) -> _Wallet:
        rows = [row for row in self._client.futures_balances() if row["asset"] == DEFAULT_QUOTE_ASSET]
        if len(rows) != 1 or not isinstance(rows[0].get("accountAlias"), str):
            raise ValueError("cannot identify the venue USDT wallet")
        row = rows[0]
        if not row["accountAlias"]:
            raise ValueError("venue USDT wallet has no account alias")
        return _Wallet(row["accountAlias"], _finite(row["balance"]), _finite(row["availableBalance"]))

    def _binding(self, wallet: _Wallet) -> str:
        return json.dumps(
            {
                "agent": self._agent_id,
                "environment": self._environment,
                "reserve_usdt": self._reserve,
                "venue_account_alias": wallet.alias,
            },
            sort_keys=True,
        )

    def _venue_positions(self) -> dict[str, tuple[float, float]]:
        positions = {}
        for row in self._client.position_risk():
            product = perp_product_id(row["symbol"])
            if row["positionSide"] != "BOTH" or product in positions:
                raise ValueError(f"{product}: expected one net one-way futures position")
            qty = _finite(row["positionAmt"])
            if qty:
                positions[product] = (qty, _finite(row["entryPrice"]))
        return positions

    def _check_settings(self) -> None:
        config = self._client.account_config()
        if config.get("canTrade") is not True:
            raise ValueError("venue account does not permit trading")
        if config.get("dualSidePosition") is not False:
            raise ValueError("venue account must use one-way position mode")
        if config.get("multiAssetsMargin") is not False:
            raise ValueError("venue account must use single-asset margin")
        if self._client.bnb_fee_burn() is not False:
            raise ValueError("venue account must disable BNB fee burn to settle commission in USDT")
        settings = {row["symbol"]: row for row in self._client.symbol_config()}
        for symbol in sorted(self._venue_symbols):
            row = settings.get(symbol, {})
            if row.get("leverage") != 1 or row.get("marginType") != "CROSSED":
                raise ValueError(f"{symbol}: venue must use 1× leverage and cross margin")

    def _reconcile(self, *, bootstrap: bool) -> ReconcileReport:
        if self.ledger_reload_required:
            raise ValueError("futures tool server must reload the ledger before trading")
        started_ms = self._now_ms()
        wallet = self._wallet()
        binding = self._binding(wallet)
        existing = self._ledger.get_meta(FUTURES_LEDGER_META_KEY)
        if existing is not None and existing != binding:
            raise ValueError("ledger binding does not match the venue account, environment, agent or reserve")
        if existing is None and not bootstrap:
            raise ValueError("futures ledger has no verified binding")

        self._check_settings()
        if existing:
            accounts = self._store.accounts
            if self._agent_id not in accounts:
                raise ValueError("bound futures agent is missing from the ledger")
            account = accounts[self._agent_id]
            if bootstrap or self._ledger.dangling_orders():
                self._executor.settle_dangling()
                wallet = self._wallet()
                if self._binding(wallet) != binding:
                    raise ValueError("venue wallet binding changed during settlement")
        else:
            # Bootstrap only from proven free USD-M collateral in a flat account.
            if wallet.available < wallet.balance - _WALLET_TOLERANCE:
                raise ValueError("wallet has unavailable collateral before initialization")
            cash = wallet.balance - self._reserve
            if cash <= 0:
                raise ValueError("wallet has no trading balance after reserve")
            account = AgentAccount(cash=cash)

        positions = self._venue_positions()
        open_orders = len(self._client.open_orders()) + len(self._client.open_algo_orders())
        blocking = self._compare(account, wallet, positions, open_orders)
        if existing is None:
            if not blocking:
                # The durable marker and collateral land together after every check.
                self._ledger.initialize_futures(self._agent_id, account, binding, started_ms)
                self._store.accounts[self._agent_id] = account
        elif len(blocking) == 1 and blocking[0].kind == "wallet_drift":
            difference = wallet.balance - blocking[0].expected
            if self._reconcile_funding(account, wallet, difference):
                blocking.clear()
        elif not blocking:
            self._advance_funding_checkpoint(started_ms)

        return ReconcileReport(
            blocking=tuple(blocking),
            balances={DEFAULT_QUOTE_ASSET: wallet.balance},
            ledger_cash=account.cash,
            ledger_positions=dict(account.positions),
        )

    def _funding_checkpoint(self) -> tuple[int, int]:
        started = self._ledger.get_meta(FUTURES_FUNDING_START_META_KEY)
        verified = self._ledger.get_meta(FUTURES_FUNDING_CHECKPOINT_META_KEY)
        if started is None or verified is None:
            raise ValueError("futures ledger is missing a funding reconciliation checkpoint")
        return int(started), int(verified)

    def _advance_funding_checkpoint(self, now_ms: int) -> None:
        _, verified = self._funding_checkpoint()
        # A healthy wallet match establishes a checkpoint. Keep an overlap when
        # querying later so delayed funding records can still be attributed.
        if now_ms - verified >= _DAY_MS:
            self._ledger.set_meta(FUTURES_FUNDING_CHECKPOINT_META_KEY, str(now_ms))

    def _reconcile_funding(self, account: AgentAccount, wallet: _Wallet, difference: float) -> bool:
        started, verified = self._funding_checkpoint()
        since = max(started, verified - _FUNDING_WINDOW_MS)
        through = self._now_ms()
        if not 0 <= through - since < 85 * _DAY_MS:
            raise ValueError("cannot prove full funding history for the reconciliation window")

        funding: dict[int, FuturesFunding] = {}
        for start in range(since, through + 1, _FUNDING_WINDOW_MS):
            end = min(through, start + _FUNDING_WINDOW_MS - 1)
            rows = self._client.funding_income(start, end)
            if len(rows) >= _INCOME_LIMIT:
                raise ValueError("venue truncated funding income history")
            for row in rows:
                event = self._funding_event(row, start, end)
                previous = funding.get(event.transaction_id)
                if previous is not None and previous != event:
                    raise ValueError("venue returned conflicting funding transactions")
                if self._ledger.get_meta(event.meta_key) is None:
                    funding[event.transaction_id] = event

        events = sorted(funding.values(), key=lambda event: (event.time_ms, event.transaction_id))
        net = math.fsum(event.amount for event in events)
        if not events or abs(net - difference) > _WALLET_TOLERANCE:
            return False
        current = self._wallet()
        if current.alias != wallet.alias or current.balance != wallet.balance:
            return False
        updated = replace(account, cash=account.cash + net)
        self._ledger.settle_futures_funding(
            self._agent_id, updated, events,
            account.portfolio_value(self._store.price_book), through,
        )
        account.cash = updated.cash
        return True

    def _funding_event(self, row: dict, start: int, end: int) -> FuturesFunding:
        ts = int(row["time"])
        transaction_id = int(row["tranId"])
        if (
            row["incomeType"] != "FUNDING_FEE"
            or row["asset"] != DEFAULT_QUOTE_ASSET
            or row["symbol"] not in self._venue_symbols
            or not start <= ts <= end
            or transaction_id <= 0
        ):
            raise ValueError("venue returned unverifiable USD-M funding income")
        return FuturesFunding(transaction_id, ts, _finite(row["income"]))

    def _compare(
        self,
        account: AgentAccount,
        wallet: _Wallet,
        positions: dict[str, tuple[float, float]],
        open_orders: int,
    ) -> list[Drift]:
        blocking = []
        for product in sorted(set(account.positions) | set(positions)):
            expected_qty = _finite(account.positions.get(product, 0.0))
            actual_qty, entry = positions.get(product, (0.0, 0.0))
            if venue_symbol(product) not in self._venue_symbols or not math.isclose(
                expected_qty, actual_qty, abs_tol=1e-10, rel_tol=1e-12
            ):
                blocking.append(Drift(
                    "position_drift", product, expected_qty, actual_qty,
                    f"{product}: ledger position {expected_qty:g}, venue {actual_qty:g}",
                ))
            elif expected_qty:
                expected_entry = _finite(account.avg_cost_per_unit(product))
                if entry <= 0 or not math.isclose(expected_entry, entry, abs_tol=1e-8, rel_tol=1e-8):
                    blocking.append(Drift(
                        "entry_drift", product, expected_entry, entry,
                        f"{product}: ledger entry {expected_entry:g}, venue {entry:g}",
                    ))

        # Undo the synthetic purchase/sale principal carried in ledger cash. USD-M
        # settles realized P&L, commissions and funding into wallet balance instead.
        expected_balance = _finite(account.cash + math.fsum(account.cost_basis.values()) + self._reserve)
        if abs(wallet.balance - expected_balance) > _WALLET_TOLERANCE:
            blocking.append(Drift(
                "wallet_drift", DEFAULT_QUOTE_ASSET, expected_balance, wallet.balance,
                f"wallet balance drift: ledger {expected_balance:.8f} USDT, venue "
                f"{wallet.balance:.8f} USDT; check funding and external transfers",
            ))
        if open_orders:
            blocking.append(Drift(
                "unaligned_open_orders", "open_orders", 0, open_orders,
                f"{open_orders} open order(s), including conditional orders, at the USD-M venue",
            ))
        dangling = len(self._ledger.dangling_orders())
        if dangling:
            blocking.append(Drift(
                "unresolved_orders", "journal", 0, dangling,
                f"{dangling} unresolved order(s) in the futures journal",
            ))
        if self._store.ledger_degraded:
            blocking.append(Drift(
                "ledger_degraded", "journal", 0, 1, "futures fill could not be persisted",
            ))
        return blocking

    def status(self) -> dict:
        report = self.last_report
        return {
            "state": self.state.state,
            "reason": self.state.reason,
            "halted": self.state.is_halted(),
            "drift": [drift.detail for drift in report.blocking] if report else [],
            "notes": [],
            "unallocated_usdt": 0.0,
        }
