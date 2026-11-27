import sqlite3
from dataclasses import replace
from decimal import Decimal
from threading import Event
from types import SimpleNamespace

import pytest

from arena.account_store import AccountStore
from arena.futures_session import FUTURES_LEDGER_META_KEY, FuturesLiveSession
from arena.instruments import SymbolRules
from arena.ledger import (
    FUTURES_FUNDING_CHECKPOINT_META_KEY,
    FUTURES_FUNDING_START_META_KEY,
    FuturesFunding,
    Ledger,
)
from arena.live_session import ARMED, HALTED, LiveState
from arena.models import AgentAccount
from arena.price_book import PriceBook


class Venue:
    def __init__(self):
        self.alias = "the futures account"
        self.config = {"canTrade": True, "dualSidePosition": False, "multiAssetsMargin": False}
        self.symbols = [{"symbol": "BTCUSDT", "leverage": 1, "marginType": "CROSSED"}]
        self.positions = []
        self.orders = []
        self.algos = []
        self.wallet = "250"
        self.available = None
        self.fail = False
        self.burn_fees = False
        self.funding = []

    def account_config(self):
        if self.fail:
            raise ValueError("venue read failed")
        return self.config

    def bnb_fee_burn(self):
        return self.burn_fees

    def funding_income(self, start_time_ms, end_time_ms):
        return [row for row in self.funding if start_time_ms <= row["time"] <= end_time_ms]

    def symbol_config(self):
        return self.symbols

    def position_risk(self):
        return self.positions

    def futures_balances(self):
        return [
            {
                "asset": "USDT",
                "accountAlias": self.alias,
                "balance": self.wallet,
                "availableBalance": self.wallet if self.available is None else self.available,
            }
        ]

    def open_orders(self):
        return self.orders

    def open_algo_orders(self):
        return self.algos


def make_session(ledger, venue, tmp_path, *, environment="demo", reserve=10, **session_kwargs):
    store = AccountStore(PriceBook())
    store.attach_ledger(ledger)
    recovered = []
    session = FuturesLiveSession(
        venue,
        store,
        ledger,
        SimpleNamespace(
            restart_required=False,
            settle_dangling=lambda: (recovered.append("settled"), [])[1],
        ),
        {
            "BTCUSDT-PERP": SymbolRules(
                symbol="BTCUSDT", base_asset="BTC", quote_asset="USDT",
                min_qty=Decimal("0.001"), step_size=Decimal("0.001"),
                min_notional=Decimal("5"), tick_size=Decimal("0.1"),
            ),
        },
        "agent",
        environment=environment,
        reserve_usdt=reserve,
        state=LiveState(ledger, halt_path=tmp_path / "HALT"),
        start_refresher=False,
        **session_kwargs,
    )
    return session, store, recovered


def test_pristine_futures_ledger_seeds_from_venue_and_survives_restart(tmp_path):
    venue = Venue()
    with Ledger(tmp_path / "futures.db") as ledger:
        session, store, recovered = make_session(ledger, venue, tmp_path)
        assert session.start().armed
        assert session.state.state == ARMED
        assert store.accounts["agent"].cash == 240
        assert ledger.load_accounts()["agent"].cash == 240
        assert ledger.get_meta(FUTURES_LEDGER_META_KEY)
        assert recovered == []

        restarted, hydrated, recovered = make_session(ledger, venue, tmp_path)
        assert restarted.start().armed
        assert hydrated.accounts["agent"].cash == 240
        assert recovered == ["settled"]
        assert len(ledger.equity_events()) == 1


@pytest.mark.parametrize(("field", "value", "detail"), [
    ("symbols", [{"symbol": "BTCUSDT", "leverage": 2, "marginType": "CROSSED"}], "1×"),
    ("config", {"canTrade": True, "dualSidePosition": True, "multiAssetsMargin": False}, "one-way"),
    ("config", {"canTrade": True, "dualSidePosition": False, "multiAssetsMargin": True}, "single-asset"),
    ("burn_fees", True, "BNB"),
    ("algos", [{"symbol": "BTCUSDT"}], "open order"),
    ("orders", [{"symbol": "BTCUSDT"}], "open order"),
    ("positions", [{"symbol": "ETHUSDT", "positionAmt": "1", "positionSide": "BOTH",
                    "entryPrice": "100"}], "ETHUSDT-PERP"),
    ("wallet", "8", "reserve"),
])
def test_initialization_refuses_venue_drift(tmp_path, field, value, detail):
    venue = Venue()
    setattr(venue, field, value)
    with Ledger(tmp_path / "futures.db") as ledger:
        session, _, _ = make_session(ledger, venue, tmp_path)
        report = session.start()
        assert not report.armed
        assert detail in report.summary()
        assert session.state.state == HALTED
        assert not ledger.get_meta(FUTURES_LEDGER_META_KEY)
        assert not ledger.load_accounts()


def test_bootstrap_refuses_nonpristine_ledger(tmp_path):
    with Ledger(tmp_path / "futures.db") as ledger:
        ledger.persist_account("agent", AgentAccount())
        session, _, recovered = make_session(ledger, Venue(), tmp_path)
        assert not session.start().armed
        assert "pristine" in session.state.reason
        assert session.state.state == HALTED
        assert recovered == []


@pytest.mark.parametrize(("field", "value"), [("environment", "production"), ("alias", "someone else")])
def test_wrong_environment_or_account_never_recovers_dangling_orders(tmp_path, field, value):
    venue = Venue()
    with Ledger(tmp_path / "futures.db") as ledger:
        session, _, _ = make_session(ledger, venue, tmp_path)
        assert session.start().armed
        kwargs = {"environment": value} if field == "environment" else {}
        if field == "alias":
            venue.alias = value
        other, _, recovered = make_session(ledger, venue, tmp_path, **kwargs)
        assert not other.start().armed
        assert "binding" in other.state.reason
        assert recovered == []


@pytest.mark.parametrize("quantity", [-2, 2])
def test_futures_wallet_reconciles_signed_open_cost_basis(tmp_path, quantity):
    venue = Venue()
    with Ledger(tmp_path / "futures.db") as ledger:
        session, store, _ = make_session(ledger, venue, tmp_path)
        assert session.start().armed
        account = store.accounts["agent"]
        account.positions["BTCUSDT-PERP"] = quantity
        account.cost_basis["BTCUSDT-PERP"] = 100 * quantity
        account.cash -= account.cost_basis["BTCUSDT-PERP"] + 0.5
        venue.wallet = "249.5"
        venue.available = "49.5"
        venue.positions = [{"symbol": "BTCUSDT", "positionAmt": str(quantity),
                            "positionSide": "BOTH", "entryPrice": "100"}]
        assert session.recheck().armed
        assert session.state.state == ARMED

        venue.wallet = "249.49"
        assert not session.recheck().armed
        assert "wallet" in session.state.reason
        assert session.state.state == HALTED
        venue.wallet = "249.5"
        assert session.recheck().armed
        assert session.state.state == ARMED


def test_reconciliation_errors_fail_closed(tmp_path):
    venue = Venue()
    with Ledger(tmp_path / "futures.db") as ledger:
        session, _, _ = make_session(ledger, venue, tmp_path)
        assert session.start().armed
        venue.fail = True
        assert not session.recheck().armed
        assert session.state.is_halted()
        assert not session.recheck().armed
        venue.fail = False
        assert session.recheck().armed
        assert session.state.state == ARMED


def test_failed_startup_retries_bootstrap(tmp_path):
    venue = Venue()
    venue.fail = True
    with Ledger(tmp_path / "futures.db") as ledger:
        session, _, _ = make_session(ledger, venue, tmp_path)
        assert not session.start().armed
        venue.fail = False
        assert session.recheck().armed
        assert session.state.state == ARMED


@pytest.mark.parametrize(("environment", "operator_halt"), [("production", False), ("demo", True)])
def test_recovery_respects_other_halts(tmp_path, environment, operator_halt):
    venue = Venue()
    with Ledger(tmp_path / "futures.db") as ledger:
        session, _, _ = make_session(ledger, venue, tmp_path, environment=environment)
        assert session.start().armed
        venue.fail = True
        assert not session.recheck().armed
        if operator_halt:
            session.state.halt_path.write_text("operator halt")
        else:
            session.state.set(HALTED, "venue refused further orders")
        assert not session.recheck().armed
        venue.fail = False
        assert session.recheck().armed
        assert session.state.state == HALTED
        if not operator_halt:
            assert session.state.reason == "venue refused further orders"


def test_halt_file_keeps_reconciled_session_disarmed(tmp_path):
    venue = Venue()
    (tmp_path / "HALT").write_text("operator halt")
    with Ledger(tmp_path / "futures.db") as ledger:
        session, _, _ = make_session(ledger, venue, tmp_path)
        assert session.start().armed
        assert session.state.is_halted()
        assert "operator halt" in session.state.reason


def funding_row(tx_id, time, amount):
    return {
        "tranId": tx_id,
        "time": time,
        "income": amount,
        "incomeType": "FUNDING_FEE",
        "asset": "USDT",
        "symbol": "BTCUSDT",
    }


def test_authenticated_funding_is_booked_once_across_restarts(tmp_path):
    venue = Venue()
    clock = [1_800_000_000_000]
    with Ledger(tmp_path / "futures.db") as ledger:
        session, store, _ = make_session(ledger, venue, tmp_path, now_ms=lambda: clock[0])
        assert session.start().armed
        clock[0] += 1000
        debit = funding_row(123, clock[0], "-0.125")
        venue.funding = [debit, debit]
        venue.wallet = "249.875"

        assert session.recheck().armed
        assert session.state.state == ARMED
        assert store.accounts["agent"].cash == 239.875
        assert ledger.load_accounts()["agent"].cash == 239.875
        assert len(ledger.equity_events()) == 2

        clock[0] += 1000
        venue.funding.append(funding_row(124, clock[0], "0.25"))
        venue.wallet = "250.125"
        restarted, store, _ = make_session(ledger, venue, tmp_path, now_ms=lambda: clock[0])
        assert restarted.start().armed
        assert store.accounts["agent"].cash == 240.125
        assert [row["amount"] for row in ledger.equity_events()[:2]] == [0.25, -0.125]


def test_incomplete_funding_and_external_transfer_do_not_change_ledger(tmp_path):
    venue = Venue()
    clock = [1_800_000_000_000]
    with Ledger(tmp_path / "futures.db") as ledger:
        session, store, _ = make_session(ledger, venue, tmp_path, now_ms=lambda: clock[0])
        assert session.start().armed
        clock[0] += 1000
        venue.funding = [funding_row(123, clock[0], "-0.125")]
        venue.wallet = "249"
        assert not session.recheck().armed
        assert store.accounts["agent"].cash == 240
        assert len(ledger.equity_events()) == 1

        # The same verified funding entry can still be applied during a clean restart.
        venue.wallet = "249.875"
        restarted, store, _ = make_session(ledger, venue, tmp_path, now_ms=lambda: clock[0])
        assert restarted.start().armed
        assert store.accounts["agent"].cash == 239.875
        assert len(ledger.equity_events()) == 2


@pytest.mark.parametrize(("update", "match"), [
    ({"alias": None}, "identify"),
    ({"alias": ""}, "no account alias"),
    ({"wallet": "NaN"}, "non-finite"),
    ({"available": "200"}, "unavailable collateral"),
    ({"config": {"canTrade": False}}, "does not permit"),
    ({"positions": [{"symbol": "BTCUSDT", "positionSide": "SHORT"}]}, "one net one-way"),
])
def test_invalid_venue_account_payload_prevents_arming(tmp_path, update, match):
    venue = Venue()
    for name, value in update.items():
        setattr(venue, name, value)
    with Ledger(tmp_path / "futures.db") as ledger:
        session, _, _ = make_session(ledger, venue, tmp_path)
        assert not session.start().armed
        assert match in session.status()["drift"][0]
        assert session.status()["halted"]


def test_recheck_requires_bootstrap_and_the_bound_account(tmp_path):
    with Ledger(tmp_path / "futures.db") as ledger:
        session, store, _ = make_session(ledger, Venue(), tmp_path)
        assert session.status()["drift"] == []
        assert not session.recheck().armed
        assert "no verified binding" in session.state.reason
        assert session.start().armed

        store.accounts.clear()
        assert not session.recheck().armed
        assert "agent is missing" in session.state.reason


def test_open_position_entry_and_dangling_order_both_block_arming(tmp_path, monkeypatch):
    venue = Venue()
    with Ledger(tmp_path / "futures.db") as ledger:
        session, store, _ = make_session(ledger, venue, tmp_path)
        assert session.start().armed
        account = store.accounts["agent"]
        account.positions["BTCUSDT-PERP"] = -1
        account.cost_basis["BTCUSDT-PERP"] = -100
        account.cash = 340
        venue.positions = [{"symbol": "BTCUSDT", "positionSide": "BOTH",
                            "positionAmt": "-1", "entryPrice": "99"}]
        ledger.save_order(client_order_id="uncertain", agent_id="agent", account="futures",
                          symbol="BTCUSDT-PERP", side="buy", state="UNKNOWN")
        monkeypatch.setattr(store, "_ledger_degraded", True)
        report = session.recheck()
        assert {issue.kind for issue in report.blocking} == {
            "entry_drift", "unresolved_orders", "ledger_degraded",
        }


def test_venue_identity_is_checked_again_after_order_recovery(tmp_path):
    venue = Venue()
    with Ledger(tmp_path / "futures.db") as ledger:
        session, _, _ = make_session(ledger, venue, tmp_path)
        assert session.start().armed
        session._executor.settle_dangling = lambda: setattr(venue, "alias", "changed")
        assert not session.start().armed
        assert "binding changed" in session.state.reason


def test_unhealthy_history_is_not_used_to_adopt_funding(tmp_path):
    venue = Venue()
    clock = [1_800_000_000_000]
    with Ledger(tmp_path / "futures.db") as ledger:
        session, store, _ = make_session(ledger, venue, tmp_path, now_ms=lambda: clock[0])
        assert session.start().armed
        clock[0] += 86_400_000
        assert session.recheck().armed
        assert ledger.get_meta(FUTURES_FUNDING_CHECKPOINT_META_KEY) == str(clock[0])
        assert ledger.get_meta(FUTURES_FUNDING_START_META_KEY) == str(clock[0] - 86_400_000)

        venue.wallet = "250.25"
        venue.funding = [funding_row(123, clock[0], "0.25")] * 1000
        assert not session.recheck().armed
        assert "truncated" in session.state.reason

        venue.funding = [funding_row(123, clock[0], "0.25"),
                         funding_row(123, clock[0], "0.20")]
        assert not session.recheck().armed
        assert "conflicting" in session.state.reason

        venue.funding = [funding_row(0, clock[0], "0.25")]
        assert not session.recheck().armed
        assert "unverifiable" in session.state.reason

        clock[0] += 90 * 86_400_000
        assert not session.recheck().armed
        assert "full funding history" in session.state.reason

        assert store.accounts["agent"].cash == 240
        assert len(ledger.equity_events()) == 1


def test_wallet_moves_during_funding_scan(tmp_path):
    venue = Venue()
    clock = [1_800_000_000_000]
    with Ledger(tmp_path / "futures.db") as ledger:
        session, store, _ = make_session(ledger, venue, tmp_path, now_ms=lambda: clock[0])
        assert session.start().armed
        clock[0] += 1000
        venue.wallet = "250.25"

        def funding_income(*_):
            venue.wallet = "250.26"
            return [funding_row(123, clock[0], "0.25")]

        venue.funding_income = funding_income
        assert not session.recheck().armed
        assert store.accounts["agent"].cash == 240
        assert len(ledger.equity_events()) == 1


@pytest.mark.parametrize("startup_fails", [False, True])
def test_refresher_retries_failed_reconciliation_in_five_seconds(
    tmp_path, monkeypatch, startup_fails,
):
    venue = Venue()
    venue.fail = startup_fails
    failures = [True, False] if startup_fails else [True, True, False]
    expected_delays = [5, 5, 60] if startup_fails else [60, 5, 5, 60]
    delays = []
    with Ledger(tmp_path / "futures.db") as ledger:
        session, _, _ = make_session(ledger, venue, tmp_path, refresh_seconds=60)
        assert session.start().armed is not startup_fails

        def wait(delay):
            delays.append(delay)
            if not failures:
                return True
            venue.fail = failures.pop(0)
            return False

        monkeypatch.setattr(session._stop, "wait", wait)
        session._refresh_loop()
        assert delays == expected_delays
        assert session.state.state == ARMED


def test_refresher_lives_through_failure_and_stops(tmp_path, monkeypatch):
    finished = Event()
    retries = []

    def recheck():
        retries.append("check")
        if len(retries) == 1:
            raise RuntimeError("refresher failure")
        finished.set()

    with Ledger(tmp_path / "futures.db") as ledger:
        session, _, _ = make_session(ledger, Venue(), tmp_path)
        monkeypatch.setattr(session, "_start_refresher", True)
        monkeypatch.setattr(session, "_refresh_seconds", 0.001)
        monkeypatch.setattr(session, "recheck", recheck)
        assert session.start().armed
        try:
            assert finished.wait(timeout=2)
        finally:
            session.stop()
        assert not session._refresher.is_alive()


def test_reconciliation_rejects_no_configured_symbols(tmp_path):
    with Ledger(tmp_path / "futures.db") as ledger:
        with pytest.raises(ValueError, match="at least one allowed symbol"):
            FuturesLiveSession(Venue(), AccountStore(PriceBook()), ledger, None, {}, "agent",
                               environment="demo")


def test_bound_ledger_with_missing_funding_checkpoint_stays_halted(tmp_path, monkeypatch):
    with Ledger(tmp_path / "futures.db") as ledger:
        session, _, _ = make_session(ledger, Venue(), tmp_path)
        assert session.start().armed
        read_meta = ledger.get_meta
        monkeypatch.setattr(ledger, "get_meta", lambda key: (
            None if key == FUTURES_FUNDING_START_META_KEY else read_meta(key)
        ))
        assert not session.recheck().armed
        assert "funding reconciliation checkpoint" in session.state.reason


def test_duplicate_funding_id_rolls_back_cash_and_checkpoint_atomically(tmp_path):
    with Ledger(tmp_path / "futures.db") as ledger:
        start = 1_800_000_000_000
        initial = AgentAccount(cash=240)
        ledger.initialize_futures("agent", initial, "futures-wallet", start)
        funding = FuturesFunding(123, start + 1000, -0.125)

        first = replace(initial, cash=239.875)
        ledger.settle_futures_funding("agent", first, [funding], 240, start + 1000)
        another = FuturesFunding(124, start + 2000, 0.25)
        mistaken = replace(first, cash=240)
        with pytest.raises(sqlite3.IntegrityError):
            ledger.settle_futures_funding(
                "agent", mistaken, [another, funding], first.cash, start + 2000,
            )

        assert ledger.load_accounts()["agent"].cash == first.cash
        assert len(ledger.equity_events()) == 2
        assert ledger.get_meta(another.meta_key) is None
        assert ledger.get_meta(FUTURES_FUNDING_CHECKPOINT_META_KEY) == str(start + 1000)


@pytest.mark.parametrize("reason", ["order needs reconciliation", "venue refused further orders"])
def test_demo_rearms_after_external_halt_when_reconciled(tmp_path, reason):
    venue = Venue()
    with Ledger(tmp_path / "futures.db") as ledger:
        session, _, _ = make_session(ledger, venue, tmp_path)
        assert session.start().armed
        session.state.set(HALTED, reason)
        venue.fail = True
        assert not session.recheck().armed
        assert session.state.is_halted()

        venue.fail = False
        assert session.recheck().armed
        assert session.state.state == ARMED


def test_demo_stays_disarmed_when_booking_requires_reload(tmp_path):
    with Ledger(tmp_path / "futures.db") as ledger:
        session, _, _ = make_session(ledger, Venue(), tmp_path)
        assert session.start().armed
        session.state.set(HALTED, "failed to book order")
        session._executor.restart_required = True

        assert not session.recheck().armed
        assert session.state.is_halted()
