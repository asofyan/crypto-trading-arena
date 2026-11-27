"""Unit tests for arena.price_book — PriceBook and CandleBook."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from arena.models import TIMEFRAMES
from arena.price_book import CandleBook, PriceBook, _default_parse_row


# ── PriceBook ────────────────────────────────────────────────────


def test_update_fills_optional_field_defaults():
    pb = PriceBook()
    pb.update(
        {"product_id": "BTC-USD", "price": "50000", "best_bid": "49990", "best_ask": "50010"}
    )
    entry = pb.get("BTC-USD")
    assert entry["price"] == "50000"
    assert entry["best_bid_size"] == "0"  # optional → default
    assert entry["best_ask_size"] == "0"
    assert entry["side"] == ""
    assert entry["volume_24h"] == "0"
    assert entry["time"] == ""


def test_get_unknown_product_returns_none():
    assert PriceBook().get("NOPE-USD") is None


def test_snapshot_is_an_independent_copy(price_book):
    snap = price_book.snapshot()
    snap.pop("BTC-USD")  # mutate the snapshot
    assert price_book.get("BTC-USD") is not None  # book unaffected


def test_update_overwrites_with_latest_quote(price_book):
    price_book.update(
        {"product_id": "BTC-USD", "price": "60000", "best_bid": "59990", "best_ask": "60010"}
    )
    assert price_book.get("BTC-USD")["price"] == "60000"


@pytest.mark.parametrize("missing", ["product_id", "price", "best_bid", "best_ask"])
def test_update_requires_core_fields(missing):
    data = {"product_id": "BTC-USD", "price": "1", "best_bid": "1", "best_ask": "1"}
    data.pop(missing)
    with pytest.raises(KeyError):
        PriceBook().update(data)


# ── CandleBook: parsing & ordering ───────────────────────────────


def test_default_parse_row_maps_coinbase_column_order():
    # Coinbase row: [time, low, high, open, close, volume]
    candle = _default_parse_row([1_700_000_000, 10.0, 30.0, 20.0, 25.0, 100.0])
    assert candle.open == 20.0
    assert candle.high == 30.0
    assert candle.low == 10.0
    assert candle.close == 25.0
    assert candle.volume == 100.0
    assert candle.time == datetime.fromtimestamp(1_700_000_000, tz=timezone.utc)


def test_update_from_api_sorts_candles_by_time():
    book = CandleBook()
    base = 1_700_000_000
    # Rows arrive newest-first (as Coinbase returns them); expect oldest-first output.
    rows = [
        [base + 120, 1, 2, 1, 2, 1],
        [base + 0, 1, 2, 1, 2, 1],
        [base + 60, 1, 2, 1, 2, 1],
    ]
    book.update_from_api("BTC-USD", 60, rows)
    out = book.format_prompt(["BTC-USD"])
    t0 = datetime.fromtimestamp(base, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    t1 = datetime.fromtimestamp(base + 60, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    t2 = datetime.fromtimestamp(base + 120, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert out.index(t0) < out.index(t1) < out.index(t2)


def test_update_from_api_replaces_previous_candles():
    book = CandleBook()
    book.update_from_api("BTC-USD", 60, [[1, 1, 1, 1, 1, 1], [2, 1, 1, 1, 1, 1]])
    book.update_from_api("BTC-USD", 60, [[3, 9, 9, 9, 9, 9]])  # replace, not append
    out = book.format_prompt(["BTC-USD"])
    assert out.count("BTC-USD,") == 1  # only the single replacement row


def test_custom_parse_row_is_used():
    sentinel = []

    def parser(row):
        sentinel.append(row)
        return _default_parse_row(row)

    book = CandleBook(parse_row=parser)
    book.update_from_api("BTC-USD", 60, [[1_700_000_000, 1, 2, 1, 2, 1]])
    assert sentinel  # custom parser was invoked


# ── CandleBook: prompt structure & has_data ──────────────────────


def test_format_prompt_includes_every_timeframe_and_header():
    book = CandleBook()
    book.update_from_api("BTC-USD", 60, [[1_700_000_000, 1, 2, 1, 2, 1]])
    out = book.format_prompt(["BTC-USD"])
    # All four timeframe section labels present...
    assert "1-daily candles" in out
    assert "15-min candles" in out
    assert "5-min candles" in out
    assert "1-min candles" in out
    # ...and the CSV header appears once per section.
    assert out.count("product,time,open,high,low,close,volume") == len(TIMEFRAMES)
    # 1-min section has the data row; coarser sections have none.
    assert "BTC-USD," in out


def test_has_data_and_empty_prompt():
    book = CandleBook()
    assert book.has_data() is False
    out = book.format_prompt(["BTC-USD"])
    assert "15-min candles" in out  # labels still render
    assert "BTC-USD," not in out  # but no data rows
    book.update_from_api("BTC-USD", 60, [[1, 1, 1, 1, 1, 1]])
    assert book.has_data() is True


# ── Lean payload: intraday tables + compact trend summary ─────────


def _candle(row):
    """Parse the synthetic [ts, open, high, low, close] rows this module uses."""
    from datetime import datetime, timezone

    from arena.models import Candle

    return Candle(
        time=datetime.fromtimestamp(row[0], tz=timezone.utc),
        open=float(row[1]),
        high=float(row[2]),
        low=float(row[3]),
        close=float(row[4]),
        volume=1.0,
    )


def _book_with(rows_by_timeframe, pid="SOLUSDT-PERP"):
    from arena.models import TIMEFRAMES
    from arena.price_book import CandleBook

    book = CandleBook(parse_row=lambda row: _candle(row))
    for tf in TIMEFRAMES:
        rows = rows_by_timeframe.get(tf.granularity)
        if rows:
            book.update_from_api(pid, tf.granularity, rows)
    return book


def _rising_rows(count, start=100.0, step=0.5):
    return [
        [i * 900, start + i * step, start + i * step + 0.2, start + i * step - 0.2, start + (i + 1) * step]
        for i in range(count)
    ]


def test_format_prompt_can_be_limited_to_a_subset_of_timeframes():
    from arena.models import INTRADAY_TIMEFRAMES

    book = _book_with({86400: _rising_rows(5), 900: _rising_rows(6), 60: _rising_rows(20)})

    lean = book.format_prompt(["SOLUSDT-PERP"], timeframes=INTRADAY_TIMEFRAMES)

    assert "15-min" in lean or "### 15-min candles" in lean
    assert "1-daily" not in lean, "the daily tier is not intraday"
    assert "4-hour" not in lean


def test_trend_summary_reports_regime_in_a_few_numbers():
    book = _book_with({86400: _rising_rows(30), 14400: _rising_rows(40)})

    summary = book.trend_summary(["SOLUSDT-PERP"])

    assert "SOLUSDT-PERP" in summary
    assert "EMA20" in summary
    assert "range position" in summary
    assert len(summary) < 600, "the whole point is that this replaces thousands of candle rows"


def test_trend_summary_is_empty_without_higher_timeframe_data():
    book = _book_with({900: _rising_rows(6)})

    assert book.trend_summary(["SOLUSDT-PERP"]) == ""
