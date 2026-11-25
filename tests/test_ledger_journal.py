"""Ledger schema v2: the order journal, idempotent venue fills, equity events.

The journal is the reason a crash cannot double-buy: an intent is written before the
venue is contacted, and ``settle_fill`` books it at most once. These tests pin that
contract without touching the network or the repo-level ``data/arena.db``.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from arena.ledger import SCHEMA_VERSION, Ledger, LedgerSchemaError
from arena.models import AgentAccount

# The exact shape a pre-journal ledger ships with, so the migration is tested against
# the real thing rather than against the new schema pretending to be old.
V1_SCHEMA = """
CREATE TABLE accounts (
    agent_id TEXT PRIMARY KEY,
    cash REAL NOT NULL,
    trade_count INTEGER NOT NULL,
    total_fees_paid REAL NOT NULL,
    realized_pnl REAL NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE positions (
    agent_id TEXT NOT NULL,
    product_id TEXT NOT NULL,
    quantity REAL NOT NULL,
    cost_basis REAL NOT NULL,
    avg_entry_ts REAL,
    PRIMARY KEY (agent_id, product_id)
);
CREATE TABLE fills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    action TEXT NOT NULL,
    product_id TEXT NOT NULL,
    quantity REAL NOT NULL,
    price REAL NOT NULL,
    fee REAL NOT NULL,
    cash_after REAL NOT NULL,
    latency REAL
);
CREATE TABLE meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE INDEX fills_agent_ts ON fills(agent_id, ts);
"""


def _write_v1_db(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(V1_SCHEMA)
        conn.execute(
            "INSERT INTO accounts VALUES ('agent-a', 48.30, 2, 0.15, -1.25, '2026-11-25T09:00:00')"
        )
        conn.execute(
            "INSERT INTO positions VALUES ('agent-a', 'SOLUSDT', 0.7, 70.5, 1764000000.0)"
        )
        conn.execute(
            "INSERT INTO fills (ts, agent_id, action, product_id, quantity, price, fee,"
            " cash_after, latency) VALUES"
            " ('2026-11-25T09:01:00', 'agent-a', 'buy', 'SOLUSDT', 0.7, 100.0, 0.15, 48.3, 1.2)"
        )
        conn.execute("INSERT INTO meta VALUES ('schema_version', '1')")
        conn.commit()
    finally:
        conn.close()


def _account(cash: float = 900.0, qty: float = 0.1) -> AgentAccount:
    acct = AgentAccount(cash=cash, trade_count=1, total_fees_paid=0.1)
    acct.positions["SOLUSDT"] = qty
    acct.cost_basis["SOLUSDT"] = 100.1
    acct.avg_entry_ts["SOLUSDT"] = 1764000000.0
    return acct


def _intent(ledger: Ledger, client_order_id: str = "arena-a-1", state: str = "PENDING") -> None:
    ledger.save_order(
        client_order_id=client_order_id,
        agent_id="agent-a",
        account="main",
        symbol="SOLUSDT",
        side="buy",
        state=state,
        quote_qty_requested=10.0,
    )


# ── Migration ────────────────────────────────────────────────────


def test_v1_database_migrates_in_place_and_keeps_its_history(tmp_path):
    path = tmp_path / "arena.db"
    _write_v1_db(path)

    with Ledger(path) as ledger:
        assert ledger.get_meta("schema_version") == str(SCHEMA_VERSION)
        accounts = ledger.load_accounts()
        assert accounts["agent-a"].cash == pytest.approx(48.30)
        assert accounts["agent-a"].positions["SOLUSDT"] == pytest.approx(0.7)
        assert len(ledger.recent_fills()) == 1
        # The new columns exist and old rows are paper fills (no order id).
        assert ledger.recent_fills()[0].quantity == pytest.approx(0.7)
        assert ledger.order_counts() == {}
        assert ledger.equity_events() == []
        ledger.save_order(
            client_order_id="arena-a-1",
            agent_id="agent-a",
            account="main",
            symbol="SOLUSDT",
            side="buy",
            state="PENDING",
        )
        assert ledger.get_order("arena-a-1") is not None


def test_migrated_fills_get_null_order_ids(tmp_path):
    path = tmp_path / "arena.db"
    _write_v1_db(path)
    Ledger(path).close()  # opening as the writer migrates it

    conn = sqlite3.connect(str(path))
    try:
        row = conn.execute("SELECT client_order_id, fee_asset FROM fills").fetchone()
    finally:
        conn.close()
    assert row == (None, None)


def test_indexes_exist_after_migration(tmp_path):
    path = tmp_path / "arena.db"
    _write_v1_db(path)
    with Ledger(path):
        pass

    conn = sqlite3.connect(str(path))
    try:
        names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    finally:
        conn.close()
    assert "fills_client_order_id" in names
    assert "orders_state" in names


def test_reader_on_an_unmigrated_database_asks_for_a_migration(tmp_path):
    path = tmp_path / "arena.db"
    _write_v1_db(path)

    with pytest.raises(LedgerSchemaError, match="Start the engine once"):
        Ledger(path, read_only=True)
    # No lock leaked, and the file is untouched: a reader must never migrate.
    with Ledger(path) as ledger:
        assert ledger.get_meta("schema_version") == str(SCHEMA_VERSION)


def test_a_database_from_a_newer_release_is_refused(tmp_path):
    path = tmp_path / "arena.db"
    with Ledger(path):
        pass
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("UPDATE meta SET value = '99' WHERE key = 'schema_version'")
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(LedgerSchemaError, match="newer schema version"):
        Ledger(path)


# ── Journal ──────────────────────────────────────────────────────


def test_an_intent_is_written_before_the_venue_answers(tmp_path):
    with Ledger(tmp_path / "arena.db") as ledger:
        _intent(ledger)
        order = ledger.get_order("arena-a-1")

    assert order.state == "PENDING"
    assert order.quote_qty_requested == pytest.approx(10.0)
    assert order.venue_order_id is None


def test_a_duplicate_intent_id_is_never_overwritten(tmp_path):
    with Ledger(tmp_path / "arena.db") as ledger:
        _intent(ledger)
        with pytest.raises(sqlite3.IntegrityError):
            _intent(ledger)


@pytest.mark.parametrize("state", ["filled", "SUBMITTED_TYPO", ""])
def test_unknown_states_are_rejected(tmp_path, state):
    with Ledger(tmp_path / "arena.db") as ledger:
        with pytest.raises(ValueError, match="unknown order state"):
            _intent(ledger, state=state)


def test_updating_an_order_only_touches_the_fields_given(tmp_path):
    with Ledger(tmp_path / "arena.db") as ledger:
        _intent(ledger)
        ledger.update_order("arena-a-1", state="SUBMITTED", venue_order_id="4242")
        order = ledger.get_order("arena-a-1")

    assert order.state == "SUBMITTED"
    assert order.venue_order_id == "4242"
    assert order.quote_qty_requested == pytest.approx(10.0)  # untouched


def test_dangling_orders_are_the_unresolved_ones_oldest_first(tmp_path):
    with Ledger(tmp_path / "arena.db") as ledger:
        ledger.save_order(
            client_order_id="arena-a-2", agent_id="agent-a", account="main",
            symbol="SOLUSDT", side="buy", state="UNKNOWN", timestamp="2026-11-25T10:00:02",
        )
        ledger.save_order(
            client_order_id="arena-a-1", agent_id="agent-a", account="main",
            symbol="SOLUSDT", side="buy", state="PENDING", timestamp="2026-11-25T10:00:01",
        )
        _intent(ledger, "arena-a-3", state="FILLED")

        assert [o.client_order_id for o in ledger.dangling_orders()] == [
            "arena-a-1",
            "arena-a-2",
        ]


# ── Idempotent settlement ───────────────────────────────────────


def test_settle_fill_books_the_account_and_closes_the_row(tmp_path):
    with Ledger(tmp_path / "arena.db") as ledger:
        _intent(ledger)
        booked = ledger.settle_fill(
            client_order_id="arena-a-1",
            agent_id="agent-a",
            account=_account(),
            timestamp="2026-11-25T10:00:03",
            action="buy",
            product_id="SOLUSDT",
            quantity=0.0997,
            price=100.3,
            fee=0.1,
            fee_asset="SOL",
            cash_after=900.0,
            venue_order_id="4242",
            executed_qty=0.0997,
            cum_notional=10.0,
        )
        order = ledger.get_order("arena-a-1")
        accounts = ledger.load_accounts()

    assert booked is True
    assert order.state == "FILLED"
    assert order.executed_qty == pytest.approx(0.0997)
    assert order.cum_notional == pytest.approx(10.0)
    assert order.fee_asset == "SOL"
    assert accounts["agent-a"].positions["SOLUSDT"] == pytest.approx(0.1)


def test_settling_the_same_fill_twice_books_it_once(tmp_path):
    with Ledger(tmp_path / "arena.db") as ledger:
        _intent(ledger)
        first = ledger.settle_fill(
            client_order_id="arena-a-1",
            agent_id="agent-a",
            account=_account(cash=900.0),
            timestamp="2026-11-25T10:00:03",
            action="buy",
            product_id="SOLUSDT",
            quantity=0.1,
            price=100.0,
            fee=0.1,
            cash_after=900.0,
        )
        # A replay after a crash would carry the *same* post-fill account snapshot.
        second = ledger.settle_fill(
            client_order_id="arena-a-1",
            agent_id="agent-a",
            account=_account(cash=800.0, qty=0.2),
            timestamp="2026-11-25T10:00:04",
            action="buy",
            product_id="SOLUSDT",
            quantity=0.1,
            price=100.0,
            fee=0.1,
            cash_after=800.0,
        )
        accounts = ledger.load_accounts()
        fills = ledger.recent_fills()

    assert first is True
    assert second is False
    assert len(fills) == 1
    # The replay must not re-apply the account snapshot it carried.
    assert accounts["agent-a"].cash == pytest.approx(900.0)
    assert accounts["agent-a"].positions["SOLUSDT"] == pytest.approx(0.1)


def test_two_different_orders_both_book(tmp_path):
    with Ledger(tmp_path / "arena.db") as ledger:
        _intent(ledger, "arena-a-1")
        _intent(ledger, "arena-a-2")
        for client_order_id, cash in (("arena-a-1", 900.0), ("arena-a-2", 800.0)):
            assert ledger.settle_fill(
                client_order_id=client_order_id,
                agent_id="agent-a",
                account=_account(cash=cash),
                timestamp="2026-11-25T10:00:03",
                action="buy",
                product_id="SOLUSDT",
                quantity=0.1,
                price=100.0,
                fee=0.1,
                cash_after=cash,
            )
        assert len(ledger.recent_fills()) == 2


# ── Equity events & meta ────────────────────────────────────────


def test_equity_events_are_recorded_newest_first(tmp_path):
    with Ledger(tmp_path / "arena.db") as ledger:
        ledger.record_equity_event(
            kind="deposit", amount=50.0, equity_before=95.0, equity_after=145.0,
            agent_id="agent-a", note="manual top-up",
        )
        ledger.record_equity_event(
            kind="write_down", amount=-50.0, equity_before=145.0, equity_after=95.0,
            agent_id="agent-a",
        )
        events = ledger.equity_events()

    assert [e["kind"] for e in events] == ["write_down", "deposit"]
    assert events[1]["amount"] == pytest.approx(50.0)
    assert events[1]["agent_id"] == "agent-a"


def test_meta_roundtrip(tmp_path):
    with Ledger(tmp_path / "arena.db") as ledger:
        assert ledger.get_meta("live_state") is None
        assert ledger.get_meta("live_state", "paper") == "paper"
        ledger.set_meta("live_state", "ARMED")
        assert ledger.get_meta("live_state") == "ARMED"
        ledger.set_meta("live_state", "HALTED")
        assert ledger.get_meta("live_state") == "HALTED"
