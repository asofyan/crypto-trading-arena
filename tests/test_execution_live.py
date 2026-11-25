"""LiveBinanceExecutor: journal → venue → venue's numbers.

The venue is faked here (no network, no keys) but everything else is real: the SQLite
ledger in ``tmp_path``, the AccountStore, the price book, the risk gates and the
symbol rules. That is deliberate — the failure modes worth pinning are ordering and
idempotency, not HTTP.
"""

from __future__ import annotations

import pytest

from arena.account_store import AccountStore
from arena.execution import FillRequest, LiveBinanceExecutor
from arena.fees import FeeModel
from arena.instruments import parse_exchange_info
from arena.ledger import Ledger
from arena.price_book import PriceBook
from arena.risk import LiveLimits, RiskGates
from exchanges.binance_spot import (
    BinanceApiError,
    InsufficientBalance,
    IpBanned,
    RateLimited,
    UnknownOutcome,
    VenueFill,
)

SOL_ASK = 100.10
SOL_BID = 99.90

SOLUSDT_INFO = {
    "symbols": [
        {
            "symbol": "SOLUSDT",
            "status": "TRADING",
            "baseAsset": "SOL",
            "quoteAsset": "USDT",
            "filters": [
                {
                    "filterType": "LOT_SIZE",
                    "minQty": "0.00100000",
                    "maxQty": "90000.00000000",
                    "stepSize": "0.00100000",
                },
                {
                    "filterType": "NOTIONAL",
                    "minNotional": "5.00000000",
                    "applyMinToMarket": True,
                },
            ],
        }
    ]
}


class FakeClient:
    """Records the call, and answers with whatever the test planned."""

    def __init__(self, outcome: VenueFill | Exception | None = None) -> None:
        self.outcome = outcome
        self.calls: list[tuple[str, float, str]] = []
        self.journal_at_call: str | None = None
        self._ledger: Ledger | None = None
        # Startup reconciliation: what the venue says about a dangling order.
        self.reconcile_outcomes: dict[str, VenueFill | None] = {}
        self.lookups: list[tuple[str, str]] = []
        self.lookup_error: Exception | None = None

    def get_order(self, symbol: str, client_order_id: str):
        self.lookups.append((symbol, client_order_id))
        if self.lookup_error is not None:
            raise self.lookup_error
        return self.reconcile_outcomes.get(client_order_id)

    def place_market_buy(self, symbol: str, quote_qty: float, client_order_id: str):
        return self._respond("buy", symbol, quote_qty, client_order_id)

    def place_market_sell(self, symbol: str, quantity: float, client_order_id: str):
        return self._respond("sell", symbol, quantity, client_order_id)

    def _respond(self, side: str, symbol: str, amount: float, client_order_id: str):
        self.calls.append((side, amount, client_order_id))
        if self._ledger is not None:
            order = self._ledger.get_order(client_order_id)
            self.journal_at_call = order.state if order else None
        if isinstance(self.outcome, Exception):
            raise self.outcome
        if self.outcome is not None:
            return self.outcome
        return VenueFill(
            client_order_id=client_order_id,
            venue_order_id="4242",
            symbol=symbol,
            side=side.upper(),
            status="FILLED",
            executed_qty=0.1,
            credited_qty=0.0999,  # 0.1 executed − 0.0001 base commission
            cum_notional=10.021,  # 0.1 × 100.21 — exactly what the venue debited
            price=100.21,
            fee=0.010021,
            fee_asset="SOL",
        )


def _fill(**overrides) -> VenueFill:
    payload = {
        "client_order_id": "arena-agent-1",
        "venue_order_id": "4242",
        "symbol": "SOLUSDT",
        "side": "BUY",
        "status": "FILLED",
        "executed_qty": 0.1,
        "credited_qty": 0.0999,
        "cum_notional": 10.021,
        "price": 100.21,
        "fee": 0.010021,
        "fee_asset": "SOL",
    }
    payload.update(overrides)
    return VenueFill(**payload)


@pytest.fixture
def book():
    pb = PriceBook()
    pb.update(
        {"product_id": "SOLUSDT", "price": "100.00", "best_bid": str(SOL_BID),
         "best_ask": str(SOL_ASK)}
    )
    pb.update(
        {"product_id": "BTCUSDT", "price": "50000.00", "best_bid": "49990",
         "best_ask": "50010"}
    )
    return pb


@pytest.fixture
def harness(tmp_path, book):
    """A live executor over a real store + real ledger, with a fake venue."""

    def _build(
        *,
        outcome=None,
        cash: float = 100.0,
        positions: dict[str, float] | None = None,
        dry_run: bool = False,
        gates: RiskGates | None = None,
        rules: dict | None = None,
        agent_accounts: dict[str, str] | None = None,
        on_fatal=None,
    ):
        counter = len(list(tmp_path.glob("arena-*.db")))
        ledger = Ledger(tmp_path / f"arena-{counter}.db")
        store = AccountStore(book, FeeModel(taker_bps=10))
        store.attach_ledger(ledger)
        account = store.get_or_create("agent")
        account.cash = cash
        for symbol, quantity in (positions or {}).items():
            account.positions[symbol] = quantity
            account.cost_basis[symbol] = quantity * 100.0

        parsed = rules if rules is not None else parse_exchange_info(SOLUSDT_INFO)
        store.set_instrument_rules(parsed)
        client = FakeClient(outcome)
        client._ledger = ledger
        executor = LiveBinanceExecutor(
            store,
            client,
            ledger,
            account="main",
            agent_accounts=agent_accounts or {"agent": "main"},
            rules=parsed,
            gates=gates
            or RiskGates(
                LiveLimits(
                    allow_symbols=("SOLUSDT",),
                    max_order_pct_of_equity=50.0,
                    max_position_pct_of_equity=None,
                    max_daily_loss_pct=50.0,
                    max_orders_per_minute=10,
                )
            ),
            dry_run=dry_run,
            on_fatal=on_fatal,
        )
        return executor, store, ledger, client

    return _build


BUY = FillRequest(agent_id="agent", product_id="SOLUSDT", quantity=0.1, action="buy")


# ── Journal first ───────────────────────────────────────────────


def test_the_intent_is_journaled_before_the_venue_is_called(harness):
    executor, _, ledger, client = harness()

    result = executor.execute(BUY)

    assert result.success, result.message
    assert client.journal_at_call == "PENDING"
    order = ledger.get_order(client.calls[0][2])
    assert order.state == "FILLED"
    assert order.venue_order_id == "4242"
    assert order.executed_qty == pytest.approx(0.0999)


# ── Booking the venue's numbers ─────────────────────────────────


def test_a_filled_buy_books_what_the_venue_reported(harness):
    executor, store, ledger, client = harness()

    result = executor.execute(BUY)

    account = store.get_or_create("agent")
    assert result.success, result.message
    assert account.positions["SOLUSDT"] == pytest.approx(0.0999)  # credited, not executed
    assert account.cash == pytest.approx(100.0 - 10.021, abs=0.001)  # exactly the debited quote
    assert account.total_fees_paid == pytest.approx(0.010021)
    fill = ledger.recent_fills()[-1]
    assert fill.quantity == pytest.approx(0.0999)
    assert fill.fee == pytest.approx(0.010021)


def test_a_filled_sell_credits_the_quote_amount_net_of_fee(harness):
    executor, store, _, client = harness(
        cash=0.0,
        positions={"SOLUSDT": 0.2},
        outcome=_fill(
            side="SELL", executed_qty=0.1, credited_qty=0.1, cum_notional=9.99,
            price=99.9, fee=0.00999, fee_asset="USDT",
        ),
    )

    result = executor.execute(
        FillRequest(agent_id="agent", product_id="SOLUSDT", quantity=0.1, action="sell")
    )

    account = store.get_or_create("agent")
    assert result.success, result.message
    assert account.positions["SOLUSDT"] == pytest.approx(0.1)
    assert account.cash == pytest.approx(9.99 - 0.00999, abs=0.001)
    assert client.calls[0][0] == "sell"


def test_a_replayed_fill_is_booked_only_once(harness):
    """The crash-replay path: same order id applied twice must not move money twice."""
    executor, store, _, client = harness()
    executor.execute(BUY)
    cash_after_first = store.get_or_create("agent").cash
    booked_id = client.calls[0][2]

    replay = store.apply_fill(
        "agent",
        "buy",
        "SOLUSDT",
        0.0999,
        100.21,
        0.010021,
        client_order_id=booked_id,
        fee_asset="SOL",
    )

    assert not replay.success
    assert "already booked" in replay.message
    assert store.get_or_create("agent").cash == pytest.approx(cash_after_first)


# ── Failure modes ───────────────────────────────────────────────


def test_an_unknown_outcome_is_journaled_and_never_retried(harness):
    executor, store, ledger, client = harness(outcome=UnknownOutcome("timed out"))

    result = executor.execute(BUY)

    order = ledger.get_order(client.calls[0][2])
    assert not result.success
    assert "outcome unknown" in result.message.lower()
    assert order.state == "UNKNOWN"
    assert order.error and "timed out" in order.error
    assert store.get_or_create("agent").positions == {}
    assert ledger.recent_fills() == []


def test_a_venue_rejection_is_journaled_with_its_code(harness):
    executor, store, ledger, client = harness(
        outcome=InsufficientBalance(-2010, "Account has insufficient balance")
    )

    result = executor.execute(BUY)

    order = ledger.get_order(client.calls[0][2])
    assert not result.success
    assert "insufficient balance" in result.message.lower()
    assert order.state == "REJECTED"
    assert order.error and "-2010" in order.error
    assert store.get_or_create("agent").positions == {}


def test_a_rate_limit_leaves_the_order_rejected_without_fatal(harness):
    failures: list[Exception] = []
    executor, _, ledger, client = harness(
        outcome=RateLimited(12.0), on_fatal=failures.append
    )

    result = executor.execute(BUY)

    assert not result.success
    assert ledger.get_order(client.calls[0][2]).state == "REJECTED"
    assert failures == []


def test_an_ip_ban_is_fatal_and_reports_to_the_operator(harness):
    failures: list[Exception] = []
    executor, _, _, client = harness(outcome=IpBanned(120.0), on_fatal=failures.append)

    result = executor.execute(BUY)

    assert not result.success
    assert len(failures) == 1 and isinstance(failures[0], IpBanned)
    assert client.calls  # the ban came from the venue, after the attempt


def test_an_order_the_venue_expired_books_nothing(harness):
    executor, store, ledger, client = harness(
        outcome=_fill(status="EXPIRED", executed_qty=0.0, credited_qty=0.0, cum_notional=0.0)
    )

    result = executor.execute(BUY)

    assert not result.success
    assert ledger.get_order(client.calls[0][2]).state == "EXPIRED"
    assert store.get_or_create("agent").positions == {}
    assert ledger.recent_fills() == []


# ── Dry run ────────────────────────────────────────────────────


def test_dry_run_journals_but_never_calls_the_venue(harness):
    executor, store, ledger, client = harness(dry_run=True)

    result = executor.execute(BUY)

    assert not result.success
    assert "DRY-RUN" in result.message
    assert client.calls == []
    orders = ledger.recent_orders()
    assert [o.state for o in orders] == ["DRY_RUN"]
    assert store.get_or_create("agent").positions == {}


# ── Refusals that must happen before the venue ─────────────────


def test_an_agent_without_a_live_mapping_is_refused(harness):
    executor, _, _, client = harness(agent_accounts={"someone-else": "main"})

    result = executor.execute(BUY)

    assert not result.success
    assert "Live trading is not enabled" in result.message
    assert client.calls == []


def test_a_symbol_outside_the_allowlist_is_refused(harness):
    executor, _, _, client = harness()
    result = executor.execute(
        FillRequest(agent_id="agent", product_id="BTCUSDT", quantity=0.001, action="buy")
    )

    assert not result.success
    assert "not enabled for live trading" in result.message
    assert client.calls == []


def test_a_stale_price_is_refused(harness, book):
    executor, _, _, client = harness()
    entry = book.get("SOLUSDT")
    entry["_received_at"] -= 60  # a minute-old quote

    result = executor.execute(BUY)

    assert not result.success
    assert "freshness" in result.message or "old" in result.message
    assert client.calls == []


def test_a_quote_of_unknown_age_is_refused(harness, book):
    executor, _, _, client = harness()
    del book.get("SOLUSDT")["_received_at"]

    result = executor.execute(BUY)

    assert not result.success
    assert "unknown age" in result.message
    assert client.calls == []


def test_a_sell_of_more_than_is_held_is_refused_before_the_venue(harness):
    executor, _, _, client = harness(cash=100.0)

    result = executor.execute(
        FillRequest(agent_id="agent", product_id="SOLUSDT", quantity=0.1, action="sell")
    )

    assert not result.success
    assert "Insufficient holdings" in result.message
    assert client.calls == []


def test_a_buy_that_the_wallet_cannot_fund_is_refused_before_the_venue(harness):
    # Cash sits above the notional ($10.01) but below notional + slippage buffer, so only
    # the executor's own check can catch it — the gate caps are wide open here.
    gates = RiskGates(
        LiveLimits(
            allow_symbols=("SOLUSDT",),
            max_order_pct_of_equity=100.0,
            max_position_pct_of_equity=None,
            max_daily_loss_pct=90.0,
        )
    )
    executor, _, _, client = harness(cash=10.05, gates=gates)

    result = executor.execute(BUY)

    assert not result.success
    assert "Insufficient cash" in result.message
    assert client.calls == []


def test_a_size_below_the_venue_minimum_is_refused(harness):
    executor, _, ledger, client = harness()
    result = executor.execute(
        FillRequest(agent_id="agent", product_id="SOLUSDT", quantity=0.01, action="buy")
    )

    assert not result.success
    assert "minimum" in result.message
    assert client.calls == []
    assert ledger.recent_orders() == []  # refusals never reach the journal


def test_a_risk_gate_denial_never_reaches_the_journal(harness):
    gates = RiskGates(
        LiveLimits(
            allow_symbols=("SOLUSDT",),
            max_order_pct_of_equity=1.0,  # 0.1 SOL (~$10) is 10% of $100
            max_position_pct_of_equity=None,
            max_daily_loss_pct=50.0,
        )
    )
    executor, store, ledger, client = harness(gates=gates)

    result = executor.execute(BUY)

    assert not result.success
    assert "exceeds" in result.message
    assert client.calls == []
    assert ledger.recent_orders() == []
    assert store.get_or_create("agent").positions == {}


def test_a_halt_flag_denies_everything(harness, tmp_path):
    flag = tmp_path / "LIVE_HALT"
    flag.write_text("operator\n")
    gates = RiskGates(
        LiveLimits(allow_symbols=("SOLUSDT",), max_order_pct_of_equity=50.0),
        is_halted=flag.exists,
    )
    executor, _, _, client = harness(gates=gates)

    result = executor.execute(BUY)

    assert not result.success
    assert "HALTED" in result.message
    assert client.calls == []


def test_the_gate_amounts_follow_the_real_equity(harness):
    """A deposit raises the ceiling: the same order passes once the wallet grows."""
    tight = RiskGates(
        LiveLimits(
            allow_symbols=("SOLUSDT",),
            max_order_pct_of_equity=15.0,
            max_position_pct_of_equity=None,
            max_daily_loss_pct=50.0,
        )
    )
    resized = FillRequest(agent_id="agent", product_id="SOLUSDT", quantity=0.12, action="buy")

    small_executor, _, _, small_client = harness(gates=tight, cash=50.0)
    assert not small_executor.execute(resized).success  # 0.12 SOL ~ $12 = 24% of $50
    assert small_client.calls == []

    big_executor, _, _, big_client = harness(gates=tight, cash=200.0)
    assert big_executor.execute(resized).success  # same order is 6% of $200
    assert big_client.calls


def test_unexpected_venue_errors_still_produce_a_readable_message(harness):
    executor, _, _, client = harness(outcome=BinanceApiError(-9000, "something odd"))

    result = executor.execute(BUY)

    assert not result.success
    assert "-9000" in result.message
    assert client.calls


# ── Crash recovery: resolving orders whose outcome was never learned ─────


def _journalled(ledger: Ledger, *, side: str = "buy", state: str = "PENDING") -> str:
    client_order_id = "arena-agent-crash01"
    ledger.save_order(
        client_order_id=client_order_id,
        agent_id="agent",
        account="main",
        symbol="SOLUSDT",
        side=side,
        state=state,
        qty_requested=0.1,
        quote_qty_requested=10.0,
    )
    return client_order_id


def test_a_post_that_never_landed_is_resolved_to_expired(harness):
    """Crash between journaling and the POST: the venue has never heard of the id."""
    executor, store, ledger, client = harness()
    client_order_id = _journalled(ledger)
    client.reconcile_outcomes[client_order_id] = None

    resolved = executor.settle_dangling()

    assert resolved == [(client_order_id, "never-landed")]
    assert ledger.get_order(client_order_id).state == "EXPIRED"
    assert ledger.recent_fills() == []
    assert store.get_or_create("agent").positions == {}
    assert ledger.dangling_orders() == []


def test_a_fill_the_crash_swallowed_is_booked_exactly_once(harness):
    """Crash after the POST: the venue filled it, this process never found out."""
    executor, store, ledger, client = harness()
    client_order_id = _journalled(ledger)
    client.reconcile_outcomes[client_order_id] = _fill(client_order_id=client_order_id)

    first = executor.settle_dangling()
    second = executor.settle_dangling()

    assert first == [(client_order_id, "booked")]
    assert second == []  # nothing left dangling, and the fill is idempotent anyway
    assert len(ledger.recent_fills()) == 1
    assert store.get_or_create("agent").positions["SOLUSDT"] == pytest.approx(0.0999)
    assert ledger.get_order(client_order_id).state == "FILLED"


def test_replaying_the_same_settlement_books_nothing_extra(harness):
    executor, store, ledger, client = harness()
    client_order_id = _journalled(ledger)
    client.reconcile_outcomes[client_order_id] = _fill(client_order_id=client_order_id)
    executor.settle_dangling()
    cash_after_first = store.get_or_create("agent").cash

    # Force the second pass by putting the row back into a dangling state.
    ledger.update_order(client_order_id, state="UNKNOWN")
    executor.settle_dangling()

    assert store.get_or_create("agent").cash == pytest.approx(cash_after_first)
    assert len(ledger.recent_fills()) == 1


def test_a_dangling_order_the_venue_expired_books_nothing(harness):
    executor, store, ledger, client = harness()
    client_order_id = _journalled(ledger)
    client.reconcile_outcomes[client_order_id] = _fill(
        client_order_id=client_order_id, status="EXPIRED", executed_qty=0.0,
        credited_qty=0.0, cum_notional=0.0,
    )

    assert executor.settle_dangling() == [(client_order_id, "expired")]
    assert ledger.recent_fills() == []
    assert ledger.get_order(client_order_id).state == "EXPIRED"


def test_a_fill_the_ledger_refuses_stays_unresolved(harness):
    """It happened at the venue but the account cannot book it: never paper over it."""
    executor, store, ledger, client = harness()  # no SOL holdings
    client_order_id = _journalled(ledger, side="sell")
    client.reconcile_outcomes[client_order_id] = _fill(
        client_order_id=client_order_id, side="SELL", executed_qty=0.1, credited_qty=0.1,
        cum_notional=9.99, price=99.9, fee=0.00999, fee_asset="USDT",
    )

    assert executor.settle_dangling() == [(client_order_id, "unbookable")]
    assert ledger.get_order(client_order_id).state == "UNKNOWN"
    assert ledger.recent_fills() == []
    # Still dangling, so the next reconcile reports the shortfall instead of hiding it.
    assert ledger.dangling_orders() != []


def test_a_lookup_failure_books_nothing_and_stays_dangling(harness):
    executor, _, ledger, client = harness()
    client_order_id = _journalled(ledger)
    client.lookup_error = OSError("venue unreachable")

    assert executor.settle_dangling() == [(client_order_id, "lookup-failed")]
    assert ledger.get_order(client_order_id).state == "PENDING"
    assert ledger.recent_fills() == []


def test_settling_nothing_is_free(harness):
    executor, _, ledger, _ = harness()

    assert executor.settle_dangling() == []
    assert ledger.recent_fills() == []
