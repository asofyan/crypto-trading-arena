"""Unit tests for the exchange connectors (no network, no LLM).

Covers candle/ticker parsing, the agent-prompt assembly in `_publish_latest` (via a
fake Client), connector construction guards, and the candle-book wiring asymmetry
between the two `run()` entrypoints (finding B1).
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from arena.fees import FeeModel
from arena.models import TIMEFRAMES
from arena.price_book import CandleBook, PriceBook
from exchanges import AGENT_OUTPUT_TOPIC, TickerMessage
from exchanges import binance as bn
from exchanges import coinbase as cb
from exchanges.binance import BINANCE_INTERVAL_MAP, BinanceKafkaConnector, parse_binance_candle
from exchanges.coinbase import CoinbaseKafkaConnector, parse_coinbase_candle


def _ticker(product_id="BTC-USD", price="50000.00") -> TickerMessage:
    return TickerMessage(
        product_id=product_id, price=price, best_bid="49990.00", best_bid_size="1",
        best_ask="50010.00", best_ask_size="2", side="buy", last_size="0.1",
        open_24h="49000", high_24h="51000", low_24h="48000", volume_24h="15000",
        volume_30d="900000", trade_id=7, sequence=11, time="2024-01-01T00:00:00Z",
    )


class _FakeGateway:
    """A fake calfkit AgentGateway: records .send() calls into a shared sink."""

    def __init__(self, topic, sink):
        self._topic = topic
        self._sink = sink

    async def send(self, user_prompt, *, deps=None, **kwargs):
        self._sink.append({"user_prompt": user_prompt, "topic": self._topic, "deps": deps})


class _FakeClient:
    """Captures agent(topic=...).send(...) invocations instead of touching a broker."""

    def __init__(self):
        self.invocations = []

    def agent(self, name=None, *, topic=None, output_type=str):
        return _FakeGateway(topic, self.invocations)


# ── Candle / ticker parsing ──────────────────────────────────────


def test_parse_coinbase_candle_column_order():
    # Coinbase: [time, low, high, open, close, volume]
    c = parse_coinbase_candle([1_700_000_000, 10.0, 30.0, 20.0, 25.0, 100.0])
    assert (c.open, c.high, c.low, c.close, c.volume) == (20.0, 30.0, 10.0, 25.0, 100.0)
    assert c.time == datetime.fromtimestamp(1_700_000_000, tz=timezone.utc)


def test_parse_binance_candle_column_order_and_ms_timestamp():
    # Binance: [openTime_ms, open, high, low, close, volume, ...]
    c = parse_binance_candle([1_700_000_000_000, 20.0, 30.0, 10.0, 25.0, 100.0, 999])
    assert (c.open, c.high, c.low, c.close, c.volume) == (20.0, 30.0, 10.0, 25.0, 100.0)
    assert c.time == datetime.fromtimestamp(1_700_000_000, tz=timezone.utc)  # ms → s


def test_parse_binance_ticker_maps_websocket_fields():
    conn = BinanceKafkaConnector(_FakeClient(), "t", ["BTCUSDT"], FeeModel(0))
    msg = conn._parse_binance_ticker(
        {"s": "BTCUSDT", "c": "50000", "b": "49990", "B": "1.0", "a": "50010",
         "A": "2.0", "Q": "0.5", "o": "49000", "h": "51000", "l": "48000",
         "v": "15000", "n": 42, "E": 1_700_000_000_000}
    )
    assert msg is not None
    assert msg.product_id == "BTCUSDT"
    assert msg.best_bid == "49990" and msg.best_ask == "50010"
    assert msg.price == "50000"


def test_parse_binance_ticker_missing_field_returns_none():
    conn = BinanceKafkaConnector(_FakeClient(), "t", ["BTCUSDT"], FeeModel(0))
    assert conn._parse_binance_ticker({"s": "BTCUSDT"}) is None  # missing required keys


def test_coinbase_skips_granularities_the_venue_cannot_serve():
    """Coinbase rejects 4h (14400s); sending it would fail the whole product poll.

    TIMEFRAMES carries one shared list, so the venue filter is what keeps the
    Binance-only tier from breaking the Coinbase connector.
    """
    from exchanges.coinbase import COINBASE_GRANULARITIES, coinbase_timeframes

    served = {tf.granularity for tf in coinbase_timeframes()}
    assert 14400 not in served
    assert served == {tf.granularity for tf in TIMEFRAMES} - {14400}
    assert COINBASE_GRANULARITIES >= served


def test_binance_interval_map_covers_every_timeframe_granularity():
    """Every TIMEFRAME granularity must map to a Binance interval, else poll_rest
    silently falls back to '1m' and fetches the wrong candle resolution."""
    for tf in TIMEFRAMES:
        assert tf.granularity in BINANCE_INTERVAL_MAP, tf.label


# ── Construction guards ──────────────────────────────────────────


def test_coinbase_connector_requires_products():
    with pytest.raises(ValueError):
        CoinbaseKafkaConnector(_FakeClient(), "t", [], FeeModel(0))


def test_binance_connector_requires_symbols():
    with pytest.raises(ValueError):
        BinanceKafkaConnector(_FakeClient(), "t", [], FeeModel(0))


def test_fee_disclosure_is_precomputed():
    fee = FeeModel(taker_bps=60)
    conn = CoinbaseKafkaConnector(_FakeClient(), "t", ["BTC-USD"], fee)
    assert conn._fee_disclosure == fee.disclosure_prompt()


# ── _publish_latest prompt assembly ──────────────────────────────

async def test_publish_latest_includes_fee_and_essential_ticker_fields():
    client = _FakeClient()
    conn = CoinbaseKafkaConnector(client, "agent_router.input", ["BTC-USD"], FeeModel(60))
    conn._latest = {"BTC-USD": _ticker()}

    await conn._publish_latest()

    assert len(client.invocations) == 1
    inv = client.invocations[0]
    prompt = inv["user_prompt"]
    assert inv["topic"] == "agent_router.input"
    assert "invoked_at" in inv["deps"]
    assert "60 bps" in prompt  # fee disclosure injected
    # Essential fields kept; verbose fields excluded from the batch JSON.
    assert '"product_id"' in prompt and '"best_bid"' in prompt and '"price"' in prompt
    assert '"volume_24h"' not in prompt
    assert '"best_bid_size"' not in prompt
    assert '"time"' not in prompt


def _coinbase_candles(rows_by_granularity):
    """A CandleBook with `n` synthetic rising candles per granularity."""
    from arena.price_book import CandleBook

    book = CandleBook()
    for granularity, count in rows_by_granularity.items():
        rows = [
            [1_700_000_000 + i * granularity, 100 + i, 101 + i, 99 + i, 100 + i, 5]
            for i in range(count)
        ]
        book.update_from_api("BTC-USD", granularity, rows)
    return book


async def test_publish_latest_sends_a_lean_message_to_the_lean_topic():
    """Two profiles off one publish: swing-trend keeps the full book, the scalper does not."""
    client = _FakeClient()
    candles = _coinbase_candles({86400: 5, 14400: 25, 900: 6, 300: 14, 60: 20})
    conn = CoinbaseKafkaConnector(
        client, "full.topic", ["BTC-USD"], FeeModel(0), candle_book=candles, lean_topic="lean.topic"
    )
    conn._latest = {"BTC-USD": _ticker()}

    await conn._publish_latest()

    prompts = {inv["topic"]: inv["user_prompt"] for inv in client.invocations}
    assert set(prompts) == {"full.topic", "lean.topic"}
    full, lean = prompts["full.topic"], prompts["lean.topic"]
    assert "1-daily" in full and "4-hour" in full
    assert "1-daily" not in lean and "4-hour candles" not in lean
    assert "intraday" in lean.lower()
    assert "Trend context" in lean and "EMA20" in lean
    assert len(lean) < len(full), "the lean payload must actually be smaller"


async def test_publish_latest_stays_single_message_without_a_lean_topic():
    client = _FakeClient()
    conn = CoinbaseKafkaConnector(client, "full.topic", ["BTC-USD"], FeeModel(0))
    conn._latest = {"BTC-USD": _ticker()}

    await conn._publish_latest()

    assert len(client.invocations) == 1


async def test_publish_latest_noop_when_no_tickers():
    client = _FakeClient()
    conn = CoinbaseKafkaConnector(client, "t", ["BTC-USD"], FeeModel(0))
    await conn._publish_latest()
    assert client.invocations == []


async def test_publish_latest_appends_candle_history_when_available():
    client = _FakeClient()
    candles = CandleBook()
    candles.update_from_api("BTC-USD", 60, [[1_700_000_000, 1, 2, 1, 2, 5]])
    conn = CoinbaseKafkaConnector(
        client, "t", ["BTC-USD"], FeeModel(0), candle_book=candles
    )
    conn._latest = {"BTC-USD": _ticker()}

    await conn._publish_latest()
    assert "Price History (OHLCV" in client.invocations[0]["user_prompt"]


async def test_publish_latest_omits_candle_history_when_absent():
    client = _FakeClient()
    conn = CoinbaseKafkaConnector(client, "t", ["BTC-USD"], FeeModel(0))  # no candle book
    conn._latest = {"BTC-USD": _ticker()}

    await conn._publish_latest()
    assert "Price History" not in client.invocations[0]["user_prompt"]


# ── run() candle-book wiring (finding B1) ────────────────────────


def _capturing(captured):
    """A stand-in connector class that records its construction kwargs."""

    class _Conn:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def stop(self):  # referenced by run()'s signal-handler registration
            pass

        async def start(self):
            return None

    return _Conn


def _stub_run_env(monkeypatch, module):
    """Stub broker connect, env, and signal-handler registration for a run() call.

    Returns (connector_kwargs, connect_kwargs) captured during the run() call."""
    captured = {}
    connect_kwargs = {}

    class _Client:
        @staticmethod
        def connect(servers, **kwargs):
            connect_kwargs.update(kwargs)
            return object()

    monkeypatch.setenv("OPENAI_API_KEY", "dummy")  # config.json references it
    monkeypatch.setattr(module, "Client", _Client)
    # run() registers SIGINT/SIGTERM handlers on the running loop; neutralize them.
    monkeypatch.setattr(
        asyncio.get_running_loop(), "add_signal_handler", lambda *a, **k: None
    )
    return captured, connect_kwargs


async def test_binance_run_wires_a_candle_book(monkeypatch):
    captured, connect_kwargs = _stub_run_env(monkeypatch, bn)
    monkeypatch.setattr(bn, "BinanceKafkaConnector", _capturing(captured))
    args = SimpleNamespace(
        config="config.json", symbols=["BTCUSDT"], min_interval=60.0, bootstrap_servers="x"
    )
    await bn.run(args)
    # The connector must bind the shared inbox so the viewer can observe agent events.
    assert connect_kwargs.get("inbox_topic") == AGENT_OUTPUT_TOPIC
    assert captured.get("candle_book") is not None
    assert captured.get("venue") is bn.SPOT_VENUE


# ── Venue parameterisation (perp, design D7) ─────────────────────


def test_spot_venue_describes_the_current_spot_contract():
    venue = bn.SPOT_VENUE
    assert venue.is_perp is False
    assert venue.product_suffix == ""
    assert venue.product_id_for("solusdt") == "SOLUSDT"  # bare ids, as today
    assert venue.ws_urls == (
        "wss://stream.binance.com:9443",
        "wss://stream.binance.com:443",
    )
    assert venue.rest_urls == (
        "https://api.binance.com",
        "https://api1.binance.com",
        "https://api2.binance.com",
        "https://api3.binance.com",
    )
    assert venue.klines_path == "/api/v3/klines"
    assert venue.ticker_path == "/api/v3/ticker/24hr"
    assert venue.book_ticker_path is None


def test_perp_venue_describes_the_futures_contract():
    venue = bn.PERP_VENUE
    assert venue.is_perp is True
    assert venue.product_suffix == "-PERP"
    assert venue.product_id_for("SOLUSDT") == "SOLUSDT-PERP"
    assert venue.ws_urls == (
        "wss://fstream.binance.com",
        "wss://fstream.binance.com:443",
    )
    assert venue.rest_urls == ("https://fapi.binance.com",)
    assert venue.klines_path == "/fapi/v1/klines"
    assert venue.ticker_path == "/fapi/v1/ticker/24hr"
    assert venue.book_ticker_path == "/fapi/v1/ticker/bookTicker"


def test_parse_binance_ticker_publishes_the_perp_product_id():
    payload = {
        "s": "SOLUSDT", "c": "100.5", "b": "100.4", "B": "3.0", "a": "100.6",
        "A": "4.0", "Q": "0.5", "o": "98", "h": "102", "l": "97",
        "v": "15000", "n": 42, "E": 1_700_000_000_000,
    }
    perp = BinanceKafkaConnector(
        _FakeClient(), "t", ["SOLUSDT"], FeeModel(0), venue=bn.PERP_VENUE
    )
    msg = perp._parse_binance_ticker(payload)
    assert msg is not None and msg.product_id == "SOLUSDT-PERP"

    # Cash market unchanged: the connector still publishes the bare venue symbol.
    spot = BinanceKafkaConnector(_FakeClient(), "t", ["SOLUSDT"], FeeModel(0))
    assert spot._parse_binance_ticker(payload).product_id == "SOLUSDT"


def test_perp_disclosure_quotes_the_perp_rate_not_the_spot_rate():
    fee = FeeModel(taker_bps=10, perp_taker_bps=5)
    perp = BinanceKafkaConnector(
        _FakeClient(), "t", ["SOLUSDT"], fee, venue=bn.PERP_VENUE
    )
    assert perp._fee_disclosure == fee.disclosure_prompt(perp=True)
    assert "A taker fee of 5 bps" in perp._fee_disclosure  # the perp rate, not 10
    assert "A taker fee of 10 bps" not in perp._fee_disclosure

    spot = BinanceKafkaConnector(_FakeClient(), "t", ["SOLUSDT"], fee)
    assert "A taker fee of 10 bps" in spot._fee_disclosure


class _FuturesREST:
    """Fake fapi REST client: two all-symbol boards, no per-symbol ticker calls."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def get_klines(self, symbol, interval, start_time=None, end_time=None, limit=1000):
        self.calls.append(("klines", symbol))
        return [[1_700_000_000_000, 20.0, 30.0, 10.0, 25.0, 100.0, 999]]

    async def get_24h_ticker(self, symbol=None):
        assert symbol is None, "futures 24hr must be requested for the whole board once"
        self.calls.append(("24hr", symbol))
        return [
            {"symbol": "SOLUSDT", "lastPrice": "100.0", "lastQty": "0.5",
             "volume": "15000", "closeTime": 1_700_000_000_000},
            {"symbol": "XRPUSDT", "lastPrice": "2.0", "lastQty": "10",
             "volume": "999", "closeTime": 1_700_000_000_000},
            {"symbol": "DOGEUSDT", "lastPrice": "0.1"},  # not subscribed: dropped
        ]

    async def get_book_ticker(self, symbol=None):
        assert symbol is None, "bookTicker must be requested for the whole board once"
        self.calls.append(("book", symbol))
        return [
            {"symbol": "SOLUSDT", "bidPrice": "99.9", "bidQty": "5",
             "askPrice": "100.1", "askQty": "6"},
            {"symbol": "XRPUSDT", "bidPrice": "1.99", "bidQty": "7",
             "askPrice": "2.01", "askQty": "8"},
        ]


async def test_poll_once_perp_joins_the_board_once_per_cycle():
    """fapi's 24hr ticker has no bid/ask and weighs 40 *per symbol*; bookTicker weighs
    2 for the whole board. One request each per cycle, joined by symbol (design D7)."""
    client = _FuturesREST()
    prices = PriceBook()
    candles = CandleBook(parse_row=parse_binance_candle)

    await bn._poll_once(
        client,
        ["SOLUSDT", "XRPUSDT"],
        prices,
        candles,
        venue=bn.PERP_VENUE,
        products={"SOLUSDT": "SOLUSDT-PERP", "XRPUSDT": "XRPUSDT-PERP"},
    )

    ticker_calls = [call for call in client.calls if call[0] in ("24hr", "book")]
    assert ticker_calls == [("24hr", None), ("book", None)]  # exactly once, no symbol

    snapshot = prices.snapshot()
    assert set(snapshot) == {"SOLUSDT-PERP", "XRPUSDT-PERP"}  # product ids, not symbols
    sol = snapshot["SOLUSDT-PERP"]
    assert (sol["price"], sol["best_bid"], sol["best_ask"]) == ("100.0", "99.9", "100.1")
    assert (sol["best_bid_size"], sol["best_ask_size"]) == ("5", "6")
    assert (sol["last_size"], sol["volume_24h"]) == ("0.5", "15000")

    # Candles are keyed by product id too, else format_prompt() finds nothing.
    prompt = candles.format_prompt(["SOLUSDT-PERP"])
    assert "SOLUSDT-PERP,2023-11-14T22:13:20Z" in prompt
    assert "XRPUSDT-PERP" not in prompt


async def test_poll_once_spot_still_calls_the_per_symbol_ticker():
    """Regression guard: the spot path must stay one `24hr?symbol=` call per symbol."""

    class _SpotREST:
        def __init__(self) -> None:
            self.ticker_calls: list[str | None] = []

        async def get_klines(
            self, symbol, interval, start_time=None, end_time=None, limit=1000
        ):
            return []

        async def get_24h_ticker(self, symbol=None):
            self.ticker_calls.append(symbol)
            return {
                "lastPrice": "100", "bidPrice": "99", "bidQty": "1",
                "askPrice": "101", "askQty": "2", "lastQty": "3",
                "volume": "5000", "closeTime": 1_700_000_000_000,
            }

    client = _SpotREST()
    prices = PriceBook()
    await bn._poll_once(
        client, ["SOLUSDT"], prices, CandleBook(parse_row=parse_binance_candle)
    )

    assert client.ticker_calls == ["SOLUSDT"]
    assert prices.snapshot()["SOLUSDT"]["best_bid"] == "99"


async def test_poll_rest_perp_cycle_stores_quotes_under_product_ids(monkeypatch):
    """End-to-end over a faked BinanceRESTClient (no network): the poller must build the
    client for the futures venue and publish the joined quote as SOLUSDT-PERP."""
    fake = _FuturesREST()
    built: dict = {}

    def _factory(*args, **kwargs):
        built.update(kwargs)
        return fake

    monkeypatch.setattr(bn, "BinanceRESTClient", _factory)

    class _SignalBook(PriceBook):
        """PriceBook that trips an event on the first update, so the loop can be stopped."""

        def __init__(self) -> None:
            super().__init__()
            self.first_quote = asyncio.Event()

        def update(self, data: dict) -> None:
            super().update(data)
            self.first_quote.set()

    prices = _SignalBook()
    task = asyncio.create_task(
        bn.poll_rest(
            ["SOLUSDT"],
            prices,
            CandleBook(parse_row=parse_binance_candle),
            interval=3600.0,
            venue=bn.PERP_VENUE,
            products={"SOLUSDT": "SOLUSDT-PERP"},
        )
    )
    try:
        await asyncio.wait_for(prices.first_quote.wait(), timeout=5)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert built["venue"] is bn.PERP_VENUE
    snapshot = prices.snapshot()
    assert set(snapshot) == {"SOLUSDT-PERP"}
    assert (snapshot["SOLUSDT-PERP"]["price"], snapshot["SOLUSDT-PERP"]["best_ask"]) == (
        "100.0",
        "100.1",
    )

async def test_rest_client_uses_the_venue_paths(monkeypatch):
    seen: list[str] = []

    async def fake_request(self, method, path, **kwargs):
        seen.append(path)
        return []

    monkeypatch.setattr(bn.BinanceRESTClient, "_request", fake_request)
    async with bn.BinanceRESTClient(venue=bn.PERP_VENUE) as client:
        await client.get_klines("SOLUSDT", "1m")
        await client.get_24h_ticker()  # no symbol -> whole board
        await client.get_book_ticker()

    assert seen == [
        "/fapi/v1/klines",
        "/fapi/v1/ticker/24hr",
        "/fapi/v1/ticker/bookTicker",
    ]


async def test_rest_client_rotates_only_within_the_venue_urls():
    async with bn.BinanceRESTClient(venue=bn.PERP_VENUE) as client:
        assert client._base_url == "https://fapi.binance.com"
        # fapi publishes a single host; rotation must not wrap onto a spot URL.
        client._rotate_url()
        assert client._base_url == "https://fapi.binance.com"


async def test_rest_client_refuses_book_ticker_on_a_venue_without_one():
    async with bn.BinanceRESTClient() as client:
        with pytest.raises(ValueError, match="bookTicker"):
            await client.get_book_ticker()


def test_parse_args_perp_defaults_to_the_futures_publish_interval(capsys):
    assert bn.parse_args([]).min_interval == 60.0
    assert bn.parse_args([], venue=bn.PERP_VENUE).min_interval == 15.0

    with pytest.raises(SystemExit):
        bn.parse_args(["--help"], venue=bn.PERP_VENUE)
    assert "python -m exchanges.binance_perp" in capsys.readouterr().out


async def test_binance_perp_run_reads_perp_symbols_and_fee(monkeypatch):
    import config as config_module

    captured, _ = _stub_run_env(monkeypatch, bn)
    monkeypatch.setattr(bn, "BinanceKafkaConnector", _capturing(captured))
    trading = SimpleNamespace(
        binance_symbols=["BTCUSDT"],
        binance_perp_symbols=["SOLUSDT", "XRPUSDT"],
        fees=SimpleNamespace(taker_bps=10, perp_taker_bps=5),
    )
    monkeypatch.setattr(
        config_module, "load_config_strict", lambda path: SimpleNamespace(trading=trading)
    )
    args = SimpleNamespace(config="config.json", symbols=None, min_interval=15.0,
                           bootstrap_servers="x")

    await bn.run(args, venue=bn.PERP_VENUE)

    assert captured["venue"] is bn.PERP_VENUE
    assert captured["symbols"] == ["SOLUSDT", "XRPUSDT"]
    assert captured["fee_model"] == FeeModel(taker_bps=10, perp_taker_bps=5)


async def test_binance_perp_run_survives_a_config_without_the_perp_key(monkeypatch):
    """The perp symbol list arrives with the config change; until then the entrypoint
    must fall back instead of raising AttributeError at startup."""
    import config as config_module

    captured, _ = _stub_run_env(monkeypatch, bn)
    monkeypatch.setattr(bn, "BinanceKafkaConnector", _capturing(captured))
    trading = SimpleNamespace(
        binance_symbols=["BTCUSDT"], fees=SimpleNamespace(taker_bps=10, perp_taker_bps=5)
    )
    monkeypatch.setattr(
        config_module, "load_config_strict", lambda path: SimpleNamespace(trading=trading)
    )
    args = SimpleNamespace(config="config.json", symbols=None, min_interval=15.0,
                           bootstrap_servers="x")

    await bn.run(args, venue=bn.PERP_VENUE)

    assert captured["symbols"] == list(bn.DEFAULT_SYMBOLS)


async def test_binance_perp_entrypoint_pins_the_futures_venue(monkeypatch):
    from exchanges import binance_perp as bp

    seen: dict = {}

    async def fake_run(args, *, venue=None, agent_topic=None, lean_topic=None):
        seen.update(args=args, venue=venue, agent_topic=agent_topic, lean_topic=lean_topic)

    monkeypatch.setattr(bp, "binance_run", fake_run)
    args = SimpleNamespace()

    await bp.run(args)

    assert seen["args"] is args
    assert seen["venue"] is bn.PERP_VENUE
    # The perp connector is the one that feeds a lean scalper at all: losing this
    # forward leaves every `payload: lean` agent starving with no error.
    assert seen["lean_topic"] == bp.AGENT_INPUT_TOPIC_LEAN
    assert bp.parse_args([]).min_interval == 15.0


@pytest.mark.xfail(
    reason="B1: Coinbase run() never passes a candle_book, so Coinbase agents get no OHLCV",
    strict=True,
)
async def test_coinbase_run_wires_a_candle_book(monkeypatch):
    captured, connect_kwargs = _stub_run_env(monkeypatch, cb)
    monkeypatch.setattr(cb, "CoinbaseKafkaConnector", _capturing(captured))
    args = SimpleNamespace(
        config="config.json", products=["BTC-USD"], min_interval=60.0, bootstrap_servers="x"
    )
    await cb.run(args)
    assert connect_kwargs.get("inbox_topic") == AGENT_OUTPUT_TOPIC
    assert captured.get("candle_book") is not None


# ── Closed-bar guarantee for the strategy prompts ───────────────


def test_drop_forming_bars_strips_the_in_progress_candle():
    """A window ending at "now" returns the forming bar last; prompts promise closed bars."""
    now_ms = 1_700_000_060_000
    rows = [
        [now_ms - 120_000, "1", "1", "1", "1", "0"],
        [now_ms - 60_000, "1", "1", "1", "1", "0"],   # closed 60s before now
        [now_ms, "1", "1", "1", "1", "0"],            # still forming
    ]

    kept = bn.drop_forming_bars(rows, 60, now_ms)

    assert len(kept) == 2
    assert kept[-1][0] == now_ms - 60_000


def test_drop_forming_bars_keeps_a_fully_closed_window():
    now_ms = 1_700_000_000_000
    rows = [[now_ms - 900_000, "1", "1", "1", "1", "0"], [now_ms - 300_000, "1", "1", "1", "1", "0"]]

    assert bn.drop_forming_bars(rows, 60, now_ms) == rows
    assert bn.drop_forming_bars([], 60, now_ms) == []


# ── REST ticker board: the fallback when the socket is blackholed ──

FUTURES_STAT = {
    "symbol": "XRPUSDT",
    "lastPrice": "2.1040",
    "lastQty": "120.5",
    "openPrice": "2.05",
    "highPrice": "2.13",
    "lowPrice": "2.01",
    "volume": "998877.6",
    "closeTime": 1_700_000_000_000,
    "lastId": 4242,
}
FUTURES_BOOK = {"symbol": "XRPUSDT", "bidPrice": "2.1039", "bidQty": "900", "askPrice": "2.1041", "askQty": "850"}


def test_the_board_ticker_carries_the_futures_touch_and_product_id():
    """fapi's 24h ticker has no bid/ask, so the touch must come from bookTicker."""
    ticker = bn._ticker_message("XRPUSDT-PERP", FUTURES_STAT, FUTURES_BOOK)

    assert ticker.product_id == "XRPUSDT-PERP"
    assert ticker.price == "2.1040"
    assert ticker.best_bid == "2.1039"
    assert ticker.best_ask == "2.1041"
    assert ticker.volume_24h == "998877.6"


def test_the_board_ticker_falls_back_to_the_stat_touch_for_spot():
    """Spot's 24h entry already includes bid/ask, so no book row is needed."""
    stat = dict(FUTURES_STAT, bidPrice="2.1038", askPrice="2.1042", bidQty="10", askQty="20")

    ticker = bn._ticker_message("XRPUSDT", stat)

    assert ticker.best_bid == "2.1038"
    assert ticker.best_ask == "2.1042"


async def test_the_board_is_ignored_while_the_socket_is_delivering():
    """A REST row that arrives next to live frames is staler than what is buffered."""
    published: list = []

    class _Broker:
        async def publish(self, message, topic):
            published.append((message, topic))

    client = _FakeClient()
    client.broker = _Broker()
    conn = BinanceKafkaConnector(
        client, "agent_router.input", ["XRPUSDT"], FeeModel(5), venue=bn.PERP_VENUE, board_interval=10
    )
    ticker = bn._ticker_message("XRPUSDT-PERP", FUTURES_STAT, FUTURES_BOOK)

    conn._ws_last_frame = time.time()
    await conn._on_board_quote(ticker)
    assert conn._latest == {}

    conn._ws_last_frame = time.time() - 60  # the socket has been silent for a minute
    await conn._on_board_quote(ticker)
    assert conn._latest["XRPUSDT-PERP"] is ticker
    assert published and published[0][0] is ticker


def test_the_lean_topic_is_a_stable_wire_contract():
    """Independently deployed connectors and agents meet on this string."""
    from exchanges import AGENT_INPUT_TOPIC, AGENT_INPUT_TOPIC_LEAN

    assert AGENT_INPUT_TOPIC == "agent_router.input"
    assert AGENT_INPUT_TOPIC_LEAN == "agent_router.input.lean"


async def test_binance_run_subscribes_both_profiles(monkeypatch):
    """The seam that actually decides whether a lean agent ever gets a message."""
    from types import SimpleNamespace

    import exchanges.binance as bn
    from config import ArenaConfig
    from exchanges import AGENT_INPUT_TOPIC, AGENT_INPUT_TOPIC_LEAN

    captured: dict = {}

    class FakeConnector:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        async def start(self):
            return None

        def stop(self):
            pass

    monkeypatch.setattr(bn, "BinanceKafkaConnector", FakeConnector)
    monkeypatch.setattr(bn, "Client", SimpleNamespace(connect=lambda *a, **k: object()))
    monkeypatch.setattr("config.load_config_strict", lambda path: ArenaConfig())

    args = SimpleNamespace(
        config="config.json", bootstrap_servers="b:9092", symbols=["ZECUSDT"],
        min_interval=60.0, board_interval=0.0,
    )

    await bn.run(args)

    assert captured["agent_topic"] == AGENT_INPUT_TOPIC
    assert captured["lean_topic"] == AGENT_INPUT_TOPIC_LEAN
