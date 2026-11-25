"""Paper fills under real venue rules (Binance exchangeInfo).

The paper engine must rehearse the constraints the venue will enforce, otherwise a
run that looks profitable is one that live mode rejects: SOLUSDT floors to 0.001 and
refuses anything under 5 USDT, while the arena's legacy rule only allowed one decimal.
These tests pin the bridge between `arena.instruments` and `PaperExecutor`.
"""

from __future__ import annotations

import pytest

from arena.account_store import AccountStore
from arena.fees import FeeModel
from arena.instruments import parse_exchange_info
from arena.price_book import PriceBook

SOL_ASK = 100.10
SOL_BID = 99.90

# Trimmed from a real GET /api/v3/exchangeInfo?symbol=SOLUSDT response.
SOLUSDT_INFO = {
    "symbols": [
        {
            "symbol": "SOLUSDT",
            "status": "TRADING",
            "baseAsset": "SOL",
            "quoteAsset": "USDT",
            "filters": [
                {
                    "filterType": "PRICE_FILTER",
                    "minPrice": "0.01000000",
                    "maxPrice": "10000.00000000",
                    "tickSize": "0.01000000",
                },
                {
                    "filterType": "LOT_SIZE",
                    "minQty": "0.00100000",
                    "maxQty": "90000.00000000",
                    "stepSize": "0.00100000",
                },
                {"filterType": "MARKET_LOT_SIZE", "minQty": "0.0", "stepSize": "0.00000000"},
                {
                    "filterType": "NOTIONAL",
                    "minNotional": "5.00000000",
                    "applyMinToMarket": True,
                },
            ],
        }
    ]
}


@pytest.fixture
def sol_price_book():
    pb = PriceBook()
    pb.update(
        {
            "product_id": "SOLUSDT",
            "price": "100.00",
            "best_bid": str(SOL_BID),
            "best_ask": str(SOL_ASK),
        }
    )
    # A product with no venue rules, to prove the legacy path survives next to it.
    pb.update(
        {
            "product_id": "BTC-USD",
            "price": "50000.00",
            "best_bid": "49990.00",
            "best_ask": "50010.00",
        }
    )
    return pb


@pytest.fixture
def store(sol_price_book):
    rules = parse_exchange_info(SOLUSDT_INFO)
    # Funds sized for a venue-aware wallet: 0.1 SOL still costs $10.
    s = AccountStore(sol_price_book, FeeModel(taker_bps=10))
    s.get_or_create("a").cash = 1000.0
    s.set_instrument_rules(rules)
    return s


# ── Step size ────────────────────────────────────────────────────


def test_quantity_floors_to_the_venue_step(store):
    # 0.0555 SOL ($5.56) is above the 5 USDT floor and floors to 0.055 on the 0.001 step.
    result = store.execute_trade("a", "SOLUSDT", 0.0555, "buy")
    assert result.success, result.message
    assert store.get_or_create("a").positions["SOLUSDT"] == pytest.approx(0.055)


def test_quantity_below_min_qty_is_rejected_with_an_actionable_message(store):
    result = store.execute_trade("a", "SOLUSDT", 0.0009, "buy")
    assert not result.success
    assert "0.001" in result.message  # names the step the agent must use
    assert store.get_or_create("a").positions == {}


def test_venue_steps_allow_sub_decimal_sol_quantities(store):
    """0.055 SOL is illegal under the legacy 1-decimal rule and legal on the venue."""
    result = store.execute_trade("a", "SOLUSDT", 0.055, "buy")
    assert result.success, result.message
    assert store.get_or_create("a").positions["SOLUSDT"] == pytest.approx(0.055)


# ── Minimum notional ─────────────────────────────────────────────


def test_order_below_min_notional_is_rejected(store):
    # 0.01 SOL = $1.00, under the venue's 5 USDT floor.
    result = store.execute_trade("a", "SOLUSDT", 0.01, "buy")
    assert not result.success
    assert "5" in result.message and "minimum" in result.message.lower()


def test_order_at_min_notional_is_allowed(store):
    # 0.05 SOL = $5.01 at the ask, just over the floor.
    result = store.execute_trade("a", "SOLUSDT", 0.05, "buy")
    assert result.success, result.message


# ── Closing a position exactly ───────────────────────────────────


def test_full_sell_of_a_stepped_position_leaves_no_dust(store):
    assert store.execute_trade("a", "SOLUSDT", 0.055, "buy").success
    result = store.execute_trade("a", "SOLUSDT", 0.055, "sell")
    assert result.success, result.message
    acct = store.get_or_create("a")
    assert acct.positions == {}
    assert acct.cost_basis == {}


def test_selling_more_than_held_is_still_rejected(store):
    assert store.execute_trade("a", "SOLUSDT", 0.055, "buy").success
    result = store.execute_trade("a", "SOLUSDT", 0.056, "sell")
    assert not result.success
    assert "Insufficient holdings" in result.message


# ── Fallback for symbols without venue rules ─────────────────────


def test_symbol_without_rules_keeps_the_legacy_decimal_rule(store):
    result = store.execute_trade("a", "BTC-USD", 0.12, "buy")
    assert not result.success
    assert "decimal place" in result.message


def test_symbol_without_rules_prices_normally(store):
    # 0.1 BTC reaches the cash check, which proves it cleared the legacy validation.
    result = store.execute_trade("a", "BTC-USD", 0.1, "buy")
    assert not result.success
    assert "Insufficient cash" in result.message
