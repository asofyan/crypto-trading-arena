"""The live session: when the engine may place real orders, and why it may not.

Fakes stand in for the venue; the ledger, the store and the comparing logic are real,
because the ordering between "resolve dangling orders" and "compare balances" is the
thing being tested.
"""

from __future__ import annotations

import json

import pytest

from arena.account_store import AccountStore
from arena.fees import FeeModel
from arena.instruments import parse_exchange_info
from arena.ledger import Ledger
from arena.live_session import ARMED, HALTED, RECONCILING, LiveSession, LiveState
from arena.price_book import PriceBook

SOLUSDT_INFO = {
    "symbols": [
        {
            "symbol": "SOLUSDT",
            "status": "TRADING",
            "baseAsset": "SOL",
            "quoteAsset": "USDT",
            "filters": [
                {"filterType": "LOT_SIZE", "minQty": "0.001", "stepSize": "0.001"},
                {"filterType": "NOTIONAL", "minNotional": "5", "applyMinToMarket": True},
            ],
        }
    ]
}
RULES = parse_exchange_info(SOLUSDT_INFO)


class FakeBalance:
    def __init__(self, free: float, locked: float = 0.0) -> None:
        self.free = free
        self.locked = locked
        self.total = free + locked


class FakeClient:
    def __init__(self, balances: dict[str, float], *, open_orders: int = 0) -> None:
        self._balances = balances
        self._open = [{"id": i} for i in range(open_orders)]
        self.calls: list[str] = []

    def balances(self) -> dict[str, FakeBalance]:
        self.calls.append("balances")
        return {asset: FakeBalance(amount) for asset, amount in self._balances.items()}

    def open_orders(self) -> list[dict]:
        return list(self._open)

    def set_balance(self, asset: str, amount: float) -> None:
        self._balances[asset] = amount


class FakeExecutor:
    """Records that dangling orders were resolved, and when."""

    def __init__(self, client: FakeClient | None = None, effect=None) -> None:
        self.calls = 0
        self._client = client
        self._effect = effect

    def settle_dangling(self):
        self.calls += 1
        if self._client is not None:
            self._client.calls.append("settle_dangling")
        if self._effect is not None:
            self._effect()
        return []


def _build(
    tmp_path,
    *,
    balances=None,
    cash: float = 95.0,
    position: float = 0.5,
    open_orders: int = 0,
    with_baseline: bool = True,
    halt: bool = False,
    settle_effect=None,
    deposit_policy: str = "credit_sole_agent",
    agents: tuple[str, ...] = ("a1",),
    sync_tolerance: float = 1.0,
):
    ledger = Ledger(tmp_path / "arena.db")
    store = AccountStore(PriceBook(), FeeModel(taker_bps=10))
    store.attach_ledger(ledger)
    account = store.get_or_create("a1")
    account.cash = cash
    if position:
        account.positions["SOLUSDT"] = position
        account.cost_basis["SOLUSDT"] = position * 100.0
    ledger.persist_account("a1", account)
    if with_baseline:
        ledger.set_meta("live_baseline", json.dumps({"USDT": 0.0, "SOL": 0.0}))

    client = FakeClient(
        balances if balances is not None else {"USDT": cash, "SOL": position},
        open_orders=open_orders,
    )
    executor = FakeExecutor(client, settle_effect)
    session = LiveSession(
        client=client,
        store=store,
        ledger=ledger,
        executor=executor,
        rules=RULES,
        mapped_agents=agents,
        deposit_policy=deposit_policy,
        sync_tolerance_usdt=sync_tolerance,
        halt_path=tmp_path / "LIVE_HALT",
        start_refresher=False,
    )
    if halt:
        session.state.halt_path.write_text("operator\n")
    return session, store, ledger, client, executor


# ── Arming ─────────────────────────────────────────────────────


def test_a_reconciled_wallet_arms_itself(tmp_path):
    session, _, ledger, _, _ = _build(tmp_path)

    report = session.start()

    assert report.armed
    assert session.state.state == ARMED
    assert ledger.get_meta("live_state") == ARMED


def test_a_shortfall_keeps_the_session_disarmed(tmp_path):
    session, _, ledger, _, _ = _build(tmp_path, balances={"USDT": 95.0, "SOL": 0.0})

    report = session.start()

    assert not report.armed
    assert session.state.state == HALTED
    assert "short 0.5" in session.state.reason
    assert ledger.get_meta("live_state") == HALTED
    # No halt flag: the next start reconciles again and may arm by itself.
    assert not session.state.halt_path.exists()


def test_an_operator_halt_survives_a_clean_reconcile(tmp_path):
    session, _, _, _, _ = _build(tmp_path, halt=True)

    report = session.start()

    assert report.armed  # the numbers agree…
    assert session.state.state == HALTED  # …but a human said stop
    assert "operator halt" in session.state.reason


def test_a_missing_baseline_keeps_the_session_disarmed(tmp_path):
    session, _, _, _, _ = _build(tmp_path, with_baseline=False)

    assert not session.start().armed
    assert "baseline" in session.state.reason


def test_open_orders_keep_the_session_disarmed(tmp_path):
    session, _, _, _, _ = _build(tmp_path, open_orders=2)

    assert not session.start().armed
    assert "open order" in session.state.reason


def test_dangling_orders_are_resolved_before_the_balances_are_compared(tmp_path):
    session, _, _, client, executor = _build(tmp_path)

    session.start()

    assert executor.calls == 1
    # the settle must happen first, or a just-found fill would look like drift
    assert client.calls[:2] == ["settle_dangling", "balances"]


def test_the_state_gate_is_what_the_risk_gates_consult(tmp_path):
    session, _, _, _, _ = _build(tmp_path)
    assert session.state.is_halted()  # RECONCILING: not armed, not tradable

    session.start()
    assert not session.state.is_halted()

    session.state.set(HALTED, "test")
    assert session.state.is_halted()


# ── Mid-session ────────────────────────────────────────────────


def test_drift_appearing_mid_session_disarms(tmp_path):
    session, _, _, client, _ = _build(tmp_path)
    session.start()
    assert session.state.state == ARMED

    client.set_balance("SOL", 0.0)
    report = session.recheck()

    assert not report.armed
    assert session.state.state == HALTED


def test_a_cleared_drift_does_not_silently_re_arm(tmp_path):
    session, _, _, client, _ = _build(tmp_path)
    session.start()
    client.set_balance("SOL", 0.0)
    session.recheck()
    assert session.state.state == HALTED

    client.set_balance("SOL", 0.5)
    session.recheck()

    # Re-arming is a human decision: `manage_bot.py resume`, or a restart.
    assert session.state.state == HALTED


def test_a_stable_deposit_is_credited_mid_session(tmp_path):
    session, store, ledger, client, _ = _build(tmp_path)
    session.start()
    client.set_balance("USDT", 145.0)  # +50 by hand

    session.recheck()  # first sighting: recorded, not yet credited
    assert store.get_or_create("a1").cash == pytest.approx(95.0)

    session.recheck()  # second sighting: credited
    account = store.get_or_create("a1")
    assert account.cash == pytest.approx(145.0)
    events = ledger.equity_events()
    assert len(events) == 1
    assert events[0]["kind"] == "deposit"
    assert events[0]["amount"] == pytest.approx(50.0)
    assert session.state.state == ARMED  # a deposit is not drift


def test_an_unallocated_deposit_never_reaches_an_agent(tmp_path):
    session, store, ledger, client, _ = _build(tmp_path, deposit_policy="unallocated")
    session.start()
    client.set_balance("USDT", 145.0)

    session.recheck()
    session.recheck()

    assert store.get_or_create("a1").cash == pytest.approx(95.0)
    assert ledger.equity_events() == []
    assert session.last_adoption is not None
    assert "unallocated" in session.last_adoption.note


def test_a_failed_balance_read_does_not_disarm(tmp_path):
    """A network blip is not drift; only a proven mismatch may disarm."""
    session, _, _, client, _ = _build(tmp_path)
    session.start()

    def explode() -> None:
        raise OSError("venue unreachable")

    client.balances = explode
    with pytest.raises(OSError):
        session.recheck()
    assert session.state.state == ARMED


def test_status_reports_state_reason_and_drift(tmp_path):
    session, _, _, _, _ = _build(tmp_path, balances={"USDT": 95.0, "SOL": 0.0})
    session.start()

    status = session.status()

    assert status["state"] == HALTED
    assert status["halted"] is True
    assert status["drift"] and "short 0.5" in status["drift"][0]


def test_stopping_a_session_without_a_refresher_is_a_noop(tmp_path):
    session, _, _, _, _ = _build(tmp_path)
    session.start()

    session.stop()  # must not hang or raise

    assert session.state.state == ARMED


def test_state_roundtrips_through_the_ledger(tmp_path):
    session, _, ledger, _, _ = _build(tmp_path)
    session.start()

    reloaded = LiveState(ledger, halt_path=tmp_path / "LIVE_HALT")
    reloaded.set(RECONCILING, "")

    assert ledger.get_meta("live_state") == RECONCILING
    assert ledger.get_meta("live_halt_reason") == ""
