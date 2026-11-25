"""Unit tests for arena.instruments.

The venue numbers here were read from GET /api/v3/exchangeInfo on 2026-11-25, so the
expectations encode what Binance Spot actually enforces. Two of them are easy to get
wrong: SOLUSDT floors to 0.001 while its MARKET_LOT_SIZE stepSize reads 0.00000000, and
0.29 is not representable as a float — a parser that passes the float straight into
Decimal() silently floors it to 0.28.
"""

from __future__ import annotations

import json
import logging
from dataclasses import replace
from decimal import Decimal

import pytest

import httpx

from arena.instruments import (
    SymbolRules,
    fetch_exchange_info,
    load_cache,
    parse_exchange_info,
    refresh_cache,
    save_cache,
)

# ── Payload builders (shapes copied from the live exchangeInfo response) ─────


def _lot_size(step: str, min_qty: str) -> dict:
    return {
        "filterType": "LOT_SIZE",
        "minQty": min_qty,
        "maxQty": "90000.00000000",
        "stepSize": step,
    }


def _price_filter(tick: str) -> dict:
    return {
        "filterType": "PRICE_FILTER",
        "minPrice": "0.00010000",
        "maxPrice": "10000.00000000",
        "tickSize": tick,
    }


def _notional(min_notional: str, apply_min_to_market: bool = True) -> dict:
    return {
        "filterType": "NOTIONAL",
        "minNotional": min_notional,
        "applyMinToMarket": apply_min_to_market,
        "maxNotional": "9000000.00000000",
        "applyMaxToMarket": False,
    }


def _symbol(
    symbol: str,
    base: str,
    quote: str,
    filters: list[dict],
    status: str = "TRADING",
) -> dict:
    return {
        "symbol": symbol,
        "status": status,
        "baseAsset": base,
        "quoteAsset": quote,
        "filters": filters,
    }


SOL_ENTRY = _symbol(
    "SOLUSDT",
    "SOL",
    "USDT",
    [
        _price_filter("0.01000000"),
        _lot_size("0.00100000", "0.00100000"),
        {"filterType": "ICEBERG_PARTS", "limit": 100},
        # Binance reports a zero step for market orders on most spot symbols; if the
        # parser trusted it every order would floor to nothing.
        {
            "filterType": "MARKET_LOT_SIZE",
            "minQty": "0.00000000",
            "maxQty": "31592.92779497",
            "stepSize": "0.00000000",
        },
        _notional("5.00000000"),
    ],
)

XRP_ENTRY = _symbol(
    "XRPUSDT",
    "XRP",
    "USDT",
    [
        _price_filter("0.00010000"),
        _lot_size("0.10000000", "0.10000000"),
        {
            "filterType": "MARKET_LOT_SIZE",
            "minQty": "0.00000000",
            "maxQty": "1969372.56025104",
            "stepSize": "0.00000000",
        },
        _notional("5.00000000"),
    ],
)

LINK_ENTRY = _symbol(
    "LINKUSDT",
    "LINK",
    "USDT",
    [
        _price_filter("0.00100000"),
        _lot_size("0.01000000", "0.01000000"),
        _notional("5.00000000"),
    ],
)

REAL_PAYLOAD = {"symbols": [SOL_ENTRY, XRP_ENTRY, LINK_ENTRY]}


@pytest.fixture
def sol() -> SymbolRules:
    return parse_exchange_info({"symbols": [SOL_ENTRY]})["SOLUSDT"]


@pytest.fixture
def xrp() -> SymbolRules:
    return parse_exchange_info({"symbols": [XRP_ENTRY]})["XRPUSDT"]


@pytest.fixture
def link() -> SymbolRules:
    return parse_exchange_info({"symbols": [LINK_ENTRY]})["LINKUSDT"]


# ── quantize: floor only, Decimal only ──────────────────────


def test_quantize_floors_to_step_size(sol):
    assert sol.quantize(0.0015) == Decimal("0.001")
    assert sol.quantize(0.0019) == Decimal("0.001")  # never rounds up
    assert sol.quantize(Decimal("0.0029")) == Decimal("0.002")
    assert isinstance(sol.quantize(0.0015), Decimal)


def test_quantize_keeps_decimal_value_a_float_would_lose(sol):
    # Decimal(0.29) is 0.28999999999999998..., which floors to 0.28 — a real bug that
    # Decimal(str(x)) avoids. step 0.01 so the boundary is the one being crossed.
    rules = replace(sol, step_size=Decimal("0.01"))
    assert rules.quantize(0.29) == Decimal("0.29")
    assert rules.quantize(0.1 + 0.2) == Decimal("0.3")


def test_quantize_below_min_qty_returns_none(sol):
    assert sol.quantize(0.0009) is None
    assert sol.quantize(Decimal("0.00000001")) is None


def test_quantize_nonzero_but_under_min_qty_returns_none(sol):
    # 0.005 survives the floor but is still unorderable, so it must not be booked.
    rules = replace(sol, min_qty=Decimal("0.01"))
    assert rules.quantize(0.005) is None


def test_quantize_zero_or_negative_returns_none(sol):
    assert sol.quantize(0) is None
    assert sol.quantize(-0.5) is None


def test_quantize_non_finite_returns_none(sol):
    assert sol.quantize(float("nan")) is None
    assert sol.quantize(float("inf")) is None


def test_quantize_exactly_min_qty_is_tradable(sol):
    assert sol.quantize(Decimal("0.001")) == Decimal("0.001")


def test_quantize_xrp_floors_0_35_to_0_3(xrp):
    assert xrp.quantize(0.35) == Decimal("0.3")
    assert xrp.quantize(0.1) == Decimal("0.1")


# ── notional_ok ─────────────────────────────────────────────


def test_notional_rejects_below_min(sol):
    # The phase's $2.50 buy: 0.1 SOL at $25 is under the 5 USDT minimum.
    assert sol.notional_ok(Decimal("0.1"), 25.0) is False


def test_notional_accepts_exactly_min(sol):
    assert sol.notional_ok(Decimal("0.05"), 100.0) is True  # 5.00 USDT
    assert sol.notional_ok(Decimal("0.05"), Decimal("100.01")) is True


def test_notional_enforced_on_market_when_venue_says_so(sol):
    assert sol.min_notional_applies_to_market is True
    assert sol.notional_ok(Decimal("0.001"), 100.0) is False


def test_notional_market_order_exempt_when_flag_is_false(sol):
    rules = replace(sol, min_notional_applies_to_market=False)
    assert rules.notional_ok(Decimal("0.001"), 100.0) is True


def test_notional_without_minimum_is_always_ok(sol):
    rules = replace(sol, min_notional=Decimal("0"))
    assert rules.notional_ok(Decimal("0.0001"), 1.0) is True


def test_notional_rejects_non_finite_price(sol):
    assert sol.notional_ok(Decimal("1"), float("nan")) is False


# ── parse_exchange_info ─────────────────────────────────────


def test_parse_reads_the_real_venue_numbers():
    rules = parse_exchange_info(REAL_PAYLOAD)
    assert set(rules) == {"SOLUSDT", "XRPUSDT", "LINKUSDT"}

    sol = rules["SOLUSDT"]
    assert (sol.base_asset, sol.quote_asset) == ("SOL", "USDT")
    assert sol.step_size == Decimal("0.001")
    assert sol.min_qty == Decimal("0.001")
    assert sol.min_notional == Decimal("5")
    assert sol.tick_size == Decimal("0.01")
    assert sol.min_notional_applies_to_market is True

    assert rules["XRPUSDT"].step_size == Decimal("0.1")
    assert rules["XRPUSDT"].tick_size == Decimal("0.0001")
    assert rules["LINKUSDT"].step_size == Decimal("0.01")
    assert rules["LINKUSDT"].tick_size == Decimal("0.001")


def test_parse_ignores_zero_market_lot_size_step():
    # LOT_SIZE governs when MARKET_LOT_SIZE reports a zero step.
    sol = parse_exchange_info({"symbols": [SOL_ENTRY]})["SOLUSDT"]
    assert sol.step_size == Decimal("0.001")
    assert sol.quantize(0.036) == Decimal("0.036")
    assert sol.quantize(0.0009) is None  # minQty came from LOT_SIZE, not the zero one


def test_parse_falls_back_to_legacy_min_notional():
    entry = _symbol(
        "ADAUSDT",
        "ADA",
        "USDT",
        [
            _price_filter("0.00010000"),
            _lot_size("1.00000000", "1.00000000"),
            {"filterType": "MIN_NOTIONAL", "minNotional": "10.00000000", "applyToMarket": False},
        ],
    )
    rules = parse_exchange_info({"symbols": [entry]})["ADAUSDT"]
    assert rules.min_notional == Decimal("10")
    assert rules.min_notional_applies_to_market is False


def test_parse_prefers_notional_over_legacy_filter():
    entry = _symbol(
        "ADAUSDT",
        "ADA",
        "USDT",
        [
            _lot_size("1.00000000", "1.00000000"),
            _notional("7.00000000"),
            {"filterType": "MIN_NOTIONAL", "minNotional": "10.00000000", "applyToMarket": False},
        ],
    )
    rules = parse_exchange_info({"symbols": [entry]})["ADAUSDT"]
    assert rules.min_notional == Decimal("7")
    assert rules.min_notional_applies_to_market is True


def test_parse_skips_symbols_that_are_not_trading():
    halting = _symbol(
        "BREAKUSDT",
        "BRK",
        "USDT",
        [_price_filter("0.01"), _lot_size("0.001", "0.001")],
        status="BREAK",
    )
    rules = parse_exchange_info({"symbols": [halting, SOL_ENTRY]})
    assert set(rules) == {"SOLUSDT"}


def test_parse_skips_symbol_without_lot_size_instead_of_raising():
    no_lot = _symbol("NOLOTUSDT", "NOL", "USDT", [_price_filter("0.01"), _notional("5")])
    rules = parse_exchange_info({"symbols": [no_lot, SOL_ENTRY]})
    assert set(rules) == {"SOLUSDT"}


def test_parse_skips_symbol_with_zero_step_size():
    zero_step = _symbol("ZEROUSDT", "ZRO", "USDT", [_lot_size("0.00000000", "1.0")])
    assert parse_exchange_info({"symbols": [zero_step]}) == {}


def test_parse_tolerates_absent_and_unknown_filters():
    sparse = _symbol(
        "SPARSEUSDT",
        "SPR",
        "USDT",
        [_lot_size("0.001", "0.001"), {"filterType": "SOMETHING_NEW", "limit": 5}],
    )
    rules = parse_exchange_info({"symbols": [sparse]})["SPARSEUSDT"]
    assert rules.tick_size == Decimal("0")
    assert rules.min_notional == Decimal("0")
    assert rules.notional_ok(Decimal("0.001"), 1.0) is True


def test_parse_tolerates_empty_payload():
    assert parse_exchange_info({}) == {}
    assert parse_exchange_info({"symbols": []}) == {}


# ── cache round-trip ────────────────────────────────────────


def test_cache_round_trip_keeps_decimals(tmp_path):
    rules = parse_exchange_info(REAL_PAYLOAD)
    path = tmp_path / "instruments.json"
    save_cache(rules, path)

    loaded = load_cache(path)
    assert loaded == rules
    assert isinstance(loaded["SOLUSDT"].step_size, Decimal)

    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["SOLUSDT"]["step_size"] == "0.00100000"  # Decimals as strings, not floats
    assert raw["SOLUSDT"]["min_notional"] == "5.00000000"
    assert raw["SOLUSDT"]["min_notional_applies_to_market"] is True


def test_save_cache_creates_parent_directories(tmp_path):
    path = tmp_path / "data" / "nested" / "instruments.json"
    save_cache(parse_exchange_info(REAL_PAYLOAD), path)
    assert path.is_file()
    assert load_cache(path)["LINKUSDT"].step_size == Decimal("0.01")


def test_load_cache_missing_file_returns_empty(tmp_path):
    assert load_cache(tmp_path / "absent.json") == {}


def test_load_cache_corrupt_json_warns_and_returns_empty(tmp_path, caplog):
    path = tmp_path / "instruments.json"
    path.write_text("{not json at all", encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="arena.instruments"):
        assert load_cache(path) == {}

    assert any("corrupt" in record.message for record in caplog.records)


def test_load_cache_accepts_a_raw_exchange_info_payload(tmp_path):
    # data/instruments-binance.json may hold the venue payload verbatim.
    path = tmp_path / "instruments.json"
    path.write_text(json.dumps(REAL_PAYLOAD), encoding="utf-8")

    rules = load_cache(path)
    assert rules["SOLUSDT"].step_size == Decimal("0.001")
    assert rules["SOLUSDT"].quantize(0.0015) == Decimal("0.001")


# ── Cache refresh over HTTP (MockTransport: no network in the suite) ─────


def _solusdt_payload() -> dict:
    return {
        "symbols": [
            {
                "symbol": "SOLUSDT",
                "status": "TRADING",
                "baseAsset": "SOL",
                "quoteAsset": "USDT",
                "filters": [
                    _price_filter("0.01000000"),
                    _lot_size("0.00100000", "0.00100000"),
                    {
                        "filterType": "NOTIONAL",
                        "minNotional": "5.00000000",
                        "applyMinToMarket": True,
                    },
                ],
            }
        ]
    }


def test_fetch_exchange_info_sends_compact_symbol_filter():
    """Binance answers 400 when the symbols array is encoded with spaces."""
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json=_solusdt_payload())

    client = httpx.Client(transport=httpx.MockTransport(handler))
    fetch_exchange_info(symbols=["solusdt"], client=client)

    assert "%5B%22SOLUSDT%22%5D" in seen["url"]
    assert " " not in seen["url"]


def test_refresh_cache_persists_parsed_rules(tmp_path):
    client = httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=_solusdt_payload()))
    )
    path = tmp_path / "instruments.json"

    rules = refresh_cache(path, symbols=["SOLUSDT"], client=client)

    assert rules["SOLUSDT"].step_size == Decimal("0.001")
    assert load_cache(path)["SOLUSDT"].min_notional == Decimal("5.00000000")


def test_refresh_cache_warns_about_a_symbol_the_venue_does_not_list(tmp_path, caplog):
    client = httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=_solusdt_payload()))
    )
    with caplog.at_level(logging.WARNING, logger="arena.instruments"):
        refresh_cache(tmp_path / "instruments.json", symbols=["SOLUSDT", "DOGEUSDT"], client=client)

    assert "DOGEUSDT" in caplog.text
