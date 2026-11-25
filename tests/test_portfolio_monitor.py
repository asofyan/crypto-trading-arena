"""Unit tests for deploy/portfolio_monitor.py — CSV replay into arena state."""

from __future__ import annotations

import csv
import time
from pathlib import Path

from arena.fees import FeeModel
from arena.models import INITIAL_CASH
from deploy.portfolio_monitor import MONITOR_GROUP_ID, PortfolioMirror, latest_trades_file

TRADE_FIELDS = [
    "timestamp",
    "agent_id",
    "action",
    "product_id",
    "quantity",
    "price",
    "total_value",
    "fee",
    "cash_after",
    "latency",
]


def _write_trades(path: Path, rows: list[dict], *, header: bool = True) -> Path:
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=TRADE_FIELDS)
        if header:
            writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in TRADE_FIELDS})
    return path


FEE_BPS = 10  # matches the fee_model_10bps fixture


def _buy(agent="a", product="SOLUSDT", qty=0.5, price=70.0, fee=None, cash=None):
    """One recorded buy row, sized for the $100 wallet the arena starts every agent on.

    ``fee``/``cash`` default to what the arena itself would have written (10 bps on
    notional, subtracted from INITIAL_CASH), so a replayed row matches the CSV and
    the mirror never has to fall back to the recorded cash.
    """
    notional = qty * price
    if fee is None:
        fee = notional * FEE_BPS / 10_000
    if cash is None:
        cash = INITIAL_CASH - notional - fee
    return {
        "timestamp": "2026-01-01T00:00:00",
        "agent_id": agent,
        "action": "buy",
        "product_id": product,
        "quantity": qty,
        "price": price,
        "total_value": notional,
        "fee": fee,
        "cash_after": cash,
        "latency": 1.0,
    }


def test_latest_trades_file_picks_newest(tmp_path):
    old = _write_trades(tmp_path / "trades_20260101_000000.csv", [])
    time.sleep(0.01)
    new = _write_trades(tmp_path / "trades_20260101_010000.csv", [])
    (tmp_path / "snapshots_20260101_000000.csv").write_text("x\n")
    assert latest_trades_file(tmp_path) == new
    assert latest_trades_file(tmp_path / "empty") is None
    assert old.exists()


def test_replay_rebuilds_positions_and_cash(tmp_path, fee_model_10bps):
    path = _write_trades(
        tmp_path / "trades_20260101_000000.csv",
        [_buy(qty=0.5, price=70.0, fee=0.035, cash=INITIAL_CASH - 35.035)],
    )
    mirror = PortfolioMirror(tmp_path, fee_model_10bps)
    assert mirror.sync() is True

    account = mirror.store.get_or_create("a")
    assert account.positions["SOLUSDT"] == 0.5
    assert abs(account.cash - (INITIAL_CASH - 35.035)) < 1e-6
    assert mirror.replayed_contracts == 1
    assert mirror.session_file == path


def test_replay_is_incremental(tmp_path, fee_model_10bps):
    first = _buy(price=70.0)  # 0.5 @ 70 → $35.035 out of the wallet
    path = _write_trades(tmp_path / "trades_20260101_000000.csv", [first])
    mirror = PortfolioMirror(tmp_path, fee_model_10bps)
    mirror.sync()
    assert mirror.replayed_contracts == 1

    # Recorder appends a second fill; only the new row may be replayed.
    second = _buy(price=71.0)  # 0.5 @ 71 → $35.5355 more
    second["cash_after"] = INITIAL_CASH - 35.035 - 35.5355
    with path.open("a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=TRADE_FIELDS)
        writer.writerow(second)

    assert mirror.sync() is True
    assert mirror.replayed_contracts == 2
    assert mirror.store.get_or_create("a").positions["SOLUSDT"] == 1.0


def test_partial_last_row_is_skipped(tmp_path, fee_model_10bps):
    path = _write_trades(tmp_path / "trades_20260101_000000.csv", [])
    with path.open("a") as f:
        f.write(",".join(TRADE_FIELDS) + "\n")
        # Half-written row: recorder flushes line-buffered, readers can catch this.
        f.write("2026-01-01T00:00:01,a,buy,SOLUSDT,5.0,70.0,")

    mirror = PortfolioMirror(tmp_path, fee_model_10bps)
    mirror.sync()
    assert mirror.replayed_contracts == 0


def test_newer_session_file_resets_state(tmp_path, fee_model_10bps):
    first = _write_trades(tmp_path / "trades_20260101_000000.csv", [_buy(qty=0.5)])
    mirror = PortfolioMirror(tmp_path, fee_model_10bps)
    mirror.sync()
    assert mirror.store.get_or_create("a").positions["SOLUSDT"] == 0.5

    time.sleep(0.01)
    second = _write_trades(tmp_path / "trades_20260101_020000.csv", [_buy(qty=0.2)])
    assert mirror.sync() is True
    assert mirror.session_file == second
    assert first.exists()
    # Fresh wallet: the new session only knows about its own fill.
    assert mirror.store.get_or_create("a").positions["SOLUSDT"] == 0.2
    assert mirror.replayed_contracts == 1


def test_live_tick_marks_positions(tmp_path, fee_model_10bps):
    _write_trades(
        tmp_path / "trades_20260101_000000.csv",
        [_buy(qty=0.5, price=70.0, fee=0.035, cash=INITIAL_CASH - 35.035)],
    )
    mirror = PortfolioMirror(tmp_path, fee_model_10bps)
    mirror.sync()

    class Ticker:
        def model_dump(self):
            return {
                "product_id": "SOLUSDT",
                "price": "80.0",
                "best_bid": "79.99",
                "best_ask": "80.01",
            }

    mirror.update_price(Ticker())
    account = mirror.store.get_or_create("a")
    expected = INITIAL_CASH - 35.035 + 0.5 * 80.0
    assert abs(account.portfolio_value(mirror.store.price_book) - expected) < 1e-6


def test_rejected_and_malformed_rows_are_skipped(tmp_path, fee_model_10bps):
    """A fill the arena would have refused, or a junk row, must not break replay."""
    path = _write_trades(tmp_path / "trades_20260101_000000.csv", [_buy()])
    mirror = PortfolioMirror(tmp_path, fee_model_10bps)
    mirror.sync()
    assert mirror.replayed_contracts == 1

    with path.open("a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=TRADE_FIELDS)
        writer.writerow(_buy(qty=0.0))            # arena rejects non-positive qty
        writer.writerow({**dict.fromkeys(TRADE_FIELDS, ""), "agent_id": "x"})

    assert mirror.sync() is False
    assert mirror.replayed_contracts == 1


def test_monitor_group_differs_from_arena_group():
    """Fan-out depends on this: same group as the arena would split partitions."""
    assert MONITOR_GROUP_ID != "tools-dashboard"


def test_fee_model_is_injected_not_recomputed(tmp_path):
    """Zero-fee replay must match a zero-fee CSV, proving the model is honoured."""
    _write_trades(
        tmp_path / "trades_20260101_000000.csv",
        [_buy(qty=0.5, price=70.0, fee=0.0, cash=INITIAL_CASH - 35.0)],
    )
    mirror = PortfolioMirror(tmp_path, FeeModel(taker_bps=0))
    mirror.sync()
    assert abs(mirror.store.get_or_create("a").cash - (INITIAL_CASH - 35.0)) < 1e-6


def test_replay_trusts_recorded_cash_when_the_fee_rate_disagrees(tmp_path, caplog):
    """The CSV is written by the arena, so it wins over a misconfigured fee rate."""
    _write_trades(
        tmp_path / "trades_20260101_000000.csv",
        [_buy(qty=0.5, price=70.0, fee=0.0, cash=INITIAL_CASH - 35.0)],  # written fee-free
    )
    mirror = PortfolioMirror(tmp_path, FeeModel(taker_bps=10))  # replay charges 10 bps
    assert mirror.sync() is True

    account = mirror.store.get_or_create("a")
    assert account.positions["SOLUSDT"] == 0.5
    assert abs(account.cash - (INITIAL_CASH - 35.0)) < 1e-6  # recorded cash, not 34.965
    assert "taker_bps matches the running arena" in caplog.text
