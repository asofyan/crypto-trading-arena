"""Unit tests for arena.ledger.Ledger — the SQLite wallet ledger.

Every test runs against a throwaway database under ``tmp_path`` so the repo-level
``data/arena.db`` is never touched.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from arena.ledger import (
    DEFAULT_DB_PATH,
    RECENT_FILLS_LIMIT,
    SCHEMA_VERSION,
    Ledger,
    LedgerInUseError,
    LedgerSchemaError,
)
from arena.models import INITIAL_CASH, AgentAccount


def _account(
    cash: float = 49_990.0,
    *,
    trade_count: int = 1,
    total_fees_paid: float = 5.0,
    realized_pnl: float = 0.0,
) -> AgentAccount:
    """An account holding 1 BTC with every persisted field populated."""
    acct = AgentAccount(
        cash=cash,
        trade_count=trade_count,
        total_fees_paid=total_fees_paid,
        realized_pnl=realized_pnl,
    )
    acct.positions["BTC-USD"] = 1.0
    acct.cost_basis["BTC-USD"] = 50_010.0
    acct.avg_entry_ts["BTC-USD"] = 1_700_000_000.0
    return acct


def _fill(
    ledger: Ledger,
    account: AgentAccount,
    *,
    agent_id: str = "a",
    timestamp: str = "2024-01-01T12:00:00",
    action: str = "buy",
    product_id: str = "BTC-USD",
    quantity: float = 1.0,
    price: float = 50_010.0,
    fee: float = 5.0,
    latency: float | None = None,
) -> None:
    ledger.save_fill(
        agent_id=agent_id,
        account=account,
        timestamp=timestamp,
        action=action,
        product_id=product_id,
        quantity=quantity,
        price=price,
        fee=fee,
        cash_after=account.cash,
        latency=latency,
    )


# ── Module contract ──────────────────────────────────────────────


def test_module_constants():
    assert DEFAULT_DB_PATH == Path("data/arena.db")
    assert SCHEMA_VERSION == 1
    assert RECENT_FILLS_LIMIT == 50
    assert issubclass(LedgerSchemaError, RuntimeError)


# ── Account roundtrip ────────────────────────────────────────────


def test_save_fill_roundtrips_account_state(tmp_path):
    with Ledger(tmp_path / "arena.db") as ledger:
        acct = _account(realized_pnl=-12.5)
        _fill(ledger, acct)

        loaded = ledger.load_accounts()

    assert set(loaded) == {"a"}
    got = loaded["a"]
    assert isinstance(got, AgentAccount)
    assert got is not acct  # a fresh instance, not the object handed to save_fill
    assert got.cash == pytest.approx(49_990.0)
    assert got.positions == pytest.approx({"BTC-USD": 1.0})
    assert got.cost_basis == pytest.approx({"BTC-USD": 50_010.0})
    assert got.avg_entry_ts == pytest.approx({"BTC-USD": 1_700_000_000.0})
    assert got.trade_count == 1
    assert got.total_fees_paid == pytest.approx(5.0)
    assert got.realized_pnl == pytest.approx(-12.5)


def test_state_survives_close_and_reopen(tmp_path):
    path = tmp_path / "arena.db"
    with Ledger(path) as ledger:
        _fill(ledger, _account())

    with Ledger(path) as reopened:
        loaded = reopened.load_accounts()

    assert loaded["a"].cash == pytest.approx(49_990.0)
    assert loaded["a"].positions == pytest.approx({"BTC-USD": 1.0})


def test_avg_entry_ts_none_roundtrips(tmp_path):
    """A position without a weighted-average entry timestamp must not blow up."""
    with Ledger(tmp_path / "arena.db") as ledger:
        acct = AgentAccount(cash=1_000.0)
        acct.positions["SOL-USD"] = 2.0
        acct.cost_basis["SOL-USD"] = 200.0
        # avg_entry_ts deliberately left empty → NULL in SQLite.
        _fill(ledger, acct, product_id="SOL-USD", quantity=2.0, price=100.0)

        loaded = ledger.load_accounts()

    assert loaded["a"].positions == pytest.approx({"SOL-USD": 2.0})
    assert loaded["a"].cost_basis == pytest.approx({"SOL-USD": 200.0})
    assert loaded["a"].avg_entry_ts == {}


def test_position_upsert_updates_quantity_and_cost_basis(tmp_path):
    with Ledger(tmp_path / "arena.db") as ledger:
        acct = _account()
        _fill(ledger, acct)  # 1 BTC

        acct.positions["BTC-USD"] = 0.4
        acct.cost_basis["BTC-USD"] = 20_004.0
        _fill(ledger, acct, timestamp="2024-01-01T12:05:00", action="sell", quantity=0.6)

        loaded = ledger.load_accounts()["a"]

    assert loaded.positions == pytest.approx({"BTC-USD": 0.4})
    assert loaded.cost_basis == pytest.approx({"BTC-USD": 20_004.0})


def test_full_sell_removes_stale_position_row(tmp_path):
    """A closed position must disappear from the table, not linger with qty 0."""
    path = tmp_path / "arena.db"
    with Ledger(path) as ledger:
        acct = _account()
        _fill(ledger, acct)
        # Mirror what a full sell does to the in-memory account.
        acct.positions.clear()
        acct.cost_basis.clear()
        acct.avg_entry_ts.pop("BTC-USD", None)
        acct.cash = 99_990.0
        _fill(ledger, acct, timestamp="2024-01-01T12:05:00", action="sell")

        loaded = ledger.load_accounts()["a"]

    assert loaded.positions == {}
    assert loaded.cost_basis == {}
    assert loaded.avg_entry_ts == {}

    raw = sqlite3.connect(path)
    try:
        stale = raw.execute("SELECT COUNT(*) FROM positions WHERE agent_id = 'a'").fetchone()[0]
        fills = raw.execute("SELECT COUNT(*) FROM fills").fetchone()[0]
    finally:
        raw.close()
    assert stale == 0
    assert fills == 2


# ── Fill log ─────────────────────────────────────────────────────


def test_recent_fills_chronological_with_hh_mm_ss(tmp_path):
    with Ledger(tmp_path / "arena.db") as ledger:
        acct = _account()
        _fill(ledger, acct, timestamp="2024-01-01T09:05:07", action="buy", latency=1.23)
        _fill(ledger, acct, timestamp="2024-01-01T10:06:08", action="sell", price=49_990.0)

        fills = ledger.recent_fills()

    assert [f.action for f in fills] == ["buy", "sell"]  # oldest → newest
    assert [f.timestamp for f in fills] == ["09:05:07", "10:06:08"]  # chart-friendly
    first = fills[0]
    assert first.agent_id == "a"
    assert first.product_id == "BTC-USD"
    assert first.quantity == pytest.approx(1.0)
    assert first.price == pytest.approx(50_010.0)
    assert first.fee == pytest.approx(5.0)
    assert first.latency == pytest.approx(1.23)
    assert fills[1].latency is None


def test_recent_fills_limit_keeps_newest_in_order(tmp_path):
    with Ledger(tmp_path / "arena.db") as ledger:
        acct = _account()
        for hour in range(5):
            _fill(ledger, acct, timestamp=f"2024-01-01T0{hour}:00:00")

        fills = ledger.recent_fills(limit=2)

    assert [f.timestamp for f in fills] == ["03:00:00", "04:00:00"]


def test_recent_fills_defaults_to_recent_fills_limit(tmp_path):
    with Ledger(tmp_path / "arena.db") as ledger:
        acct = _account()
        for i in range(RECENT_FILLS_LIMIT + 5):
            _fill(ledger, acct, timestamp=f"2024-01-01T00:{i:02d}:00")

        fills = ledger.recent_fills()

    assert len(fills) == RECENT_FILLS_LIMIT
    assert fills[-1].timestamp == f"00:{RECENT_FILLS_LIMIT + 4:02d}:00"


def test_recent_fills_falls_back_to_raw_timestamp(tmp_path):
    """A non-ISO timestamp (e.g. migrated data) is surfaced verbatim, not dropped."""
    path = tmp_path / "arena.db"
    with Ledger(path) as ledger:
        _fill(ledger, _account(), timestamp="not-a-timestamp")

        fills = ledger.recent_fills()

    assert [f.timestamp for f in fills] == ["not-a-timestamp"]


# ── Write path robustness ────────────────────────────────────────


class _MutatingAccount(AgentAccount):
    """An account whose sibling mappings change while save_fill reads them.

    This makes the M1 race deterministic: a concurrent buy adds a position between
    two iterations of the position loop, exactly like another thread calling
    get_or_create/execute_trade on the same account.
    """

    def __init__(self) -> None:
        super().__init__(cash=49_990.0, trade_count=1, total_fees_paid=5.0)
        self.positions["BTC-USD"] = 1.0
        positions = self.positions
        mutated = False

        class _CostBasis(dict[str, float]):
            def get(self, key, default=None):
                nonlocal mutated
                if not mutated:
                    mutated = True
                    positions["ETH-USD"] = 5.0
                return super().get(key, default)

        self.cost_basis = _CostBasis({"BTC-USD": 50_010.0})
        self.avg_entry_ts["BTC-USD"] = 1_700_000_000.0


def test_save_fill_snapshots_account_state_before_writing(tmp_path):
    """save_fill must copy positions/cost_basis, never iterate the live mappings."""
    with Ledger(tmp_path / "arena.db") as ledger:
        _fill(ledger, _MutatingAccount())  # without a snapshot this raised RuntimeError

        loaded = ledger.load_accounts()["a"]

    assert loaded.positions == pytest.approx({"BTC-USD": 1.0})
    assert loaded.cost_basis == pytest.approx({"BTC-USD": 50_010.0})


# ── CLI summary ──────────────────────────────────────────────────


def test_summary_reports_per_agent_totals(tmp_path):
    with Ledger(tmp_path / "arena.db") as ledger:
        _fill(ledger, _account(), agent_id="a", timestamp="2024-01-01T11:00:00")
        _fill(
            ledger,
            _account(cash=INITIAL_CASH, trade_count=4, total_fees_paid=9.5),
            agent_id="b",
            timestamp="2024-01-01T11:30:00",
        )

        rows = ledger.summary()

    assert [r["agent_id"] for r in rows] == ["a", "b"]
    first = rows[0]
    assert set(first) == {
        "agent_id",
        "cash",
        "position_count",
        "trade_count",
        "realized_pnl",
        "total_fees_paid",
        "last_fill_ts",
    }
    assert first["cash"] == pytest.approx(49_990.0)
    assert first["position_count"] == 1
    assert first["trade_count"] == 1
    assert first["realized_pnl"] == pytest.approx(0.0)
    assert first["total_fees_paid"] == pytest.approx(5.0)
    assert first["last_fill_ts"] == "2024-01-01T11:00:00"
    assert rows[1]["trade_count"] == 4
    assert rows[1]["position_count"] == 1


def test_summary_last_fill_ts_none_without_fills(tmp_path):
    path = tmp_path / "arena.db"
    with Ledger(path) as ledger:
        raw = sqlite3.connect(path)
        raw.execute(
            "INSERT INTO accounts "
            "(agent_id, cash, trade_count, total_fees_paid, realized_pnl, updated_at) "
            "VALUES ('idle', 1000.0, 0, 0.0, 0.0, '2024-01-01T00:00:00')"
        )
        raw.commit()
        raw.close()

        rows = ledger.summary()

    assert len(rows) == 1
    assert rows[0]["agent_id"] == "idle"
    assert rows[0]["cash"] == pytest.approx(1000.0)
    assert rows[0]["position_count"] == 0
    assert rows[0]["last_fill_ts"] is None


def test_summary_on_empty_database_is_empty(tmp_path):
    with Ledger(tmp_path / "arena.db") as ledger:
        assert ledger.summary() == []
        assert ledger.load_accounts() == {}
        assert ledger.recent_fills() == []


# ── Bootstrapping & schema guardrails ────────────────────────────


def test_parent_directory_is_created(tmp_path):
    path = tmp_path / "nested" / "deeper" / "arena.db"
    with Ledger(path) as ledger:
        _fill(ledger, _account())

    assert path.exists()


def test_schema_version_is_recorded(tmp_path):
    path = tmp_path / "arena.db"
    with Ledger(path):
        pass

    raw = sqlite3.connect(path)
    try:
        value = raw.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0]
    finally:
        raw.close()
    assert int(value) == SCHEMA_VERSION


def test_schema_version_mismatch_raises(tmp_path):
    path = tmp_path / "arena.db"
    with Ledger(path):
        pass

    raw = sqlite3.connect(path)
    raw.execute("UPDATE meta SET value = '999' WHERE key = 'schema_version'")
    raw.commit()
    raw.close()

    with pytest.raises(LedgerSchemaError, match="schema"):
        Ledger(path)


def test_schema_mismatch_does_not_leak_the_writer_lock(tmp_path):
    """A refused open must release the lock, else the real error hides behind LedgerInUseError."""
    path = tmp_path / "arena.db"
    with Ledger(path):
        pass

    raw = sqlite3.connect(path)
    raw.execute("UPDATE meta SET value = '999' WHERE key = 'schema_version'")
    raw.commit()
    raw.close()

    for _ in range(2):  # the second attempt proves the failed first one let go
        with pytest.raises(LedgerSchemaError, match="schema"):
            Ledger(path)


# ── Single-writer guard ──────────────────────────────────────────


def test_second_writer_on_the_same_database_is_rejected(tmp_path):
    path = tmp_path / "arena.db"
    with Ledger(path):
        with pytest.raises(LedgerInUseError) as excinfo:
            Ledger(path)

    message = str(excinfo.value)
    assert str(path.with_name("arena.db.lock")) in message
    assert "already owns" in message


def test_close_releases_the_writer_lock_and_is_idempotent(tmp_path):
    path = tmp_path / "arena.db"
    first = Ledger(path)
    first.close()
    first.close()  # closing twice must not raise

    with Ledger(path) as second:
        _fill(second, _account())

    with Ledger(path) as third:  # reacquired after the previous holder closed
        assert third.load_accounts()["a"].cash == pytest.approx(49_990.0)


def test_lock_file_sits_next_to_the_database_without_breaking_queries(tmp_path):
    path = tmp_path / "arena.db"
    with Ledger(path) as ledger:
        lock_path = path.with_name("arena.db.lock")
        assert lock_path.exists()

        _fill(ledger, _account())

        assert len(ledger.summary()) == 1
        assert len(ledger.recent_fills()) == 1
        assert lock_path.exists()  # advisory lock never shadows the DB for SQLite readers


# ── Read-only mode (status against a live engine) ────────────────


def test_read_only_ledger_reads_while_the_writer_holds_the_lock(tmp_path):
    path = tmp_path / "arena.db"
    with Ledger(path) as writer:
        _fill(writer, _account())

        with Ledger(path, read_only=True) as reader:
            assert reader.load_accounts()["a"].cash == pytest.approx(49_990.0)
            assert reader.summary()[0]["trade_count"] == 1
            assert len(reader.recent_fills()) == 1


def test_read_only_ledger_takes_no_lock(tmp_path):
    path = tmp_path / "arena.db"
    with Ledger(path):
        pass

    with Ledger(path, read_only=True):
        with Ledger(path) as writer:  # a reader must not block the writer
            _fill(writer, _account())


def test_read_only_missing_database_raises_file_not_found(tmp_path):
    with pytest.raises(FileNotFoundError):
        Ledger(tmp_path / "missing.db", read_only=True)


def test_read_only_on_a_database_that_is_not_a_ledger_raises_operational_error(tmp_path):
    """A foreign sqlite file is a sqlite problem, not an "old schema, migrate it" problem."""
    path = tmp_path / "empty.db"
    sqlite3.connect(path).close()  # a file that exists but carries no schema

    with pytest.raises(sqlite3.OperationalError, match="meta"):
        Ledger(path, read_only=True)


def test_non_schema_sqlite_errors_surface_unchanged(tmp_path):
    path = tmp_path / "foreign.db"
    path.write_text("definitely not a sqlite database")

    with pytest.raises(sqlite3.Error) as excinfo:
        Ledger(path, read_only=True)

    assert not isinstance(excinfo.value, LedgerSchemaError)


# ── Failure classification ───────────────────────────────────────


def test_lock_creation_failure_propagates_the_os_error(tmp_path):
    """EACCES/ENOSPC on the lock file is not "another engine owns the ledger"."""
    path = tmp_path / "arena.db"
    # A directory at the lock path makes os.open fail with EISDIR — the same shape as
    # a hostile filesystem answering EACCES, but reproducible without chmod.
    path.with_name(f"{path.name}.lock").mkdir()

    with pytest.raises(OSError) as excinfo:
        Ledger(path)

    assert not isinstance(excinfo.value, LedgerInUseError)


# ── Degraded marker (cross-process visibility) ──────────────────


def test_degraded_marker_roundtrips_through_a_restart(tmp_path):
    path = tmp_path / "arena.db"
    with Ledger(path) as ledger:
        assert ledger.degraded_marker() is None
        ledger.mark_degraded("momentum", "2025-01-01T00:00:00")

    with Ledger(path, read_only=True) as reopened:  # what `manage_bot status` sees
        assert reopened.degraded_marker() == {
            "ts": "2025-01-01T00:00:00",
            "agent": "momentum",
        }


def test_mark_healthy_clears_the_degraded_marker(tmp_path):
    path = tmp_path / "arena.db"
    with Ledger(path) as ledger:
        ledger.mark_degraded("momentum", "2025-01-01T00:00:00")
        ledger.mark_healthy()

    with Ledger(path, read_only=True) as reader:
        assert reader.degraded_marker() is None


def test_degraded_marker_tolerates_a_hand_edited_value(tmp_path):
    path = tmp_path / "arena.db"
    with Ledger(path):
        pass

    raw = sqlite3.connect(path)
    raw.execute("INSERT INTO meta (key, value) VALUES ('ledger_degraded', 'garbage')")
    raw.commit()
    raw.close()

    with Ledger(path, read_only=True) as reader:
        assert reader.degraded_marker() == {"ts": "garbage", "agent": "unknown"}


def test_read_only_reads_a_database_written_by_a_closed_writer(tmp_path):
    path = tmp_path / "arena.db"
    with Ledger(path) as writer:
        _fill(writer, _account())

    with Ledger(path, read_only=True) as reader:
        assert reader.load_accounts()["a"].positions == pytest.approx({"BTC-USD": 1.0})


# ── Indexes ──────────────────────────────────────────────────────


def test_fills_agent_ts_index_is_created(tmp_path):
    path = tmp_path / "arena.db"
    with Ledger(path):
        pass

    raw = sqlite3.connect(path)
    try:
        indexes = {
            row[0]
            for row in raw.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'fills'"
            )
        }
    finally:
        raw.close()

    assert "fills_agent_ts" in indexes


def test_reopening_is_idempotent(tmp_path):
    """Reopening must not wipe rows nor duplicate the meta row."""
    path = tmp_path / "arena.db"
    for _ in range(3):
        with Ledger(path) as ledger:
            _fill(ledger, _account(), timestamp="2024-01-01T12:00:00")

    with Ledger(path) as ledger:
        assert len(ledger.load_accounts()) == 1
        assert len(ledger.recent_fills()) == 3

    raw = sqlite3.connect(path)
    try:
        meta_rows = raw.execute("SELECT COUNT(*) FROM meta").fetchone()[0]
    finally:
        raw.close()
    assert meta_rows == 1
