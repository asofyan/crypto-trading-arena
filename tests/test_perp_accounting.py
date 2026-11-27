"""Perpetual-futures paper accounting: signed net positions, 1:1 margin, perp fees.

A perp product is identified by the ``-PERP`` suffix (see
``docs/plans/261126-0810-scalper-perp/design.md``). The properties exercised here are
the ones the money path must never lose: opening either side, closing net of both
fees, flipping through zero, the average-cost basis, the 1:1 notional cap, and the
per-product fee rate.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from arena.account_store import AccountStore
from arena.fees import DEFAULT_PERP_TAKER_BPS, FeeModel
from arena.instruments import SymbolRules, is_perp_product, perp_product_id, venue_symbol
from arena.price_book import PriceBook

PERP = "SOLUSDT-PERP"
SPOT = "SOL-USD"
TAKER = 10
PERP_TAKER = 5


def perp_fee(notional: float, product_id: str = PERP) -> float:
    """The fee the engine must charge, written out independently of the code under test."""
    bps = PERP_TAKER if is_perp_product(product_id) else TAKER
    return notional * bps / 10_000.0


def make_env(*, cash: float = 100.0, mark: float = 50.0) -> tuple[AccountStore, PriceBook]:
    """A store with one marked perp product and a starting wallet."""
    book = PriceBook()
    book.update(
        {
            "product_id": PERP,
            "price": str(mark),
            "best_bid": str(mark),
            "best_ask": str(mark),
        }
    )
    book.update(
        {
            "product_id": SPOT,
            "price": "50.0",
            "best_bid": "50.0",
            "best_ask": "50.0",
        }
    )
    store = AccountStore(book, FeeModel(taker_bps=TAKER, perp_taker_bps=PERP_TAKER))
    store.get_or_create("a").cash = cash
    return store, book


def mark(book: PriceBook, product_id: str, price: float) -> None:
    book.update(
        {
            "product_id": product_id,
            "price": str(price),
            "best_bid": str(price),
            "best_ask": str(price),
        }
    )


def fill(store: AccountStore, action: str, qty: float, price: float, product_id=PERP):
    """Book one perp fill at an explicit price, charging the perp taker rate."""
    return store.apply_fill(
        "a", action, product_id, qty, price, perp_fee(qty * price, product_id),
        fee_bps=PERP_TAKER if is_perp_product(product_id) else TAKER,
    )


def test_perp_fee_defaults_to_the_binance_futures_taker_rate():
    """5 bps is what Binance USDⓈ-M VIP 0 actually charges; the user asked for real fees."""
    assert DEFAULT_PERP_TAKER_BPS == 5
    model = FeeModel()
    assert model.fee(10_000.0, perp=True) == pytest.approx(5.0)
    assert model.fee(10_000.0, perp=False) == pytest.approx(60.0)
    assert "5 bps" in model.disclosure_prompt(perp=True)
    assert "5 bps" not in model.disclosure_prompt(perp=False)


# ── product identity ────────────────────────────────────────────


def test_perp_product_ids_are_suffixed_and_map_back_to_the_venue_symbol():
    assert perp_product_id("solusdt") == PERP
    assert perp_product_id(" SOLUSDT ") == PERP
    assert is_perp_product(PERP) is True
    assert is_perp_product("SOLUSDT") is False
    assert venue_symbol(PERP) == "SOLUSDT"
    assert venue_symbol("SOLUSDT") == "SOLUSDT"


# ── opening ─────────────────────────────────────────────────────


def test_open_long_books_cash_basis_and_entry():
    store, _ = make_env()
    result = fill(store, "buy", 1.0, 50.0)

    assert result.success, result.message
    account = store.get_or_create("a")
    assert account.positions[PERP] == pytest.approx(1.0)
    assert account.cost_basis[PERP] == pytest.approx(50.0)
    assert account.cash == pytest.approx(50.0 - perp_fee(50.0))
    assert account.realized_pnl == pytest.approx(-perp_fee(50.0))
    assert account.total_fees_paid == pytest.approx(perp_fee(50.0))


def test_open_short_credits_the_notional_and_keeps_a_signed_basis():
    """A short is not a spot sell: proceeds land in cash and the basis is negative."""
    store, _ = make_env()
    result = fill(store, "sell", 1.0, 50.0)

    assert result.success, result.message
    account = store.get_or_create("a")
    assert account.positions[PERP] == pytest.approx(-1.0)
    assert account.cost_basis[PERP] == pytest.approx(-50.0)
    assert account.avg_cost_per_unit(PERP) == pytest.approx(50.0)
    assert account.cash == pytest.approx(150.0 - perp_fee(50.0))


def test_short_marks_to_market_as_an_inverse_position():
    """Equity must fall when the marked price rises against a short."""
    store, book = make_env()
    fill(store, "sell", 1.0, 50.0)
    account = store.get_or_create("a")

    mark(book, PERP, 40.0)
    assert account.portfolio_value(book) == pytest.approx(100.0 + 10.0 - perp_fee(50.0))

    mark(book, PERP, 60.0)
    assert account.portfolio_value(book) == pytest.approx(100.0 - 10.0 - perp_fee(50.0))


# ── closing ─────────────────────────────────────────────────────


def test_close_long_realizes_net_of_both_fees():
    store, _ = make_env()
    fill(store, "buy", 1.0, 50.0)
    result = fill(store, "sell", 1.0, 60.0)

    assert result.success, result.message
    account = store.get_or_create("a")
    assert account.positions == {}
    assert account.cost_basis == {}
    assert account.realized_pnl == pytest.approx(10.0 - perp_fee(50.0) - perp_fee(60.0))
    assert account.cash == pytest.approx(
        100.0 - 50.0 - perp_fee(50.0) + 60.0 - perp_fee(60.0)
    )


def test_close_short_realizes_the_inverse_move_net_of_both_fees():
    store, _ = make_env()
    fill(store, "sell", 1.0, 50.0)
    result = fill(store, "buy", 1.0, 40.0)

    assert result.success, result.message
    account = store.get_or_create("a")
    assert account.positions == {}
    assert account.realized_pnl == pytest.approx(10.0 - perp_fee(50.0) - perp_fee(40.0))
    assert account.cash == pytest.approx(
        100.0 + 50.0 - perp_fee(50.0) - 40.0 - perp_fee(40.0)
    )


# ── adding and reducing ─────────────────────────────────────────


def test_adding_to_a_long_uses_the_weighted_average_entry():
    store, _ = make_env()
    fill(store, "buy", 1.0, 50.0)
    # 1.5 x 54 = 81 gross, still under $100 equity: the 1:1 cap allows the add.
    assert fill(store, "buy", 0.5, 54.0).success

    account = store.get_or_create("a")
    assert account.positions[PERP] == pytest.approx(1.5)
    assert account.avg_cost_per_unit(PERP) == pytest.approx((50.0 + 27.0) / 1.5)
    assert account.cost_basis[PERP] == pytest.approx(77.0)


def test_adding_to_a_short_uses_the_weighted_average_entry():
    store, _ = make_env()
    fill(store, "sell", 1.0, 50.0)
    fill(store, "sell", 0.5, 46.0)

    account = store.get_or_create("a")
    assert account.positions[PERP] == pytest.approx(-1.5)
    assert account.avg_cost_per_unit(PERP) == pytest.approx((50.0 + 23.0) / 1.5)
    assert account.cost_basis[PERP] == pytest.approx(-73.0)


def test_partial_reduce_leaves_the_average_entry_untouched():
    store, _ = make_env()
    fill(store, "buy", 1.5, 50.0)  # 75 notional, under the 1:1 cap
    fill(store, "sell", 0.5, 70.0)

    account = store.get_or_create("a")
    assert account.positions[PERP] == pytest.approx(1.0)
    assert account.avg_cost_per_unit(PERP) == pytest.approx(50.0)
    assert account.realized_pnl == pytest.approx(
        0.5 * 20.0 - perp_fee(75.0) - perp_fee(35.0)
    )


# ── flipping ────────────────────────────────────────────────────


def test_flip_long_to_short_realizes_the_close_and_rebases_the_new_side():
    store, _ = make_env()
    fill(store, "buy", 1.0, 50.0)
    result = fill(store, "sell", 2.0, 60.0)

    assert result.success, result.message
    account = store.get_or_create("a")
    assert account.positions[PERP] == pytest.approx(-1.0)
    assert account.avg_cost_per_unit(PERP) == pytest.approx(60.0)
    assert account.cost_basis[PERP] == pytest.approx(-60.0)
    # Only the closing unit realizes P&L; the new short unit is an open position.
    assert account.realized_pnl == pytest.approx(
        10.0 - perp_fee(50.0) - perp_fee(120.0)
    )
    assert account.cash == pytest.approx(
        100.0 - 50.0 - perp_fee(50.0) + 120.0 - perp_fee(120.0)
    )


def test_flip_short_to_long_realizes_the_inverse_move():
    store, _ = make_env()
    fill(store, "sell", 1.0, 50.0)
    fill(store, "buy", 2.0, 40.0)

    account = store.get_or_create("a")
    assert account.positions[PERP] == pytest.approx(1.0)
    assert account.avg_cost_per_unit(PERP) == pytest.approx(40.0)
    assert account.realized_pnl == pytest.approx(10.0 - perp_fee(50.0) - perp_fee(80.0))


# ── ledger consistency ──────────────────────────────────────────


def test_equity_always_equals_cash_plus_marked_positions():
    """No P&L may hide in the cost basis: the wallet and the marks tell the whole story."""
    store, book = make_env()
    fill(store, "buy", 1.0, 50.0)
    mark(book, PERP, 55.0)
    fill(store, "sell", 1.5, 55.0)  # flip to short 0.5
    mark(book, PERP, 45.0)
    fill(store, "buy", 0.2, 45.0)  # reduce the short

    account = store.get_or_create("a")
    expected = account.cash + sum(
        qty * float(book.get(pid)["price"]) for pid, qty in account.positions.items()
    )
    assert account.portfolio_value(book) == pytest.approx(expected)


def test_float_dust_is_treated_as_flat():
    store, _ = make_env()
    fill(store, "buy", 0.1, 50.0)
    fill(store, "buy", 0.2, 50.0)
    fill(store, "sell", 0.3, 50.0)

    account = store.get_or_create("a")
    assert account.positions == {}
    assert account.cost_basis == {}
    assert account.avg_entry_ts == {}


# ── leverage 1:1 ────────────────────────────────────────────────


def test_leverage_1x_rejects_opening_more_notional_than_equity():
    store, _ = make_env(cash=100.0)
    result = fill(store, "buy", 2.0, 50.0)  # 100 notional + fee > 100 equity

    assert result.success is False
    assert "leverage" in result.message.lower()
    assert store.get_or_create("a").positions == {}
    assert store.get_or_create("a").cash == pytest.approx(100.0)


def test_leverage_1x_allows_the_same_size_on_either_side():
    store, _ = make_env(cash=100.0)
    # 1.9 x 50 = 95 notional, fee 0.0475 -> 95.0475 <= 99.95 equity
    assert fill(store, "buy", 1.9, 50.0).success

    short_store, _ = make_env(cash=100.0)
    assert fill(short_store, "sell", 1.9, 50.0).success


def test_leverage_1x_counts_both_sides_of_a_hedged_book():
    """Gross notional is |long| + |short|, so a hedge cannot smuggle in 2x."""
    store, _ = make_env(cash=100.0)
    assert fill(store, "buy", 1.0, 50.0).success  # 50 gross

    result = fill(store, "sell", 2.0, 50.0)  # flip: 50 long -> 50 short = 50 gross
    assert result.success  # not an increase in gross

    # Adding a fresh opposite position on a second product would double the gross.
    store.price_book.update(
        {"product_id": "XRPUSDT-PERP", "price": "50.0", "best_bid": "50.0", "best_ask": "50.0"}
    )
    result = store.apply_fill(
        "a", "sell", "XRPUSDT-PERP", 1.0, 50.0, perp_fee(50.0), fee_bps=PERP_TAKER
    )
    assert result.success is False
    assert store.get_or_create("a").positions.get("XRPUSDT-PERP") is None


def test_leverage_1x_never_blocks_a_close_even_when_equity_is_gone():
    """An underwater short must always be closable: the gate only guards new exposure."""
    store, book = make_env(cash=100.0)
    assert fill(store, "sell", 1.9, 50.0).success
    mark(book, PERP, 120.0)  # 1.9*120 = 228 > equity -> deeply underwater

    result = fill(store, "buy", 1.9, 120.0)
    assert result.success, result.message
    assert store.get_or_create("a").positions == {}


# ── fees and the spot path ─────────────────────────────────────


def test_perp_products_are_charged_the_perp_rate_through_the_executor():
    """The paper executor must pick the rate from the product, not from one global config."""
    store, book = make_env(cash=100.0)
    rules = SymbolRules(
        symbol=PERP,
        base_asset="SOL",
        quote_asset="USDT",
        step_size=Decimal("0.01"),
        min_qty=Decimal("0.01"),
        min_notional=Decimal("5"),
        tick_size=Decimal("0.01"),
    )
    store.set_instrument_rules({PERP: rules})

    result = store.execute_trade("a", PERP, 0.5, "sell")

    assert result.success, result.message
    account = store.get_or_create("a")
    assert account.total_fees_paid == pytest.approx(perp_fee(0.5 * 50.0))


def test_spot_products_keep_the_long_only_rule_and_the_spot_rate():
    store, _ = make_env(cash=100.0)
    result = store.apply_fill("a", "sell", SPOT, 1.0, 50.0, perp_fee(50.0, SPOT), fee_bps=TAKER)

    assert result.success is False
    assert "insufficient holdings" in result.message.lower()


# ── persistence ─────────────────────────────────────────────────


def test_signed_positions_survive_a_ledger_restart(make_store_with_ledger, tmp_path):
    """A short must rehydrate as a short, with its negative basis intact."""
    db = tmp_path / "perp.db"
    store = make_store_with_ledger(taker_bps=0, path=db)
    store.get_or_create("a").cash = 100.0
    store.apply_fill("a", "sell", PERP, 1.0, 50.0, perp_fee(50.0), fee_bps=PERP_TAKER)
    store.apply_fill("a", "buy", PERP, 0.25, 40.0, perp_fee(10.0), fee_bps=PERP_TAKER)

    reloaded = make_store_with_ledger(taker_bps=0, path=db)
    account = reloaded.get_or_create("a")
    assert account.positions[PERP] == pytest.approx(-0.75)
    assert account.cost_basis[PERP] == pytest.approx(-37.5)
    assert account.realized_pnl == pytest.approx(2.5 - perp_fee(50.0) - perp_fee(10.0))


# ── cross-market guards ─────────────────────────────────────────


def test_short_proceeds_cannot_fund_an_unlevered_spot_buy():
    """A perp short credits cash; without the gate the spot path would reach ~1.95x."""
    store, _ = make_env(cash=100.0)
    assert fill(store, "sell", 1.0, 50.0).success  # cash 149.97, gross 50

    # 2.9 SOL-USD @ 50 = 145 notional: affordable in cash, but gross would be 195.
    result = store.apply_fill(
        "a", "buy", SPOT, 2.9, 50.0, perp_fee(145.0, SPOT), fee_bps=TAKER
    )

    assert result.success is False
    assert "leverage" in result.message.lower()
    assert SPOT not in store.get_or_create("a").positions


def test_an_agent_is_confined_to_the_market_its_strategy_was_written_for():
    """A spot prompt has no way to express a short, so the executor refuses one for it."""
    store, _ = make_env(cash=100.0)
    store.set_agent_markets({"spotbot": ("spot",), "perps": ("perp",)})

    spot_short = store.execute_trade("spotbot", PERP, 0.5, "sell")
    assert spot_short.success is False
    assert "may only trade spot" in spot_short.message

    perp_buy = store.execute_trade("perps", SPOT, 0.5, "buy")
    assert perp_buy.success is False
    assert "may only trade perp" in perp_buy.message

    # An agent with no declared scope keeps the historical behaviour.
    assert store.execute_trade("anyone", SPOT, 0.5, "buy").success


def test_equity_values_an_unpriced_position_at_entry_instead_of_dropping_it():
    """Dropping a short's liability would report equity that does not exist."""
    store, _ = make_env(cash=100.0)
    fill(store, "sell", 1.0, 50.0)
    account = store.get_or_create("a")

    # No quotes at all: each position falls back to its own average entry.
    assert account.portfolio_value(PriceBook()) == pytest.approx(
        account.cash - 50.0
    )


def test_a_zero_basis_position_does_not_realize_its_notional_as_profit():
    """A corrupt legacy row must not turn a flat close into a windfall."""
    store, _ = make_env(cash=100.0)
    account = store.get_or_create("a")
    account.positions[PERP] = -1.0
    account.cost_basis[PERP] = 0.0

    result = fill(store, "buy", 1.0, 50.0)

    assert result.success, result.message
    assert account.realized_pnl == pytest.approx(-perp_fee(50.0))


def test_the_leverage_gate_refuses_a_stale_mark_on_another_position():
    """A quote older than a minute cannot prove the book is not levered."""
    store, book = make_env(cash=200.0)
    assert fill(store, "buy", 1.0, 50.0).success
    book.update(
        {"product_id": "XRPUSDT-PERP", "price": "1.0", "best_bid": "1.0", "best_ask": "1.0"}
    )
    book._book[PERP]["_received_at"] -= 120  # 2 minutes of silence

    result = store.apply_fill(
        "a", "sell", "XRPUSDT-PERP", 10.0, 1.0, perp_fee(10.0), fee_bps=PERP_TAKER
    )

    assert result.success is False
    assert "mark" in result.message.lower()


def test_a_replay_books_a_fill_the_cap_would_reject():
    """The monitor replays fills the arena already accepted; re-gating would drop them."""
    store, _ = make_env(cash=100.0)
    assert fill(store, "buy", 2.0, 50.0).success is False  # 100 notional + fee > equity

    result = store.apply_fill(
        "a", "buy", PERP, 2.0, 50.0, perp_fee(100.0),
        fee_bps=PERP_TAKER, enforce_margin=False,
    )

    assert result.success, result.message
    assert store.get_or_create("a").positions[PERP] == pytest.approx(2.0)


def test_a_spot_only_account_needs_no_marks_to_buy():
    """Cash funding already implies 1:1, so the gate must not demand quotes for spot.

    A spot deployment whose feed drops for one held symbol must keep trading with the
    cash it has; only a short-credited book needs the mark-to-market proof.
    """
    book = PriceBook()  # deliberately empty: no quote for anything
    store = AccountStore(book, FeeModel(taker_bps=TAKER, perp_taker_bps=PERP_TAKER))
    store.get_or_create("a").cash = 100.0

    result = store.apply_fill("a", "buy", SPOT, 1.0, 50.0, perp_fee(50.0, SPOT), fee_bps=TAKER)

    assert result.success, result.message
    assert store.get_or_create("a").positions[SPOT] == pytest.approx(1.0)


# ── an agent order may not cross zero ───────────────────────────
#
# The model learned its size from a portfolio it read earlier. Once a fill lands, that
# number is a claim about the past: selling it turns an exit into an opening order on the
# other side (2026-11-27 09:27 — see tests/test_tools.py::test_a_stale_size_cannot_open_a_short).
# The store keeps flipping for callers that read the book themselves (close_position, the
# hold watchdog, replay); only the agent tool path is refused.


def test_an_agent_order_may_not_cross_zero():
    store, _ = make_env()
    assert store.execute_trade("a", PERP, 0.5, "buy", allow_flip=False).success
    store.record_portfolio_read("a", trade_count=store.get_or_create("a").trade_count)

    refusal = store.execute_trade("a", PERP, 0.8, "sell", allow_flip=False)

    assert not refusal.success
    assert "crosses zero" in refusal.message.lower()
    assert "close_position" in refusal.message
    assert store.get_or_create("a").positions[PERP] == pytest.approx(0.5)
    # The refusal names the live size, so the model can correct itself in the same turn.
    assert "0.5" in refusal.message


def test_a_trusted_caller_may_still_flip():
    """The ban is agent policy, not a property of the book: the store contract is unchanged."""
    store, _ = make_env()
    assert store.execute_trade("a", PERP, 0.5, "buy").success

    assert store.execute_trade("a", PERP, 0.8, "sell").success

    assert store.get_or_create("a").positions[PERP] == pytest.approx(-0.3)


def test_close_position_still_closes_under_the_agent_gate():
    """An exit at the live size never crosses zero, so the gate must not touch it."""
    store, _ = make_env()
    store.execute_trade("a", PERP, 0.5, "buy", allow_flip=False)

    result = store.close_position("a", PERP)

    assert result.success, result.message
    assert store.get_or_create("a").positions.get(PERP, 0.0) == pytest.approx(0.0)


# ── a perp order needs a book the agent has seen ─────────────────
#
# The flip gate above compares the order against the position that is still there. It
# cannot see the harder case: the position went to zero in between, so "sell the size I
# read" and "open that size short" are the same order. Only the read count separates them
# (AccountStore._stale_read_rejection), and it is why a stale exit cannot open a position.


def test_a_perp_order_needs_a_book_seen_since_the_last_fill():
    store, _ = make_env()
    assert store.execute_trade("a", PERP, 0.5, "buy", allow_flip=False).success

    stale = store.execute_trade("a", PERP, 0.2, "buy", allow_flip=False)

    assert not stale.success
    assert "get_portfolio" in stale.message
    assert store.get_or_create("a").positions[PERP] == pytest.approx(0.5)

    store.record_portfolio_read("a", trade_count=store.get_or_create("a").trade_count)
    assert store.execute_trade("a", PERP, 0.2, "buy", allow_flip=False).success


def test_a_stale_size_cannot_open_a_short_on_a_flat_book():
    """The hole a position-shaped gate cannot see: flat is not a smaller long."""
    store, _ = make_env()
    assert store.execute_trade("a", PERP, 0.5, "buy", allow_flip=False).success
    assert store.close_position("a", PERP).success
    assert store.get_or_create("a").positions.get(PERP, 0.0) == pytest.approx(0.0)

    stale = store.execute_trade("a", PERP, 0.5, "sell", allow_flip=False)

    assert not stale.success
    assert "stale" in stale.message.lower()
    assert store.get_or_create("a").positions.get(PERP, 0.0) == pytest.approx(0.0)


def test_an_exit_is_exempt_from_the_read_count():
    """A model-sized reduce can only ever end smaller, so it never waits for a re-read."""
    store, _ = make_env()
    assert store.execute_trade("a", PERP, 0.5, "buy", allow_flip=False).success

    result = store.execute_trade("a", PERP, 0.2, "sell", allow_flip=False)

    assert result.success, result.message
    assert store.get_or_create("a").positions[PERP] == pytest.approx(0.3)


def test_spot_is_not_gated_by_the_read_count():
    """A stale size on spot cannot open anything: a sell past the holding is refused, and a
    buy is cash-funded. The probe is an *adding* buy, which is what the perp gate refuses."""
    store, _ = make_env()

    assert store.execute_trade("a", SPOT, 0.5, "buy", allow_flip=False).success
    # Same shape as the perp refusal above (an add, no re-read) — allowed here on purpose.
    assert store.execute_trade("a", SPOT, 0.2, "buy", allow_flip=False).success
    assert store.get_or_create("a").positions[SPOT] == pytest.approx(0.7)


def test_the_flip_gate_judges_the_clamped_size():
    """A dust flip is booked as a close, so the gate must not refuse it (F1 from review)."""
    store, book = make_env()
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
    assert store.execute_trade("a", PERP, 1.0, "buy", allow_flip=False).success
    store.record_portfolio_read("a", trade_count=store.get_or_create("a").trade_count)

    # 1.05 booked against a long of 1.0 leaves a 0.05 opening leg ($2.50) — under the $20
    # minimum, so the executor books it as the close it mostly was.
    result = store.execute_trade("a", PERP, 1.05, "sell", allow_flip=False)

    assert result.success, result.message
    assert store.get_or_create("a").positions.get(PERP, 0.0) == pytest.approx(0.0)


def test_the_flip_refusal_is_asked_before_churn():
    """A spent fill budget must not answer a question the flip gate can answer better."""
    from arena.trade_policy import ChurnLimits

    store, _ = make_env()
    store.set_churn_limits(ChurnLimits(max_fills_per_hour=0))
    store.apply_fill(
        "a", "buy", PERP, 0.5, 50.0, perp_fee(25.0), fee_bps=PERP_TAKER
    )
    store.record_portfolio_read("a", trade_count=store.get_or_create("a").trade_count)  # isolate the flip gate from the read-count gate

    refusal = store.execute_trade("a", PERP, 0.8, "sell", allow_flip=False)

    assert not refusal.success
    assert "crosses zero" in refusal.message.lower()
    assert "fills already" not in refusal.message  # not the churn message


def test_close_position_can_be_refused_by_the_exit_floor():
    """Documents the dead end the prompt must not promise its way out of: inside the band,
    neither a reduce nor a close is allowed, so a reversal is impossible until the band
    resolves."""
    from arena.trade_policy import ExitFloor

    store, book = make_env()
    assert store.execute_trade("a", PERP, 0.5, "buy").success
    store.set_exit_floor(ExitFloor(min_profit_bps=100, stop_bps=50, max_hold_seconds=3600))
    mark(book, PERP, 50.2)  # +40 bps: inside the band

    refusal = store.close_position("a", PERP)

    assert not refusal.success
    assert "exit floor" in refusal.message.lower()
    assert store.get_or_create("a").positions[PERP] == pytest.approx(0.5)
