"""Unit tests for arena.account_store.AccountStore.

These build fresh AccountStore instances (via the `make_store` fixture) rather than
the arena.tools singleton, so account state never leaks between tests. Confirmed bugs
are encoded as strict xfails referencing docs/REVIEW_FINDINGS.md.
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone

import pytest

from arena.account_store import AccountStore
from arena.ledger import Ledger
from arena.models import INITIAL_CASH, AgentAccount, TradeResult
from arena.trade_policy import ChurnLimits, ExitFloor

# BTC costs ~$50k per unit and orders are capped at one decimal place, so no BTC order
# fits the $100 wallet. Trades below use the affordable products (SOL ~$100/unit,
# XRP ~$2/unit) at quantities a real $100 wallet can fund; BTC is kept only where a
# test needs a quote the arena will refuse to fill.
SOL_ASK = 100.10
SOL_BID = 99.90


# ── Happy-path execution & price selection ───────────────────────


def test_buy_executes_at_best_ask(make_store):
    store = make_store(taker_bps=0)
    result = store.execute_trade("a", "SOL-USD", 0.5, "buy")
    assert result.success, result.message

    acct = store.get_or_create("a")
    assert acct.positions["SOL-USD"] == pytest.approx(0.5)
    expected_cost = SOL_ASK * 0.5
    assert acct.cash == pytest.approx(INITIAL_CASH - expected_cost)  # filled at ask, not last/bid
    assert acct.cost_basis["SOL-USD"] == pytest.approx(expected_cost)
    assert acct.trade_count == 1


def test_sell_executes_at_best_bid(make_store):
    store = make_store(taker_bps=0)
    store.execute_trade("a", "SOL-USD", 0.5, "buy")
    cash_after_buy = store.get_or_create("a").cash

    result = store.execute_trade("a", "SOL-USD", 0.5, "sell")
    assert result.success, result.message
    acct = store.get_or_create("a")
    # proceeds at bid, not last/ask
    assert acct.cash == pytest.approx(cash_after_buy + SOL_BID * 0.5)


def test_action_and_product_id_are_normalized(make_store):
    store = make_store(taker_bps=0)
    result = store.execute_trade("a", "  sol-usd ", 0.5, " BUY ")
    assert result.success, result.message
    assert "SOL-USD" in store.get_or_create("a").positions


# ── Validation / reject paths (no state mutation) ────────────────


def test_invalid_action_rejected(make_store):
    store = make_store()
    result = store.execute_trade("a", "BTC-USD", 1.0, "hodl")
    assert not result.success
    assert "Invalid action" in result.message
    assert store.get_or_create("a").trade_count == 0


def test_unknown_product_rejected_lists_available(make_store):
    store = make_store()
    result = store.execute_trade("a", "DOGE-USD", 1.0, "buy")
    assert not result.success
    assert "No live price" in result.message
    assert "BTC-USD" in result.message and "SOL-USD" in result.message  # available list


@pytest.mark.parametrize("qty", [0.0, -1.0, -0.5])
def test_non_positive_quantity_rejected(make_store, qty):
    store = make_store()
    result = store.execute_trade("a", "BTC-USD", qty, "buy")
    assert not result.success
    assert "positive" in result.message.lower()


@pytest.mark.parametrize("qty", [0.12, 1.23, 0.05])
def test_excess_precision_quantity_rejected(make_store, qty):
    store = make_store()
    result = store.execute_trade("a", "BTC-USD", qty, "buy")
    assert not result.success
    assert "decimal place" in result.message


def test_insufficient_cash_leaves_state_unchanged(make_store):
    store = make_store(taker_bps=0)
    acct = store.get_or_create("a")
    acct.cash = 100.0  # one SOL unit costs $100.10 — a cent more than the wallet holds

    result = store.execute_trade("a", "SOL-USD", 1.0, "buy")
    assert not result.success
    assert "Insufficient cash" in result.message
    assert acct.cash == 100.0
    assert acct.positions == {}
    assert acct.cost_basis == {}
    assert acct.trade_count == 0
    assert store.trade_log == []


def test_sell_more_than_held_rejected(make_store):
    store = make_store(taker_bps=0)
    store.execute_trade("a", "SOL-USD", 0.5, "buy")
    result = store.execute_trade("a", "SOL-USD", 1.0, "sell")
    assert not result.success
    assert "Insufficient holdings" in result.message
    assert store.get_or_create("a").positions["SOL-USD"] == pytest.approx(0.5)  # untouched


def test_sell_with_no_position_rejected(make_store):
    store = make_store()
    result = store.execute_trade("a", "SOL-USD", 0.5, "sell")
    assert not result.success
    assert "Insufficient holdings" in result.message


# ── Position lifecycle ───────────────────────────────────────────


def test_full_sell_clears_position_and_all_metadata(make_store):
    store = make_store(taker_bps=0)
    store.execute_trade("a", "SOL-USD", 0.5, "buy")
    store.execute_trade("a", "SOL-USD", 0.5, "sell")

    acct = store.get_or_create("a")
    assert "SOL-USD" not in acct.positions
    assert "SOL-USD" not in acct.cost_basis
    assert "SOL-USD" not in acct.avg_entry_ts


def test_partial_sell_keeps_per_unit_cost_and_all_keys(make_store):
    store = make_store(taker_bps=0)
    store.execute_trade("a", "SOL-USD", 0.5, "buy")
    acct = store.get_or_create("a")
    avg_before = acct.avg_cost_per_unit("SOL-USD")

    store.execute_trade("a", "SOL-USD", 0.2, "sell")

    assert acct.positions["SOL-USD"] == pytest.approx(0.3)
    assert acct.avg_cost_per_unit("SOL-USD") == pytest.approx(avg_before)
    # All three maps remain populated and in sync after a partial close.
    assert acct.cost_basis["SOL-USD"] == pytest.approx(avg_before * 0.3)
    assert "SOL-USD" in acct.avg_entry_ts


def test_account_maps_stay_in_sync_across_a_trade_sequence(make_store):
    """positions / cost_basis / avg_entry_ts must share one key set after every fill (R9)."""
    store = make_store(taker_bps=10)
    acct = store.get_or_create("a")
    sequence = [
        ("SOL-USD", 0.5, "buy"),
        ("XRP-USD", 5.0, "buy"),
        ("SOL-USD", 0.2, "buy"),
        ("SOL-USD", 0.2, "sell"),
        ("XRP-USD", 5.0, "sell"),  # fully closes XRP
        ("SOL-USD", 0.5, "sell"),  # fully closes SOL
    ]
    for product, qty, action in sequence:
        assert store.execute_trade("a", product, qty, action).success
        keys = set(acct.positions)
        assert set(acct.cost_basis) == keys
        assert set(acct.avg_entry_ts) == keys
        assert all(v >= 0 for v in acct.cost_basis.values())
    assert acct.positions == {}  # everything closed


def test_adding_to_a_position_does_not_reset_its_hold_clock(make_store, monkeypatch):
    """An add keeps the clock it had: the limit is the age of the exposure.

    Averaging the timestamps let a dust add renew the hold — the position could then
    neither be closed by the floor's band (an add keeps it inside) nor by its escape
    hatch (the clock never expires). The measured run is exactly that shape: LINK was
    bought three times in ten minutes, and a limit that a 0.01 add can renew is a limit
    that does not exist.
    """
    import arena.account_store as acct_mod

    clock = {"t": 1000.0}

    class _FixedClock:
        @classmethod
        def now(cls):
            return datetime.fromtimestamp(clock["t"], tz=timezone.utc)

    monkeypatch.setattr(acct_mod, "datetime", _FixedClock)

    store = make_store(taker_bps=0)
    assert store.execute_trade("a", "SOL-USD", 0.1, "buy").success
    clock["t"] = 2000.0
    assert store.execute_trade("a", "SOL-USD", 0.1, "buy").success

    assert store.get_or_create("a").avg_entry_ts["SOL-USD"] == pytest.approx(1000.0)


def test_a_flip_starts_a_fresh_clock(make_store, monkeypatch):
    """The other edge: what is held after a flip is a new position, so it starts now."""
    import arena.account_store as acct_mod

    clock = {"t": 1000.0}

    class _FixedClock:
        @classmethod
        def now(cls):
            return datetime.fromtimestamp(clock["t"], tz=timezone.utc)

    monkeypatch.setattr(acct_mod, "datetime", _FixedClock)

    store = make_store(taker_bps=0)
    store.price_book.update(dict(PERP_TICKER))
    assert store.execute_trade("a", PERP, 1.0, "buy").success
    clock["t"] = 2000.0
    assert store.execute_trade("a", PERP, 1.5, "sell").success

    assert store.get_or_create("a").avg_entry_ts[PERP] == pytest.approx(2000.0)


# ── Trade log & recorder forwarding ──────────────────────────────


def test_trade_log_appends_one_entry_per_fill(make_store):
    store = make_store(taker_bps=10)
    store.execute_trade("a", "SOL-USD", 0.5, "buy")
    store.execute_trade("a", "SOL-USD", 0.2, "sell")

    log = store.trade_log
    assert len(log) == 2
    assert log[0].action == "buy" and log[0].product_id == "SOL-USD"
    assert log[0].price == pytest.approx(SOL_ASK)
    assert log[1].action == "sell"
    assert log[1].price == pytest.approx(SOL_BID)
    assert log[0].fee > 0  # 10 bps charged


def test_latency_forwarded_to_log_and_recorder(make_store):
    store = make_store(taker_bps=0)
    captured = {}

    class _Recorder:
        def record_trade(self, **kw):
            captured.update(kw)

    store.attach_recorder(_Recorder())
    store.execute_trade("a", "SOL-USD", 0.5, "buy", latency=1.23)

    assert store.trade_log[0].latency == pytest.approx(1.23)
    assert captured["latency"] == pytest.approx(1.23)
    assert captured["agent_id"] == "a"
    assert captured["action"] == "buy"
    assert captured["cash_after"] == pytest.approx(INITIAL_CASH - SOL_ASK * 0.5)


def test_rejected_trade_is_not_recorded(make_store):
    store = make_store(taker_bps=0)
    calls = []

    class _Recorder:
        def record_trade(self, **kw):
            calls.append(kw)

    store.attach_recorder(_Recorder())
    result = store.execute_trade("a", "SOL-USD", 0.5, "sell")  # nothing held → rejected
    assert not result.success
    assert calls == []
    assert store.trade_log == []


def test_get_or_create_is_idempotent_and_per_agent(make_store):
    store = make_store()
    a1 = store.get_or_create("a")
    a2 = store.get_or_create("a")
    b = store.get_or_create("b")
    assert a1 is a2
    assert a1 is not b


# ── Quantity edge cases (previously findings B2/B5/B6, fixed) ──


def test_subrounding_quantity_rejected(make_store):
    """B2: a quantity that rounds to zero must be rejected, not crash or phantom-fill."""
    store = make_store(taker_bps=0)
    result = store.execute_trade("a", "BTC-USD", 1e-10, "buy")
    assert not result.success


def test_nan_quantity_rejected(make_store):
    """B5: NaN used to bypass every guard and poison cash/positions."""
    import math

    store = make_store(taker_bps=0)
    result = store.execute_trade("a", "BTC-USD", float("nan"), "buy")
    assert not result.success
    acct = store.get_or_create("a")
    assert math.isfinite(acct.cash)
    assert acct.positions == {}


def test_full_position_sell_after_fractional_buys(make_store):
    """B6: float drift (0.1+0.1+0.7) must not block the closing sell."""
    store = make_store(taker_bps=0)
    for qty in (0.1, 0.1, 0.7):
        assert store.execute_trade("a", "SOL-USD", qty, "buy").success
    result = store.execute_trade("a", "SOL-USD", 0.9, "sell")
    assert result.success, result.message
    assert store.get_or_create("a").positions == {}


# ── Ledger persistence (wallet survives a restart) ───────────────


def test_store_without_ledger_stays_purely_in_memory(make_store):
    store = make_store(taker_bps=0)
    assert store.execute_trade("a", "SOL-USD", 0.5, "buy").success

    assert store.get_or_create("a").trade_count == 1
    assert len(store.trade_log) == 1


def test_attach_ledger_hydrates_persisted_accounts(make_store, make_ledger):
    ledger = make_ledger()
    acct = AgentAccount(cash=42_000.0, trade_count=3, total_fees_paid=12.5, realized_pnl=-3.0)
    acct.positions["SOL-USD"] = 2.0
    acct.cost_basis["SOL-USD"] = 200.2
    acct.avg_entry_ts["SOL-USD"] = 1_700_000_000.0
    ledger.save_fill(
        agent_id="whale",
        account=acct,
        timestamp="2024-01-01T12:00:00",
        action="buy",
        product_id="SOL-USD",
        quantity=2.0,
        price=100.1,
        fee=12.5,
        cash_after=42_000.0,
        latency=None,
    )

    store = make_store(taker_bps=0)
    assert store.accounts == {}  # nothing hydrated before attach
    store.attach_ledger(ledger)

    hydrated = store.get_or_create("whale")
    assert hydrated is not acct  # a fresh instance built from the ledger rows
    assert hydrated.cash == pytest.approx(42_000.0)
    assert hydrated.positions == pytest.approx({"SOL-USD": 2.0})
    assert hydrated.cost_basis == pytest.approx({"SOL-USD": 200.2})
    assert hydrated.avg_entry_ts == pytest.approx({"SOL-USD": 1_700_000_000.0})
    assert hydrated.trade_count == 3
    assert hydrated.total_fees_paid == pytest.approx(12.5)
    assert hydrated.realized_pnl == pytest.approx(-3.0)
    assert [e.action for e in store.trade_log] == ["buy"]


def test_reopened_store_restores_cash_positions_and_counters(make_store_with_ledger):
    first = make_store_with_ledger(taker_bps=10)
    assert first.execute_trade("a", "SOL-USD", 0.5, "buy").success
    assert first.execute_trade("a", "XRP-USD", 5.0, "buy").success
    assert first.execute_trade("a", "SOL-USD", 0.2, "sell").success
    before = first.get_or_create("a")

    restarted = make_store_with_ledger(taker_bps=10)  # same DB file → simulated restart
    after = restarted.get_or_create("a")

    assert after.cash == pytest.approx(before.cash)
    assert after.positions == pytest.approx(before.positions)
    assert after.cost_basis == pytest.approx(before.cost_basis)
    assert after.avg_entry_ts == pytest.approx(before.avg_entry_ts)
    assert after.trade_count == before.trade_count == 3
    assert after.total_fees_paid == pytest.approx(before.total_fees_paid)
    assert after.realized_pnl == pytest.approx(before.realized_pnl)
    # The whole point of the ledger: a restart no longer resets the wallet.
    assert after.cash != pytest.approx(INITIAL_CASH)

    assert [e.action for e in restarted.trade_log] == ["buy", "buy", "sell"]
    # The panel expects HH:MM:SS timestamps; ISO values from SQLite are reformatted.
    assert all(len(e.timestamp) == 8 and e.timestamp.count(":") == 2 for e in restarted.trade_log)


def test_full_sell_then_reopen_leaves_no_position(make_store_with_ledger):
    first = make_store_with_ledger(taker_bps=0)
    assert first.execute_trade("a", "SOL-USD", 0.5, "buy").success
    assert first.execute_trade("a", "SOL-USD", 0.5, "sell").success

    restarted = make_store_with_ledger(taker_bps=0)
    acct = restarted.get_or_create("a")

    assert acct.positions == {}
    assert acct.cost_basis == {}
    assert acct.avg_entry_ts == {}
    assert acct.trade_count == 2
    assert acct.cash == pytest.approx(INITIAL_CASH - SOL_ASK * 0.5 + SOL_BID * 0.5)


def test_rejected_trade_is_not_persisted(make_store_with_ledger, make_ledger):
    store = make_store_with_ledger(taker_bps=0)
    assert not store.execute_trade("a", "SOL-USD", 0.5, "sell").success  # nothing held
    assert not store.execute_trade("a", "DOGE-USD", 1.0, "buy").success  # no live price

    reopened = make_ledger()
    assert reopened.load_accounts() == {}
    assert reopened.recent_fills() == []


def test_attach_ledger_refuses_to_discard_live_state(make_store, make_ledger):
    """Re-attaching would silently drop every account/trade the store already holds."""
    store = make_store(taker_bps=0)
    store.attach_ledger(make_ledger())
    assert store.execute_trade("a", "SOL-USD", 0.5, "buy").success

    with pytest.raises(RuntimeError, match="already holds"):
        store.attach_ledger(make_ledger())


def test_attach_ledger_refuses_when_only_the_trade_log_is_populated(make_store, make_ledger):
    store = make_store(taker_bps=0)
    store.attach_ledger(make_ledger())
    assert store.execute_trade("a", "SOL-USD", 0.5, "buy").success
    store.accounts.clear()  # even a half-populated store must not be re-hydrated over

    with pytest.raises(RuntimeError, match="already holds"):
        store.attach_ledger(make_ledger())


# ── Ledger failure handling ─────────────────


def test_broken_ledger_keeps_the_fill_in_memory_and_flags_degraded(make_store, make_ledger, caplog):
    """A disk error must not lose the fill, but the operator has to be able to see it."""
    ledger = make_ledger()
    store = make_store(taker_bps=0)
    store.attach_ledger(ledger)
    ledger.close()  # the DB handle dies under us (unmounted disk, disk full, ...)

    result = store.execute_trade("a", "SOL-USD", 0.5, "buy")

    assert result.success, result.message  # same behaviour as before: the fill stands
    assert store.ledger_degraded is True
    assert any(rec.levelname == "ERROR" for rec in caplog.records)
    assert "ledger" in caplog.text.lower()

    acct = store.get_or_create("a")
    assert acct.trade_count == 1
    assert acct.positions == pytest.approx({"SOL-USD": 0.5})
    assert acct.cash == pytest.approx(INITIAL_CASH - SOL_ASK * 0.5)


def test_healthy_ledger_leaves_degraded_flag_false(make_store_with_ledger):
    store = make_store_with_ledger(taker_bps=0)

    assert store.ledger_degraded is False
    assert store.execute_trade("a", "SOL-USD", 0.5, "buy").success
    assert store.ledger_degraded is False


class _FailingFills:
    """A ledger whose fill insert breaks while the handle itself still works."""

    def __init__(self, wrapped: Ledger) -> None:
        self._wrapped = wrapped
        self.path = wrapped.path
        # Every marker write, so a test can prove repeated failures write only one.
        self.degraded_marks: list[tuple[str, str]] = []

    def load_accounts(self):
        return self._wrapped.load_accounts()

    def recent_fills(self):
        return self._wrapped.recent_fills()

    def save_fill(self, **kwargs):
        raise sqlite3.OperationalError("disk I/O error")

    def mark_degraded(self, agent_id: str, timestamp: str) -> None:
        self.degraded_marks.append((timestamp, agent_id))
        self._wrapped.mark_degraded(agent_id, timestamp)

    def mark_healthy(self) -> None:
        self._wrapped.mark_healthy()

    def degraded_marker(self):
        return self._wrapped.degraded_marker()


def test_degraded_fill_records_a_durable_marker_for_status(make_store, make_ledger):
    """`manage_bot status` is another process: the in-memory flag cannot reach it."""
    ledger = make_ledger()
    store = make_store(taker_bps=0)
    store.attach_ledger(_FailingFills(ledger))

    assert store.execute_trade("a", "SOL-USD", 0.5, "buy").success
    assert store.ledger_degraded is True

    with Ledger(ledger.path, read_only=True) as reader:
        marker = reader.degraded_marker()
    assert marker is not None
    assert marker["agent"] == "a"
    assert datetime.fromisoformat(marker["ts"])  # a real timestamp, not a placeholder


def test_repeated_failures_write_the_degraded_marker_once(make_store, make_ledger):
    """S2: the marker's ts is when the divergence STARTED, not the latest failure.

    Rewriting it on every failed fill would push the timestamp forward, so
    ``status`` would claim every earlier fill made it to disk. It would also hammer a
    database that is already broken. The in-memory flag stays sticky for the rest of
    the process: those fills are missing on disk permanently.
    """
    ledger = make_ledger()
    failing = _FailingFills(ledger)
    store = make_store(taker_bps=0)
    store.attach_ledger(failing)

    assert store.execute_trade("a", "SOL-USD", 0.5, "buy").success
    assert len(failing.degraded_marks) == 1
    first_ts, first_agent = failing.degraded_marks[0]

    assert store.execute_trade("a", "SOL-USD", 0.2, "buy").success

    assert failing.degraded_marks == [(first_ts, first_agent)]
    assert store.ledger_degraded is True
    with Ledger(ledger.path, read_only=True) as reader:
        assert reader.degraded_marker()["ts"] == first_ts


def test_attach_ledger_keeps_the_degraded_marker_by_default(make_store, make_ledger):
    """S1: hydrating is not a successful startup — Kafka may refuse the very next call."""
    ledger = make_ledger()
    ledger.mark_degraded("momentum", "2025-01-01T00:00:00")

    store = make_store(taker_bps=0)
    store.attach_ledger(ledger)

    assert store.ledger_degraded is False
    assert ledger.degraded_marker() == {"ts": "2025-01-01T00:00:00", "agent": "momentum"}


def test_attach_ledger_clears_the_marker_only_when_asked(make_store, make_ledger):
    ledger = make_ledger()
    ledger.mark_degraded("momentum", "2025-01-01T00:00:00")

    store = make_store(taker_bps=0)
    store.attach_ledger(ledger, clear_marker=True)

    assert store.ledger_degraded is False
    assert ledger.degraded_marker() is None


def test_mark_ledger_healthy_clears_a_marker_written_before_hydration(make_store, make_ledger):
    """The deploy hook calls this only once the broker is up (startup really succeeded)."""
    ledger = make_ledger()
    ledger.mark_degraded("momentum", "2025-01-01T00:00:00")
    store = make_store(taker_bps=0)
    store.attach_ledger(ledger)

    store.mark_ledger_healthy()

    assert ledger.degraded_marker() is None


def test_mark_ledger_healthy_is_a_noop_without_a_ledger(make_store):
    store = make_store(taker_bps=0)

    store.mark_ledger_healthy()  # must not raise: --no-ledger runs have nothing to clear

    assert store.ledger_degraded is False


def test_store_without_ledger_is_never_degraded(make_store):
    store = make_store(taker_bps=0)
    assert store.execute_trade("a", "SOL-USD", 0.5, "buy").success
    assert store.ledger_degraded is False


# ── Concurrency: overlapping fills ─────────────


def _run_parallel_trades(store: AccountStore, agent_id: str, thread_count: int) -> list:
    """Fire ``thread_count`` fills at one agent simultaneously, returning their failures."""
    barrier = threading.Barrier(thread_count)
    failures: list[BaseException] = []
    results: list[TradeResult] = []

    def trade() -> None:
        barrier.wait(timeout=10)  # force every thread into execute_trade at once
        try:
            # XRP keeps 32 concurrent buys (32 × $0.201) well inside the $100 wallet.
            results.append(store.execute_trade(agent_id, "XRP-USD", 0.1, "buy"))
        except BaseException as exc:  # noqa: BLE001 - any escape is the bug under test
            failures.append(exc)

    threads = [threading.Thread(target=trade) for _ in range(thread_count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
        assert not thread.is_alive(), f"thread hung: {agent_id}"

    assert failures == [], failures
    assert len(results) == thread_count
    assert all(result.success for result in results), [r.message for r in results]
    return results


def test_parallel_trades_for_one_agent_keep_fills_in_sync_with_trade_count(make_store, make_ledger):
    """M1/S4 regression: concurrent fills must not crash nor drop ledger rows."""
    ledger = make_ledger()
    store = make_store(taker_bps=0)
    store.attach_ledger(ledger)

    rounds = 3
    thread_count = 32
    agent_ids = ["agent-0", "agent-1", "agent-2"]
    for agent_id in agent_ids:
        _run_parallel_trades(store, agent_id, thread_count)
        assert store.get_or_create(agent_id).trade_count == thread_count

    expected_fills = sum(store.get_or_create(agent_id).trade_count for agent_id in agent_ids)

    reader = Ledger(ledger.path, read_only=True)
    try:
        fills = reader.recent_fills(limit=rounds * thread_count)
    finally:
        reader.close()

    assert expected_fills == rounds * thread_count
    assert len(fills) == expected_fills
    assert all(fill.agent_id in agent_ids for fill in fills)


# ── Churn guard: the store wires the turnover policy into execute_trade ──
# See arena/trade_policy.py — 140 fills in two hours cost $2.06 of fees on $100 of
# equity, so the store must refuse a re-entry that the guard flags while keeping the
# exit open.

PERP = "SOLUSDT-PERP"
PERP_TICKER = {
    "product_id": PERP,
    "price": "50.0",
    "best_bid": "50.0",
    "best_ask": "50.0",
    "best_bid_size": "10",
    "best_ask_size": "10",
    "side": "buy",
    "last_size": "1.0",
    "volume_24h": "1000.0",
    "time": "2024-01-01T00:00:00Z",
}


def _stalled_store(make_store):
    """A store whose only churn limit is the cooldown, so no test waits for an hour."""
    store = make_store(taker_bps=0)
    store.set_churn_limits(
        ChurnLimits(max_fills_per_hour=None, cooldown_seconds=60.0, max_fee_burn_pct_per_hour=None)
    )
    return store


def test_churn_guard_rejects_a_second_fill_in_the_same_product(make_store):
    store = _stalled_store(make_store)

    first = store.execute_trade("a", "SOL-USD", 0.1, "buy")
    second = store.execute_trade("a", "SOL-USD", 0.1, "buy")

    assert first.success, first.message
    assert second.success is False
    assert "cooldown" in second.message.lower()
    # The refusal must not touch the account: no position, no fee, no trade count.
    account = store.get_or_create("a")
    assert account.positions["SOL-USD"] == pytest.approx(0.1)
    assert account.trade_count == 1
    assert account.total_fees_paid == pytest.approx(0.0)


def test_churn_guard_still_allows_closing_the_position(make_store):
    store = _stalled_store(make_store)
    assert store.execute_trade("a", "SOL-USD", 0.1, "buy").success

    # Same product, same instant: reducing exposure must never be blocked.
    closing = store.execute_trade("a", "SOL-USD", 0.1, "sell")

    assert closing.success, closing.message
    assert "SOL-USD" not in store.get_or_create("a").positions


def test_churn_guard_rejects_a_perp_reopen_and_allows_the_close(make_store):
    store = _stalled_store(make_store)
    store.price_book.update(dict(PERP_TICKER))

    opened = store.execute_trade("scalper-perp", PERP, 1.0, "buy")
    reopened = store.execute_trade("scalper-perp", PERP, 1.0, "buy")
    closed = store.execute_trade("scalper-perp", PERP, 1.0, "sell")

    assert opened.success, opened.message
    assert reopened.success is False
    assert "cooldown" in reopened.message.lower()
    assert closed.success, closed.message
    assert PERP not in store.get_or_create("scalper-perp").positions


def test_churn_guard_is_off_by_default(make_store):
    store = make_store(taker_bps=0)
    store.price_book.update(dict(PERP_TICKER))

    # Nothing configured: the pre-guard behaviour is unchanged, so every fill books.
    for _ in range(4):
        assert store.execute_trade("a", PERP, 0.1, "buy").success
    assert store.get_or_create("a").trade_count == 4


def test_churn_guard_never_blocks_a_replay(make_store):
    store = _stalled_store(make_store)
    assert store.execute_trade("a", "SOL-USD", 0.1, "buy").success

    # A replay books a fill that already happened; re-gating it would drop real money
    # from the ledger.
    replayed = store.execute_trade("a", "SOL-USD", 0.1, "buy", replay=True)

    assert replayed.success, replayed.message


def test_churn_guard_does_not_spend_the_budget_on_replayed_history(make_store):
    """A restart must not hand the guard an hour that a previous process already spent.

    Replayed fills were paid by the process that recorded them. If they were stamped
    with the current clock, restoring the ledger would fill the hour's budget before
    the agent made a single decision, and the first live trade of the day would be
    refused for spending that never happened here.
    """
    store = make_store(taker_bps=0)
    store.set_churn_limits(
        ChurnLimits(max_fills_per_hour=2, cooldown_seconds=None, max_fee_burn_pct_per_hour=None)
    )

    for _ in range(4):
        assert store.execute_trade("a", "SOL-USD", 0.1, "buy", replay=True).success

    # Two fills of real budget remain, which is only true if the replays bought nothing.
    assert store.execute_trade("a", "SOL-USD", 0.1, "buy").success
    assert store.execute_trade("a", "SOL-USD", 0.1, "buy").success
    third = store.execute_trade("a", "SOL-USD", 0.1, "buy")
    assert third.success is False
    assert "fills" in third.message.lower()


def test_churn_guard_clears_when_limits_are_set_back_to_none(make_store):
    store = _stalled_store(make_store)
    assert store.execute_trade("a", "SOL-USD", 0.1, "buy").success

    store.set_churn_limits(None)

    assert store.execute_trade("a", "SOL-USD", 0.1, "buy").success


# ── Exit floor: a close inside the stop/target band is refused ────
#
# Measured on the real ledger: 26% of exit legs (29/111) closed at 0-10 bps of profit
# against a 10 bps round trip, median move 0.00 bps. The floor makes the store refuse
# the close *before* the order goes out, while still letting the stop, the target and a
# stale position through. The position being closed is the account's, so this lives in
# the store and not in the pure policy module (see arena.trade_policy.ExitFloor).


def _floored_store(make_store, **floor) -> AccountStore:
    """A fee-free store with an exit floor installed, so bps arithmetic is exact."""
    store = make_store(taker_bps=0)
    store.set_exit_floor(ExitFloor(**floor))
    return store


def _perp_at(store: AccountStore, bid: float, ask: float, product_id: str = PERP) -> None:
    store.price_book.update(
        {
            **PERP_TICKER,
            "product_id": product_id,
            "price": f"{(bid + ask) / 2:.4f}",
            "best_bid": f"{bid:.4f}",
            "best_ask": f"{ask:.4f}",
        }
    )


def _open_long(store: AccountStore, qty: float = 1.0, price: float = 50.0) -> None:
    _perp_at(store, bid=price, ask=price)
    opened = store.execute_trade("a", PERP, qty, "buy")
    assert opened.success, opened.message


def test_exit_floor_refuses_a_scratch_close_inside_the_band(make_store):
    store = _floored_store(make_store)
    _open_long(store)
    # +10 bps of profit: one bid tick above the entry, i.e. exactly the leg that pays a
    # 10 bps round trip to make nothing.
    _perp_at(store, bid=50.05, ask=50.05)

    scratch = store.execute_trade("a", PERP, 1.0, "sell")

    assert scratch.success is False
    assert "exit floor" in scratch.message.lower()
    # The refusal must not touch the account: still long, still one fill.
    account = store.get_or_create("a")
    assert account.positions[PERP] == pytest.approx(1.0)
    assert account.trade_count == 1


def test_exit_floor_allows_the_close_at_the_stop(make_store):
    """Cutting a loss is the whole reason the stop exists — never block it."""
    store = _floored_store(make_store)
    _open_long(store)
    _perp_at(store, bid=49.70, ask=49.70)  # -60 bps, past the -50 stop

    stopped = store.execute_trade("a", PERP, 1.0, "sell")

    assert stopped.success, stopped.message
    assert PERP not in store.get_or_create("a").positions


def test_exit_floor_allows_the_close_at_the_target(make_store):
    store = _floored_store(make_store)
    _open_long(store)
    _perp_at(store, bid=50.60, ask=50.60)  # +120 bps, past the +100 target

    closed = store.execute_trade("a", PERP, 1.0, "sell")

    assert closed.success, closed.message
    assert PERP not in store.get_or_create("a").positions


def test_exit_floor_judges_a_partial_trim_too(make_store):
    """A trim closes old lots just as a full close does, so it lands in the same band."""
    store = _floored_store(make_store)
    _open_long(store)
    _perp_at(store, bid=50.05, ask=50.05)

    trim = store.execute_trade("a", PERP, 0.5, "sell")

    assert trim.success is False
    assert "exit floor" in trim.message.lower()
    assert store.get_or_create("a").positions[PERP] == pytest.approx(1.0)


def test_exit_floor_judges_a_flip_that_closes_the_old_lot(make_store):
    """A flip sells through the long lot: judging by |pos| alone would let it scratch.

    ``abs(pos_new) < abs(pos_old)`` is false for a flip, so a ``reduces_exposure`` gate
    would wave it through — and a flip at +5 bps is precisely the scratch exit the
    floor exists to stop.
    """
    store = _floored_store(make_store)
    _open_long(store)
    _perp_at(store, bid=50.05, ask=50.05)

    flip = store.execute_trade("a", PERP, 1.5, "sell")

    assert flip.success is False
    assert "exit floor" in flip.message.lower()
    assert store.get_or_create("a").positions[PERP] == pytest.approx(1.0)


def test_exit_floor_does_not_judge_a_position_it_does_not_close(make_store):
    store = _floored_store(make_store)
    _open_long(store, qty=0.4)
    _perp_at(store, bid=49.95, ask=49.95)  # -10 bps: inside the band

    added = store.execute_trade("a", PERP, 0.4, "buy")

    # Adding to the long leaves every old lot in place; it is not a close.
    assert added.success, added.message
    assert store.get_or_create("a").positions[PERP] == pytest.approx(0.8)


def test_exit_floor_does_not_judge_opening_from_flat(make_store):
    store = _floored_store(make_store)
    _perp_at(store, bid=49.95, ask=49.95)

    opened = store.execute_trade("a", PERP, 0.2, "sell")

    # A brand-new short has no entry basis to be measured against yet.
    assert opened.success, opened.message
    assert store.get_or_create("a").positions[PERP] == pytest.approx(-0.2)


def test_exit_floor_fails_open_when_the_entry_timestamp_is_missing(make_store):
    """A position hydrated across a restart has no ``avg_entry_ts``: never trap it."""
    store = _floored_store(make_store)
    _open_long(store)
    store.get_or_create("a").avg_entry_ts.pop(PERP)
    _perp_at(store, bid=50.05, ask=50.05)

    closed = store.execute_trade("a", PERP, 1.0, "sell")

    assert closed.success, closed.message


def test_exit_floor_fails_open_when_the_cost_basis_is_missing(make_store):
    store = _floored_store(make_store)
    _open_long(store)
    store.get_or_create("a").cost_basis.pop(PERP)
    _perp_at(store, bid=50.05, ask=50.05)

    closed = store.execute_trade("a", PERP, 1.0, "sell")

    assert closed.success, closed.message


def test_exit_floor_lets_a_stale_position_go_after_the_hold_limit(make_store):
    store = _floored_store(make_store)
    _open_long(store)
    account = store.get_or_create("a")
    account.avg_entry_ts[PERP] = datetime.now(timezone.utc).timestamp() - 4_000
    _perp_at(store, bid=50.05, ask=50.05)

    closed = store.execute_trade("a", PERP, 1.0, "sell")

    assert closed.success, closed.message


def test_exit_floor_is_off_by_default_and_can_be_switched_off(make_store):
    unwired = make_store(taker_bps=0)
    _open_long(unwired)
    _perp_at(unwired, bid=50.05, ask=50.05)
    assert unwired.execute_trade("a", PERP, 1.0, "sell").success  # pre-guard behaviour

    store = _floored_store(make_store)
    _open_long(store)
    _perp_at(store, bid=50.05, ask=50.05)
    assert store.execute_trade("a", PERP, 1.0, "sell").success is False

    store.set_exit_floor(None)
    assert store.execute_trade("a", PERP, 1.0, "sell").success


def test_exit_floor_never_judges_a_replay(make_store):
    """Replay books a fill that already happened; re-gating it would drop real money."""
    store = _floored_store(make_store)
    _open_long(store)
    _perp_at(store, bid=50.05, ask=50.05)

    replayed = store.execute_trade("a", PERP, 1.0, "sell", replay=True)

    assert replayed.success, replayed.message


def test_exit_floor_judges_a_spot_scratch_exit_too(make_store):
    """Spot closes run through the same cost basis, so the same band applies."""
    store = _floored_store(make_store)
    assert store.execute_trade("a", "SOL-USD", 0.1, "buy").success  # entry ≈ 100.10
    store.price_book.update(
        {
            "product_id": "SOL-USD",
            "price": "100.20",
            "best_bid": "100.20",
            "best_ask": "100.20",
        }
    )

    scratch = store.execute_trade("a", "SOL-USD", 0.1, "sell")

    assert scratch.success is False
    assert "exit floor" in scratch.message.lower()
    assert store.get_or_create("a").positions["SOL-USD"] == pytest.approx(0.1)


# ── close_position: the close the watchdog is allowed to ask for ─
#
# The watchdog decides *which* position is stale, but it must not decide the *size*:
# a read that is one fill out of date turns a close into an opening order on the other
# side (a long of 8.0 read at 8.0, closed by the agent while the watchdog was between
# calls, becomes a short of 8.0). Resolving the size under the same lock that books the
# fill is the only version of this that cannot flip.


def test_close_position_flattens_what_is_still_open(make_store):
    store = make_store(taker_bps=0)
    store.price_book.update(dict(PERP_TICKER))
    assert store.execute_trade("a", PERP, 1.0, "buy").success

    result = store.close_position("a", PERP)

    assert result.success, result.message
    assert PERP not in store.get_or_create("a").positions


def test_close_position_closes_a_short_by_buying(make_store):
    store = make_store(taker_bps=0)
    store.price_book.update(dict(PERP_TICKER))
    assert store.execute_trade("a", PERP, 1.0, "sell").success

    assert store.close_position("a", PERP).success

    assert PERP not in store.get_or_create("a").positions


def test_close_position_after_a_partial_fill_closes_only_the_remainder(make_store):
    """The race, from the side that used to bite: the agent got there first."""
    store = make_store(taker_bps=0)
    store.price_book.update(dict(PERP_TICKER))
    assert store.execute_trade("a", PERP, 1.0, "buy").success
    assert store.execute_trade("a", PERP, 0.5, "sell").success

    assert store.close_position("a", PERP).success

    assert PERP not in store.get_or_create("a").positions


def test_close_position_on_a_flat_book_opens_nothing(make_store):
    """The blocker: an order the watchdog meant as a close must never open a position."""
    store = make_store(taker_bps=0)
    store.price_book.update(dict(PERP_TICKER))

    result = store.close_position("a", PERP)

    assert result.success is False
    assert "flat" in result.message.lower()
    assert store.get_or_create("a").positions == {}
    assert store.get_or_create("a").trade_count == 0


def test_close_position_is_a_no_op_the_second_time(make_store):
    """Two ticking watchdogs (or a restart) must not trade against each other."""
    store = make_store(taker_bps=0)
    store.price_book.update(dict(PERP_TICKER))
    assert store.execute_trade("a", PERP, 1.0, "buy").success
    assert store.close_position("a", PERP).success

    again = store.close_position("a", PERP)

    assert again.success is False
    assert store.get_or_create("a").positions == {}
    assert store.get_or_create("a").trade_count == 2


# ── Churn guard: a flip is a re-entry, not a reduce ──────────────


def test_churn_guard_does_not_treat_a_flip_as_a_reduce(make_store):
    """``abs(pos_after) < abs(pos_now)`` is true for a *smaller* flip — and wrong.

    Selling 1.5 against a long of 1.0 closes 1.0 and opens a short of 0.5: new
    exposure, paid for with a fill the cooldown exists to space out. The guard read it
    as a reduce and waved it through one second after the entry.
    """
    store = _stalled_store(make_store)
    store.price_book.update(dict(PERP_TICKER))
    assert store.execute_trade("a", PERP, 1.0, "buy").success

    flip = store.execute_trade("a", PERP, 1.5, "sell")

    assert flip.success is False
    assert "cooldown" in flip.message.lower()
    assert store.get_or_create("a").positions[PERP] == pytest.approx(1.0)


def test_churn_guard_still_lets_the_close_out_after_a_refused_flip(make_store):
    """The flip is refused, the honest exit is not — the guard spaces entries, not exits."""
    store = _stalled_store(make_store)
    store.price_book.update(dict(PERP_TICKER))
    assert store.execute_trade("a", PERP, 1.0, "buy").success
    assert store.execute_trade("a", PERP, 1.5, "sell").success is False

    closing = store.execute_trade("a", PERP, 1.0, "sell")

    assert closing.success, closing.message
    assert PERP not in store.get_or_create("a").positions


def test_churn_guard_sees_an_overshooting_close_as_the_close_it_will_be(make_store):
    """The clamp happens in the executor; the churn gate judges the intent *before* it.

    Selling 8.9 against a long of 8.8 is booked as a close of 8.8 (its opening leg is
    below the venue floor), but the guard read the raw 8.9, called it a flip and refused
    a close the executor would have allowed. Both gates now ask the same function.
    """
    from arena.instruments import SymbolRules
    from decimal import Decimal

    store = make_store(taker_bps=0)
    store.price_book.update(dict(PERP_TICKER))
    store.set_instrument_rules(
        {
            PERP: SymbolRules(
                symbol="SOLUSDT",
                base_asset="SOL",
                quote_asset="USDT",
                step_size=Decimal("0.01"),
                min_qty=Decimal("0.01"),
                min_notional=Decimal("20"),
                tick_size=Decimal("0.01"),
            )
        }
    )
    store.set_churn_limits(
        ChurnLimits(max_fills_per_hour=None, cooldown_seconds=60.0, max_fee_burn_pct_per_hour=None)
    )
    assert store.execute_trade("a", PERP, 1.8, "buy").success

    closing = store.execute_trade("a", PERP, 1.81, "sell")

    assert closing.success, closing.message
    assert PERP not in store.get_or_create("a").positions
