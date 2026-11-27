"""Paper fills under real venue rules (Binance exchangeInfo).

The paper engine must rehearse the constraints the venue will enforce, otherwise a
run that looks profitable is one that live mode rejects: SOLUSDT floors to 0.001 and
refuses anything under 5 USDT, while the arena's legacy rule only allowed one decimal.
These tests pin the bridge between `arena.instruments` and `PaperExecutor`.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from arena.account_store import AccountStore
from arena.fees import FeeModel
from arena.instruments import SymbolRules, parse_exchange_info
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


# ── Dust: a flip must not create a position no order can close ───
#
# The measured run ended holding LINKUSDT-PERP short 0.1 — a $0.92 residue from a sell
# that overshot a long by one step. The venue floor is 20 USDT, so the only order that
# could flatten it was refused for being too small, and the residue sat for two hours.
# The scorecard read that as a guard that did not fire, which is right: nothing could
# ever close it. Two rules stop the arena growing those: a flip that would leave dust is
# booked as the close it really is, and an exit is never refused for being small.

LINK_PERP = "LINKUSDT-PERP"


@pytest.fixture
def perp_store():
    """A wallet with one perp symbol carrying the real LINK-PERP 20 USDT floor."""
    book = PriceBook()
    book.update(
        {
            "product_id": LINK_PERP,
            "price": "9.155",
            "best_bid": "9.150",
            "best_ask": "9.160",
        }
    )
    rules = parse_exchange_info(SOLUSDT_INFO)
    rules[LINK_PERP] = SymbolRules(
        symbol="LINKUSDT",
        base_asset="LINK",
        quote_asset="USDT",
        step_size=Decimal("0.01"),
        min_qty=Decimal("0.01"),
        min_notional=Decimal("20"),
        tick_size=Decimal("0.01"),
    )
    s = AccountStore(book, FeeModel(taker_bps=10, perp_taker_bps=5))
    s.get_or_create("a").cash = 100.0
    s.set_instrument_rules(rules)
    return s


def test_flip_that_would_leave_dust_is_booked_as_a_close(perp_store):
    """Selling 8.9 against a long of 8.8 is not "short 0.1", it is dust nobody can close."""
    assert perp_store.execute_trade("a", LINK_PERP, 8.8, "buy").success

    result = perp_store.execute_trade("a", LINK_PERP, 8.9, "sell")

    assert result.success, result.message
    account = perp_store.get_or_create("a")
    assert account.positions.get(LINK_PERP, 0.0) == pytest.approx(0.0)


def test_flip_that_clears_the_floor_still_flips(perp_store):
    """Clamping is for dust, not for intent: a short above 20 USDT is a real position."""
    assert perp_store.execute_trade("a", LINK_PERP, 8.8, "buy").success

    result = perp_store.execute_trade("a", LINK_PERP, 11.0, "sell")

    assert result.success, result.message
    # 2.2 LINK at the bid is $20.13, just over the venue's 20 USDT floor.
    assert perp_store.get_or_create("a").positions[LINK_PERP] == pytest.approx(-2.2)


def test_dust_below_the_floor_can_still_be_closed(perp_store):
    """An exit is never refused for being small — a size floor must not become the trap."""
    account = perp_store.get_or_create("a")
    account.positions[LINK_PERP] = 0.1
    account.cost_basis[LINK_PERP] = 0.1 * 9.16

    result = perp_store.execute_trade("a", LINK_PERP, 0.1, "sell")

    assert result.success, result.message
    assert account.positions.get(LINK_PERP, 0.0) == pytest.approx(0.0)


def test_opening_dust_is_still_refused(perp_store):
    """The exemption is for exits only: a 0.1 long is not a trade the venue accepts."""
    result = perp_store.execute_trade("a", LINK_PERP, 0.1, "buy")

    assert not result.success
    assert "minimum" in result.message.lower()
    assert perp_store.get_or_create("a").positions == {}


def test_a_partial_close_is_untouched_by_the_clamp(perp_store):
    """Only a flip is clamped; trimming a real position keeps the agent's quantity."""
    assert perp_store.execute_trade("a", LINK_PERP, 4.0, "buy").success

    result = perp_store.execute_trade("a", LINK_PERP, 3.0, "sell")

    assert result.success, result.message
    assert perp_store.get_or_create("a").positions[LINK_PERP] == pytest.approx(1.0)


def test_a_short_flip_that_would_leave_dust_is_clamped_too(perp_store):
    """The same rule on the other side: buying 8.9 against a short of 8.8 is a close."""
    assert perp_store.execute_trade("a", LINK_PERP, 8.8, "sell").success

    result = perp_store.execute_trade("a", LINK_PERP, 8.9, "buy")

    assert result.success, result.message
    assert perp_store.get_or_create("a").positions.get(LINK_PERP, 0.0) == pytest.approx(0.0)
