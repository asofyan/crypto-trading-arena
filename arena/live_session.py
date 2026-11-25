"""The live session: arming state, startup reconciliation, and the balance refresher.

Arming is the whole point of this module. A process may only place real orders when it
has *proved* — against the venue, not against its own memory — that the ledger
describes the wallet. Anything else is `HALTED`, and the arena keeps running so the
operator can see why.

Two different stop signals, on purpose:

- ``data/LIVE_HALT`` (the file) means **do not trade until a human says so**. An
  operator halt, an IP ban, a credential problem. It survives restarts.
- ``live_state != ARMED`` means **this process could not prove it is safe**. Drift, a
  shortfall, a half-known order. The next start reconciles again and may arm itself —
  which is exactly the "stop anytime, restart, carry on" requirement.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Callable

from arena.reconcile import (
    DEFAULT_QUOTE_ASSET,
    Adoption,
    EquitySync,
    ReconcileReport,
    compare,
)
from arena.risk import DEFAULT_HALT_PATH

log = logging.getLogger(__name__)

RECONCILING = "RECONCILING"
ARMED = "ARMED"
HALTED = "HALTED"
STATE_META_KEY = "live_state"
REASON_META_KEY = "live_halt_reason"


class LiveState:
    """Where the session stands, persisted so `manage_bot.py status` can read it."""

    def __init__(self, ledger, *, halt_path: Path = DEFAULT_HALT_PATH) -> None:
        self._ledger = ledger
        self.halt_path = halt_path
        self._state = RECONCILING
        self._reason = ""

    @property
    def state(self) -> str:
        return self._state

    @property
    def reason(self) -> str:
        return self._reason

    def is_halted(self) -> bool:
        """The single predicate every risk gate consults, per order."""
        return self.halt_path.exists() or self._state != ARMED

    def set(self, state: str, reason: str = "") -> None:
        # No "unchanged, skip the write" shortcut: the in-memory state always starts at
        # RECONCILING (fail closed) while the persisted one may say ARMED, so skipping on
        # equality would leave the ledger claiming a state this process never proved.
        self._state = state
        self._reason = reason
        self._ledger.set_meta(STATE_META_KEY, state)
        self._ledger.set_meta(REASON_META_KEY, reason)
        log.warning("live session state: %s %s", state, f"— {reason}" if reason else "")

    def halt_file_reason(self) -> str | None:
        try:
            return self.halt_path.read_text(encoding="utf-8").strip() or "halt flag set"
        except OSError:
            return "halt flag set" if self.halt_path.exists() else None


class LiveSession:
    """Reconcile at startup, then keep the ledger and the wallet in step."""

    def __init__(
        self,
        client,
        store,
        ledger,
        executor,
        rules: dict,
        mapped_agents: tuple[str, ...],
        *,
        quote_asset: str = DEFAULT_QUOTE_ASSET,
        reserve_usdt: float = 0.0,
        deposit_policy: str = "credit_sole_agent",
        sync_tolerance_usdt: float = 1.0,
        refresh_seconds: float = 30.0,
        halt_path: Path = DEFAULT_HALT_PATH,
        state: LiveState | None = None,
        now: Callable[[], float] = time.time,
        start_refresher: bool = True,
    ) -> None:
        self._client = client
        self._store = store
        self._ledger = ledger
        self._executor = executor
        self._rules = rules
        self._agents = tuple(mapped_agents)
        self._quote_asset = quote_asset
        self._reserve = reserve_usdt
        self._refresh_seconds = refresh_seconds
        self._now = now
        # Shared with the risk gates, so "armed" and "may place an order" can never be
        # two different answers.
        self.state = state or LiveState(ledger, halt_path=halt_path)
        self.equity_sync = EquitySync(
            policy=deposit_policy,
            tolerance_usdt=sync_tolerance_usdt,
            mapped_agents=self._agents,
        )
        self._refresher: threading.Thread | None = None
        self._stop = threading.Event()
        self._start_refresher = start_refresher
        self.last_report: ReconcileReport | None = None
        self.last_adoption: Adoption | None = None

    # ── Lifecycle ────────────────────────────────────────────────

    def start(self) -> ReconcileReport:
        """Settle anything unresolved, reconcile, then arm or halt."""
        self.state.set(RECONCILING, "startup")
        self._settle_dangling()
        try:
            report = self.reconcile()
        except Exception as exc:  # noqa: BLE001 — a bootstrap failure must not trade
            self.state.set(HALTED, f"reconcile failed: {exc}")
            log.exception("live session: startup reconcile failed")
            return ReconcileReport()

        halt_reason = self.state.halt_file_reason()
        if halt_reason is not None:
            self.state.set(HALTED, f"operator halt remains in effect ({halt_reason})")
        elif report.armed:
            self.state.set(ARMED)
        else:
            # No halt file: a restart re-reconciles and may arm on its own. Only a human
            # halt (or a hard venue failure) is made permanent.
            self.state.set(HALTED, report.first_blocker())

        if self._start_refresher:
            self._start_refresher_thread()
        return report

    def stop(self) -> None:
        self._stop.set()
        refresher = self._refresher
        if refresher is not None and refresher.is_alive():
            refresher.join(timeout=self._refresh_seconds + 5)

    def _start_refresher_thread(self) -> None:
        if self._refresher is not None:
            return
        self._refresher = threading.Thread(
            target=self._refresh_loop, name="live-balance-refresh", daemon=True
        )
        self._refresher.start()

    def _refresh_loop(self) -> None:
        # A deposit is adopted mid-session; drift disarms. Both are cheap reads against
        # GET /api/v3/account (weight 20), so 30s is ~40 weight/min of a 6000 budget.
        while not self._stop.wait(self._refresh_seconds):
            try:
                self.recheck()
            except Exception:  # noqa: BLE001 — never let the thread die silently
                log.exception("live session: balance refresh failed")

    # ── Reconciliation ───────────────────────────────────────────

    def reconcile(self) -> ReconcileReport:
        balances = {
            asset: balance.total for asset, balance in self._client.balances().items()
        }
        rules = self._rules
        expected_positions: dict[str, float] = {}
        cash = 0.0
        for agent in self._agents:
            account = self._store.get_or_create(agent)
            cash += account.cash
            for symbol, quantity in account.positions.items():
                expected_positions[symbol] = expected_positions.get(symbol, 0.0) + quantity

        report = compare(
            balances=balances,
            baseline=self._baseline(),
            ledger_positions=expected_positions,
            ledger_cash=cash,
            rules=rules,
            quote_asset=self._quote_asset,
            reserve_usdt=self._reserve,
            open_orders=len(self._client.open_orders()),
        )
        self.last_report = report
        return report

    def recheck(self) -> ReconcileReport:
        """Mid-session: disarm on drift, adopt a stable deposit, never silently re-arm."""
        report = self.reconcile()

        if not report.armed:
            self.state.set(HALTED, report.first_blocker())
            return report

        if self.state.state == HALTED and not self.state.halt_path.exists():
            # The drift is gone, but mid-session arming is a human decision (resume or
            # restart). Re-arming here would let a transient mismatch hide itself.
            log.info("live session: drift cleared; run `manage_bot.py resume` to re-arm")

        adoption = self.equity_sync.observe(report, self._equity_by_agent())
        if adoption is not None:
            self._apply_adoption(adoption)
        return report

    def _apply_adoption(self, adoption: Adoption) -> None:
        if not adoption.allocations:
            log.info("live session: %s (%.2f %s)", adoption.note, adoption.amount,
                     self._quote_asset)
            self.last_adoption = adoption
            return

        for agent, amount in adoption.allocations.items():
            before, after = self._store.apply_equity_adjustment(agent, amount)
            self._ledger.record_equity_event(
                kind="deposit",
                amount=amount,
                equity_before=before,
                equity_after=after,
                agent_id=agent,
                note=adoption.note,
            )
            log.info("live session: credited %.2f %s to %s", amount, self._quote_asset, agent)
        self.last_adoption = adoption

    def _settle_dangling(self) -> None:
        """Resolve orders whose outcome this process never learned, before comparing."""
        if self._executor is None:
            return
        settled = self._executor.settle_dangling()
        if settled:
            log.warning("live session: resolved %d dangling order(s) at startup", len(settled))

    # ── Helpers ──────────────────────────────────────────────────

    def _baseline(self) -> dict[str, float]:
        raw = self._ledger.get_meta("live_baseline")
        if not raw:
            return {}
        try:
            import json

            parsed = json.loads(raw)
        except ValueError:
            log.error("live_baseline is not JSON (%r); treating it as missing", raw)
            return {}
        if not isinstance(parsed, dict):
            return {}
        return {str(asset): float(quantity) for asset, quantity in parsed.items()}

    def _equity_by_agent(self) -> dict[str, float]:
        return {
            agent: self._store.get_or_create(agent).portfolio_value(self._store.price_book)
            for agent in self._agents
        }

    def status(self) -> dict:
        report = self.last_report
        return {
            "state": self.state.state,
            "reason": self.state.reason,
            "halted": self.state.is_halted(),
            "drift": [d.detail for d in report.blocking] if report else [],
            "notes": [d.detail for d in report.notes] if report else [],
            "unallocated_usdt": report.quote_surplus if report else 0.0,
        }
