from dataclasses import replace
from decimal import Decimal

import pytest

from arena.account_store import AccountStore
from arena.execution import FillRequest
from arena.fees import FeeModel
from arena.futures_execution import LiveBinanceFuturesExecutor
from arena.instruments import SymbolRules
from arena.ledger import Ledger
from arena.price_book import PriceBook
from arena.risk import LiveLimits, RiskGates
from exchanges.binance_spot import BinanceApiError, IpBanned, UnknownOutcome, VenueFill

PRODUCT = "SOLUSDT-PERP"
RULES = SymbolRules("SOLUSDT", "SOL", "USDT", Decimal(".01"), Decimal(".01"),
                    Decimal("5"), Decimal(".01"))


class Venue:
    def __init__(self, ledger):
        self.ledger = ledger
        self.calls = []
        self.fills = {}
        self.response = lambda fill: fill

    def place_market_order(self, symbol, side, quantity, client_order_id, *, reduce_only):
        assert self.ledger.get_order(client_order_id).state == "PENDING"
        self.calls.append((symbol, side, quantity, reduce_only))
        fill = VenueFill(client_order_id, str(len(self.calls)), symbol, side, "FILLED",
                         float(quantity), float(quantity), 100 * float(quantity), 100,
                         .05 * float(quantity), "USDT")
        self.fills[client_order_id] = fill
        return self.response(fill)

    def query_order(self, symbol, client_order_id):
        assert symbol == "SOLUSDT"
        return self.fills.get(client_order_id)

    def account_config(self):
        return {"canTrade": True, "dualSidePosition": False, "multiAssetsMargin": False}

    def bnb_fee_burn(self):
        return False

    def position_risk(self, symbol):
        amount = sum(
            fill.executed_qty * (1 if fill.side == "buy" else -1)
            for fill in self.fills.values()
        )
        return [{"symbol": symbol, "positionSide": "BOTH", "positionAmt": str(amount)}]

    def symbol_config(self, symbol):
        return [{"symbol": symbol, "leverage": 1, "marginType": "CROSSED"}]

    def futures_balances(self):
        return [{"asset": "USDT", "availableBalance": "100"}]


@pytest.fixture
def setup(tmp_path):
    ledger = Ledger(tmp_path / "arena.db")
    ledger.set_meta("live_session_start_equity", "100")
    book = PriceBook()
    book.update({"product_id": PRODUCT, "price": "100", "best_ask": "100", "best_bid": "100"})
    store = AccountStore(book, FeeModel())
    store.attach_ledger(ledger)
    client = Venue(ledger)
    unresolved = []
    gates = RiskGates(LiveLimits(allow_symbols=(PRODUCT,), max_orders_per_minute=20))
    executor = LiveBinanceFuturesExecutor(
        store, client, ledger, account="margin", agent_accounts={"agent": "margin"},
        rules={PRODUCT: RULES}, gates=gates, dry_run=False,
        id_factory=lambda _: f"order-{len(client.calls)}", on_unresolved=unresolved.append,
    )
    store.set_executor(executor)
    return executor, store, ledger, client, unresolved


@pytest.mark.parametrize(("side", "sign", "close_side"), [("buy", 1, "sell"), ("sell", -1, "buy")])
def test_open_and_close_with_venue_fee(setup, side, sign, close_side):
    executor, store, ledger, client, _ = setup
    assert executor.execute(FillRequest("agent", PRODUCT, .1, side)).success
    account = store.get_or_create("agent")
    assert account.positions[PRODUCT] == sign * .1
    assert ledger.get_order("order-0").state == "FILLED"
    assert store.close_position("agent", PRODUCT).success
    assert PRODUCT not in account.positions
    assert client.calls == [
        ("SOLUSDT", side, Decimal(".10"), False),
        ("SOLUSDT", close_side, Decimal(".10"), True),
    ]
    assert account.cash == pytest.approx(99.99)


def test_no_flip_and_dust_opening_leg_clamped(setup):
    executor, store, _, client, _ = setup
    assert executor.execute(FillRequest("agent", PRODUCT, .1, "buy")).success
    assert not executor.execute(FillRequest("agent", PRODUCT, .2, "sell")).success
    assert len(client.calls) == 1
    assert executor.execute(FillRequest("agent", PRODUCT, .11, "sell")).success
    assert PRODUCT not in store.get_or_create("agent").positions
    assert client.calls[-1][-2:] == (Decimal(".10"), True)


def test_unknown_blocks_orders_until_recovery(setup):
    executor, store, ledger, client, unresolved = setup

    def timeout(_):
        raise UnknownOutcome("lost response")

    client.response = timeout
    request = FillRequest("agent", PRODUCT, .1, "sell")
    assert not executor.execute(request).success
    assert ledger.get_order("order-0").state == "UNKNOWN"
    assert unresolved
    assert not executor.execute(request).success
    assert len(client.calls) == 1
    fill = client.fills.pop("order-0")
    assert executor.settle_dangling() == [("order-0", "not-found")]
    assert ledger.get_order("order-0").state == "UNKNOWN"
    client.fills["order-0"] = fill
    assert executor.settle_dangling() == [("order-0", "booked")]
    assert executor.settle_dangling() == []
    assert store.get_or_create("agent").positions[PRODUCT] == -.1


def test_terminal_partial_fill_uses_executed_quantity(setup):
    executor, store, _, client, _ = setup
    client.response = lambda fill: replace(
        fill, status="EXPIRED", executed_qty=.06, credited_qty=.06, cum_notional=6, fee=.003,
    )
    assert executor.execute(FillRequest("agent", PRODUCT, .1, "sell")).success
    assert store.get_or_create("agent").positions[PRODUCT] == -.06


def test_memory_only_fill_waits_for_hydration_before_recovery(setup, monkeypatch):
    executor, store, ledger, client, unresolved = setup

    def fail(**_):
        raise OSError("disk offline")

    with monkeypatch.context() as patch:
        patch.setattr(ledger, "settle_fill", fail)
        result = executor.execute(FillRequest("agent", PRODUCT, .1, "buy"))
    assert not result.success
    assert "not persisted" in unresolved[0]
    assert ledger.get_order("order-0").state == "UNKNOWN"
    assert store.get_or_create("agent").positions[PRODUCT] == .1
    assert executor.settle_dangling() == [("order-0", "restart-required")]
    assert not executor.execute(FillRequest("agent", PRODUCT, .1, "buy")).success
    assert store.get_or_create("agent").positions[PRODUCT] == .1
    assert len(client.calls) == 1


def test_short_add_counts_towards_position_cap(setup):
    executor, _, _, client, _ = setup
    request = FillRequest("agent", PRODUCT, .2, "sell")
    assert executor.execute(request).success
    assert executor.execute(request).success
    assert "position after" in executor.execute(request).message
    assert len(client.calls) == 2


@pytest.mark.parametrize(
    ("request_args", "message"),
    [
        (("agent", PRODUCT, .1, "buy", None, True), "cannot replay"),
        (("other", PRODUCT, .1, "buy"), "not enabled for 'other'"),
        (("agent", PRODUCT, .1, "withdraw"), "Action must be"),
        (("agent", "SOLUSDT", .1, "buy"), "not enabled for live perpetual"),
        (("agent", PRODUCT, 0, "buy"), "venue market lot"),
        (("agent", PRODUCT, .001, "buy"), "venue market lot"),
        (("agent", PRODUCT, .01, "buy"), "minimum notional"),
        (("agent", PRODUCT, 2, "buy"), "gross exposure cap"),
    ],
)
def test_denial_creates_no_venue_order(setup, request_args, message):
    executor, _, ledger, client, _ = setup
    assert message in executor.execute(FillRequest(*request_args)).message
    assert not client.calls
    assert not ledger.recent_orders()


def test_full_close_snaps_float_residue_to_exact_step(setup):
    executor, store, _, client, _ = setup
    assert executor.execute(FillRequest("agent", PRODUCT, .08, "buy")).success
    assert executor.execute(FillRequest("agent", PRODUCT, .01, "sell")).success
    assert executor.execute(FillRequest("agent", PRODUCT, .01, "sell")).success
    assert store.close_position("agent", PRODUCT).success
    assert PRODUCT not in store.get_or_create("agent").positions
    assert client.calls[-1][-2:] == (Decimal(".06"), True)


def test_stale_portfolio_stamp_cannot_open_exposure(setup):
    executor, store, _, client, _ = setup
    request = FillRequest("agent", PRODUCT, .1, "buy", allow_flip=False)
    assert executor.execute(request).success
    assert not executor.execute(request).success
    store.record_portfolio_read("agent", trade_count=1)
    assert executor.execute(request).success
    assert len(client.calls) == 2


@pytest.mark.parametrize("quantity,reason", [("0.2", "differs from ledger"), ("nan", "differs from ledger")])
def test_unexpected_venue_position_disarms(setup, monkeypatch, quantity, reason):
    executor, _, ledger, client, unresolved = setup
    monkeypatch.setattr(client, "position_risk", lambda _: [
        {"symbol": "SOLUSDT", "positionSide": "BOTH", "positionAmt": quantity},
    ])
    assert reason in executor.execute(FillRequest("agent", PRODUCT, .1, "buy")).message
    assert reason in unresolved[0]
    assert not client.calls and not ledger.recent_orders()


@pytest.mark.parametrize(
    ("available", "leverage", "reason", "disarmed"),
    [("1", 1, "Insufficient available", False), ("100", 2, "margin configuration", True),
     ("nan", 1, "margin is invalid", True)],
)
def test_venue_margin_checks_before_submission(setup, monkeypatch, available, leverage, reason, disarmed):
    executor, _, _, client, unresolved = setup
    monkeypatch.setattr(client, "futures_balances", lambda: [{"asset": "USDT", "availableBalance": available}])
    monkeypatch.setattr(client, "symbol_config", lambda _: [
        {"symbol": "SOLUSDT", "marginType": "CROSSED", "leverage": leverage},
    ])
    assert reason in executor.execute(FillRequest("agent", PRODUCT, .1, "buy")).message
    assert bool(unresolved) is disarmed
    assert not client.calls


def test_halt_set_during_venue_reads_blocks_post(setup, monkeypatch):
    executor, _, ledger, client, _ = setup
    halted = False

    def balance():
        nonlocal halted
        halted = True
        return [{"asset": "USDT", "availableBalance": "100"}]

    executor._gates = RiskGates(executor._gates.limits, is_halted=lambda: halted)
    monkeypatch.setattr(client, "futures_balances", balance)
    assert "HALTED" in executor.execute(FillRequest("agent", PRODUCT, .1, "buy")).message
    assert not client.calls and not ledger.recent_orders()


def test_dry_run_requires_no_signed_venue_requests(setup, monkeypatch):
    executor, store, ledger, client, unresolved = setup
    monkeypatch.setattr(executor, "_dry_run", True)
    assert executor.dry_run
    assert executor.client is client
    assert executor.rules == {PRODUCT: RULES}
    assert "DRY-RUN" in executor.execute(FillRequest("agent", PRODUCT, .1, "buy")).message
    assert ledger.get_order("order-0").state == "DRY_RUN"
    assert not client.calls and not unresolved
    assert PRODUCT not in store.get_or_create("agent").positions


@pytest.mark.parametrize(
    ("changes", "state"),
    [
        ({"status": "PARTIALLY_FILLED"}, "UNKNOWN"),
        ({"client_order_id": "external"}, "UNKNOWN"),
        ({"status": "FILLED", "executed_qty": .01}, "UNKNOWN"),
        ({"status": "FILLED", "fee_asset": "BNB"}, "UNKNOWN"),
        ({"status": "CANCELED", "executed_qty": 0}, "EXPIRED"),
    ],
)
def test_settlement_requires_terminal_matching_fill(setup, changes, state):
    executor, store, ledger, client, unresolved = setup
    client.response = lambda fill: replace(fill, **changes)
    assert not executor.execute(FillRequest("agent", PRODUCT, .1, "buy")).success
    assert ledger.get_order("order-0").state == state
    assert bool(unresolved) is (state == "UNKNOWN")
    assert PRODUCT not in store.get_or_create("agent").positions


def test_old_venue_timestamp_is_stale_even_with_fresh_local_receipt(setup):
    executor, store, _, client, _ = setup
    row = dict(store.price_book.get(PRODUCT), product_id=PRODUCT, time="2000-01-01T00:00:00Z")
    store.price_book.update(row)
    result = executor.execute(FillRequest("agent", PRODUCT, .1, "buy"))
    assert "freshness limit" in result.message
    assert not client.calls


def test_no_price_or_malformed_quote_prevents_orders(setup):
    executor, store, _, client, _ = setup
    store.price_book._book.pop(PRODUCT)
    request = FillRequest("agent", PRODUCT, .1, "buy")
    assert "No usable" in executor.execute(request).message
    for received_time in ("2026-01-01T09:00:00", "unknown"):
        store.price_book.update({
            "product_id": PRODUCT, "price": "100", "best_bid": "100", "best_ask": "100",
            "time": received_time,
        })
        assert "No usable" in executor.execute(request).message
    assert not client.calls


def test_quote_change_while_reading_venue_position_refuses_old_price(setup, monkeypatch):
    executor, store, ledger, client, _ = setup
    original = client.position_risk

    def position(symbol):
        store.price_book.update({
            "product_id": PRODUCT, "price": "101", "best_bid": "101", "best_ask": "101",
        })
        return original(symbol)

    monkeypatch.setattr(client, "position_risk", position)
    assert "quote changed" in executor.execute(FillRequest("agent", PRODUCT, .1, "buy")).message
    assert not ledger.recent_orders()
    assert not client.calls


def test_halt_after_journal_keeps_unsent_intent_terminal(setup, monkeypatch):
    executor, _, ledger, client, _ = setup
    executor._gates = RiskGates(
        executor._gates.limits, is_halted=lambda: bool(ledger.dangling_orders()),
    )
    result = executor.execute(FillRequest("agent", PRODUCT, .1, "buy"))
    assert "HALTED" in result.message
    assert ledger.get_order("order-0").state == "REJECTED"
    assert not client.calls


def test_cant_add_without_fresh_marks_but_can_reduce(setup):
    executor, store, _, client, _ = setup
    assert executor.execute(FillRequest("agent", PRODUCT, .1, "buy")).success
    account = store.get_or_create("agent")
    account.positions["BTCUSDT-PERP"] = .001
    assert "No fresh mark" in executor.execute(FillRequest("agent", PRODUCT, .1, "buy")).message
    assert store.close_position("agent", PRODUCT).success
    assert client.calls[-1][-1]


def test_per_order_market_lot_ceiling(setup, monkeypatch):
    executor, _, _, client, _ = setup
    monkeypatch.setitem(executor._rules, PRODUCT, replace(RULES, max_qty=Decimal(".2")))
    result = executor.execute(FillRequest("agent", PRODUCT, .21, "buy"))
    assert "maximum 0.2" in result.message
    assert not client.calls


def test_definite_post_error_closes_journal(setup):
    executor, _, ledger, client, unresolved = setup

    def reject(_):
        raise BinanceApiError(-2019, "Margin is insufficient")

    client.response = reject
    result = executor.execute(FillRequest("agent", PRODUCT, .1, "buy"))
    assert "Margin is insufficient" in result.message
    assert ledger.get_order("order-0").state == "REJECTED"
    assert not unresolved


def test_unexpected_post_exception_keeps_order_unresolved(setup):
    executor, _, ledger, client, unresolved = setup

    def malformed(_):
        raise ValueError("Malformed venue response")

    client.response = malformed
    result = executor.execute(FillRequest("agent", PRODUCT, .1, "buy"))
    assert "Malformed venue response" in result.message
    assert ledger.get_order("order-0").state == "UNKNOWN"
    assert unresolved


def test_recovery_skips_foreign_intents_and_keeps_lookup_errors_unknown(setup, monkeypatch):
    executor, _, ledger, client, _ = setup
    for ident, account, symbol in [
        ("foreign", "spot", "SOLUSDT"),
        ("ours", "margin", PRODUCT),
    ]:
        ledger.save_order(
            client_order_id=ident, agent_id="agent", account=account, symbol=symbol,
            side="buy", state="PENDING", qty_requested=.1, quote_qty_requested=10,
        )

    def fail(*_):
        raise OSError("connection lost")

    monkeypatch.setattr(client, "query_order", fail)
    assert executor.settle_dangling() == [("foreign", "foreign-intent"), ("ours", "lookup-failed")]
    assert len(ledger.dangling_orders()) == 2


@pytest.mark.parametrize("during_submission", [True, False])
def test_venue_ip_ban_triggers_fatal_halt(setup, monkeypatch, during_submission):
    executor, _, ledger, client, unresolved = setup
    fatals = []
    monkeypatch.setattr(executor, "_on_fatal", fatals.append)

    def fail(_):
        raise IpBanned(30)

    monkeypatch.setattr(client, "response" if during_submission else "position_risk", fail)
    assert "IP banned" in executor.execute(FillRequest("agent", PRODUCT, .1, "buy")).message
    assert len(fatals) == 1
    if during_submission:
        assert ledger.get_order("order-0").state == "REJECTED"
    else:
        assert unresolved and not ledger.recent_orders()


@pytest.mark.parametrize("mode", ["position", "margin", "fee"])
def test_account_configuration_drift_disarms_before_money_moves(setup, monkeypatch, mode):
    executor, _, _, client, unresolved = setup
    if mode == "position":
        monkeypatch.setattr(client, "position_risk", lambda _: [
            {"symbol": "SOLUSDT", "positionSide": "LONG", "positionAmt": "0"},
        ])
    elif mode == "margin":
        monkeypatch.setattr(client, "account_config", lambda: {
            "canTrade": True, "dualSidePosition": False, "multiAssetsMargin": True,
        })
    else:
        monkeypatch.setattr(client, "bnb_fee_burn", lambda: True)
    assert not executor.execute(FillRequest("agent", PRODUCT, .1, "buy")).success
    assert not client.calls and unresolved


@pytest.mark.parametrize("baseline", ["corrupt", None])
def test_order_cannot_initialise_unverified_session(setup, monkeypatch, baseline):
    executor, _, ledger, client, _ = setup
    monkeypatch.setattr(ledger, "get_meta", lambda _: baseline)
    assert "non-finite" in executor.execute(FillRequest("agent", PRODUCT, .1, "buy")).message
    assert ledger.get_meta("live_session_start_equity") is baseline
    assert len(client.calls) == 0


def test_cannot_round_unorderable_residue_up_to_close(setup):
    executor, store, _, client, _ = setup
    account = store.get_or_create("agent")
    account.positions[PRODUCT] = .005
    result = executor.execute(FillRequest("agent", PRODUCT, .01, "sell"))
    assert "below the venue quantity step" in result.message
    assert not client.calls


def test_bad_touch_prevents_order(setup):
    executor, store, _, client, _ = setup
    store.price_book.update({
        "product_id": PRODUCT, "price": "100", "best_bid": "100", "best_ask": "NaN",
    })
    assert "No usable" in executor.execute(FillRequest("agent", PRODUCT, .1, "buy")).message
    assert not client.calls


def test_missing_journal_after_venue_response_disarms(setup, monkeypatch):
    executor, _, ledger, client, unresolved = setup
    get = ledger.get_order
    monkeypatch.setattr(ledger, "get_order", lambda ident: None if client.calls else get(ident))
    assert "journal row is missing" in executor.execute(FillRequest("agent", PRODUCT, .1, "buy")).message
    assert unresolved


@pytest.mark.parametrize("failure", ["refused", "before_persistence", "after_persistence"])
def test_imperfect_booking_disarms_and_preserves_persistence_truth(setup, monkeypatch, failure):
    from arena.models import TradeResult

    executor, store, ledger, _, unresolved = setup
    apply_fill = store.apply_fill

    def apply(*args, **kwargs):
        if failure == "refused":
            return TradeResult(False, "Account denied fill")
        if failure == "after_persistence":
            assert apply_fill(*args, **kwargs).success
        raise OSError("broken output stream")

    monkeypatch.setattr(store, "apply_fill", apply)
    assert not executor.execute(FillRequest("agent", PRODUCT, .1, "buy")).success
    assert unresolved
    assert ledger.get_order("order-0").state == (
        "FILLED" if failure == "after_persistence" else "UNKNOWN"
    )


@pytest.mark.parametrize("side", ["buy", "sell"])
def test_add_cannot_grow_beyond_single_order_close_size(setup, monkeypatch, side):
    executor, store, _, client, _ = setup
    monkeypatch.setitem(executor._rules, PRODUCT, replace(RULES, max_qty=Decimal(".15")))
    assert executor.execute(FillRequest("agent", PRODUCT, .1, side)).success
    assert "maximum single-order close" in executor.execute(
        FillRequest("agent", PRODUCT, .06, side),
    ).message
    assert len(client.calls) == 1
    assert executor.execute(FillRequest("agent", PRODUCT, .05, side)).success
    assert store.close_position("agent", PRODUCT).success
    assert client.calls[-1][-2:] == (Decimal(".15"), True)
