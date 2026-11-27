"""Binance exchange connector — WebSocket consumer, REST poller,
and Kafka connector in a single module.

The module serves two markets from one implementation: Binance **spot** and Binance
**USDⓈ-M perpetual futures**. Everything that differs between them — WS/REST hosts, the
klines and ticker paths, and whether product ids carry the ``-PERP`` suffix — lives in a
`BinanceVenue`, so the ticker parser, the candle parser and the Kafka contract stay
single-sourced. See docs/plans/261126-0810-scalper-perp/design.md, D7.

Usage:
    uv run python -m exchanges.binance
    uv run python -m exchanges.binance --symbols SOLUSDT XRPUSDT LINKUSDT
    uv run python -m exchanges.binance --min-interval 30
    uv run python -m exchanges.binance_perp --symbols SOLUSDT XRPUSDT LINKUSDT
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import os
import signal
import sys
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

import httpx
import websockets

from calfkit import Client

from arena.fees import DEFAULT_PERP_TAKER_BPS, FeeModel
from arena.instruments import PERP_SUFFIX
from arena.models import Candle, TIMEFRAMES
from arena.price_book import CandleBook, PriceBook
from exchanges import (
    AGENT_INPUT_TOPIC,
    AGENT_INPUT_TOPIC_LEAN,
    AGENT_OUTPUT_TOPIC,
    PRICE_TOPIC,
    TickerMessage,
    build_market_message,
    quiet_shared_inbox_reply_log,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BinanceVenue:
    """One Binance market's addressing: hosts, REST paths, and product-id shape.

    Frozen and data-only on purpose: a venue is a constant, never runtime state, so a
    connector can log which market it is streaming from a single field.
    """

    name: str
    ws_urls: tuple[str, ...]
    rest_urls: tuple[str, ...]
    klines_path: str
    ticker_path: str
    book_ticker_path: str | None
    product_suffix: str

    @property
    def is_perp(self) -> bool:
        return bool(self.product_suffix)

    @property
    def label(self) -> str:
        return "USDⓈ-M perpetual futures" if self.is_perp else "spot"

    def product_id_for(self, symbol: str) -> str:
        """The arena product id for a venue symbol (spot ids stay bare)."""
        return f"{symbol.strip().upper()}{self.product_suffix}"


SPOT_VENUE = BinanceVenue(
    name="binance",
    ws_urls=("wss://stream.binance.com:9443", "wss://stream.binance.com:443"),
    rest_urls=(
        "https://api.binance.com",
        "https://api1.binance.com",
        "https://api2.binance.com",
        "https://api3.binance.com",
    ),
    klines_path="/api/v3/klines",
    ticker_path="/api/v3/ticker/24hr",
    # Spot's 24hr ticker already carries bid/ask and its size, so no second call.
    book_ticker_path=None,
    product_suffix="",
)

PERP_VENUE = BinanceVenue(
    name="binance_perp",
    ws_urls=("wss://fstream.binance.com", "wss://fstream.binance.com:443"),
    rest_urls=("https://fapi.binance.com",),
    klines_path="/fapi/v1/klines",
    ticker_path="/fapi/v1/ticker/24hr",
    # Futures' 24hr ticker has no bid/ask at all; bookTicker (weight 2, all symbols)
    # supplies the touch. See design D7.
    book_ticker_path="/fapi/v1/ticker/bookTicker",
    product_suffix=PERP_SUFFIX,
)
PERP_DEMO_VENUE = replace(
    PERP_VENUE,
    name="binance-futures-demo",
    ws_urls=("wss://demo-fstream.binance.com",),
    rest_urls=("https://demo-fapi.binance.com",),
)


def futures_quote_source(demo: bool) -> str:
    return PERP_DEMO_VENUE.name if demo else PERP_VENUE.name


def valid_futures_touch(bid: str, ask: str) -> bool:
    try:
        bid_price, ask_price = float(bid), float(ask)
    except (ValueError, TypeError):
        return False
    return 0 < bid_price <= ask_price and math.isfinite(ask_price)

# Binance interval mapping (seconds -> Binance format)
BINANCE_INTERVAL_MAP = {
    60: "1m",
    300: "5m",
    900: "15m",
    14400: "4h",
    86400: "1d",
}

DEFAULT_SYMBOLS = [
    "SOLUSDT",
    "XRPUSDT",
    "LINKUSDT",
]

RECONNECT_DELAY_SECONDS = 3
PING_INTERVAL_SECONDS = 30  # Must send ping within 60 seconds per Binance docs
MAX_CONNECTION_LIFETIME_SECONDS = 82800  # 23 hours (Binance limit is 24h)


def parse_binance_candle(row: list) -> Candle:
    """Parse a Binance candle row: [ts_ms, open, high, low, close, vol, ...]."""
    return Candle(
        time=datetime.fromtimestamp(row[0] / 1000, tz=timezone.utc),
        open=float(row[1]),
        high=float(row[2]),
        low=float(row[3]),
        close=float(row[4]),
        volume=float(row[5]),
    )


class BinanceRESTClient:
    """Binance REST client for one venue, with automatic failover."""

    def __init__(self, venue: BinanceVenue = SPOT_VENUE) -> None:
        self._venue = venue
        self._url_index = 0
        self._client = httpx.AsyncClient(timeout=15.0)

    @property
    def _base_url(self) -> str:
        return self._venue.rest_urls[self._url_index]

    def _rotate_url(self) -> str:
        """Rotate to next fallback URL."""
        self._url_index = (self._url_index + 1) % len(self._venue.rest_urls)
        logger.info("Switching to fallback URL: %s", self._base_url)
        return self._base_url

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self._client:
            await self._client.aclose()

    async def _request(self, method: str, path: str, **kwargs) -> Any:
        """Make request with automatic retry on different base URLs."""
        last_error = None

        for _ in range(len(self._venue.rest_urls)):
            url = f"{self._base_url}{path}"
            try:
                response = await self._client.request(method, url, **kwargs)
                response.raise_for_status()
                return response.json()
            except (httpx.NetworkError, httpx.TimeoutException) as e:
                last_error = e
                logger.warning("Request failed to %s: %s", url, e)
                self._rotate_url()
            except httpx.HTTPStatusError as e:
                # Don't retry on 4xx errors
                if e.response.status_code < 500:
                    raise
                last_error = e
                logger.warning("Server error from %s: %s", url, e)
                self._rotate_url()

        raise last_error or Exception("All Binance API endpoints failed")

    async def get_klines(
        self,
        symbol: str,
        interval: str,
        start_time: int | None = None,
        end_time: int | None = None,
        limit: int = 1000,
    ) -> list[list]:
        """Fetch klines (candlestick) data."""
        params = {
            "symbol": symbol.upper(),
            "interval": interval,
            "limit": limit,
        }
        if start_time:
            params["startTime"] = start_time
        if end_time:
            params["endTime"] = end_time

        return await self._request("GET", self._venue.klines_path, params=params)

    async def get_24h_ticker(self, symbol: str | None = None) -> Any:
        """Fetch 24h rolling window statistics for `symbol`, or the whole board if None."""
        params = {"symbol": symbol.upper()} if symbol else None
        return await self._request("GET", self._venue.ticker_path, params=params)

    async def get_book_ticker(self, symbol: str | None = None) -> Any:
        """Fetch best bid/ask for `symbol`, or the whole board if None.

        Futures only: spot's 24hr ticker already carries the touch.
        """
        if self._venue.book_ticker_path is None:
            raise ValueError(
                f"{self._venue.name} has no bookTicker REST path; "
                "its 24hr ticker already carries bid/ask"
            )
        params = {"symbol": symbol.upper()} if symbol else None
        return await self._request("GET", self._venue.book_ticker_path, params=params)


def drop_forming_bars(rows: list[list], granularity_seconds: int, now_ms: int) -> list[list]:
    """Strip trailing candles that have not closed yet.

    A request whose endTime is "now" comes back with the in-progress candle as its last
    row. The strategy prompts promise that the newest row of every table is a *closed*
    bar — a scalper reading the forming 1-minute bar would trade a high that is not a
    high yet. Rows are keyed by their open time, so a row closes at
    ``open + granularity``.
    """
    closed = list(rows)
    while closed and isinstance(closed[-1], list) and (
        closed[-1][0] + granularity_seconds * 1000 > now_ms
    ):
        closed.pop()
    return closed


async def _refresh_candles(
    client: BinanceRESTClient,
    symbol: str,
    product_id: str,
    candle_book: CandleBook,
    now_ms: int,
) -> None:
    """Refresh every timeframe for one symbol.

    Candles are stored under the *product id* because that is what `format_prompt`
    indexes by; the venue is only ever asked for its own bare symbol.
    """
    for tf in TIMEFRAMES:
        start_ms = now_ms - tf.start_minutes_ago * 60 * 1000
        end_ms = now_ms - tf.end_minutes_ago * 60 * 1000
        interval_str = BINANCE_INTERVAL_MAP.get(tf.granularity, "1m")

        resp = await client.get_klines(
            symbol=symbol,
            interval=interval_str,
            start_time=start_ms,
            end_time=end_ms,
        )
        if tf.end_minutes_ago == 0:
            # Only the window that ends at "now" can contain a forming bar.
            resp = drop_forming_bars(resp, tf.granularity, now_ms)
        candle_book.update_from_api(product_id, tf.granularity, resp)


def _iso_ms(value: Any) -> str:
    """A venue millisecond timestamp as ISO-8601, or "" when the venue omitted it."""
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000, tz=timezone.utc).isoformat()
    return ""


def _ticker_message(
    product_id: str,
    stat: dict,
    book: dict | None = None,
) -> TickerMessage:
    """One agent-facing ticker built from a REST board row.

    `stat` is a 24h-ticker entry; `book` is the futures bookTicker row that carries the
    touch, because `/fapi/v1/ticker/24hr` has no bid/ask at all. Spot's 24hr entry already
    includes the touch, so `book` stays ``None`` there and the stat's own fields are used.
    """
    bid = (book or {}).get("bidPrice") or stat.get("bidPrice") or stat.get("lastPrice", "")
    ask = (book or {}).get("askPrice") or stat.get("askPrice") or stat.get("lastPrice", "")
    return TickerMessage(
        product_id=product_id,
        price=str(stat.get("lastPrice", "")),
        best_bid=str(bid),
        best_bid_size=str((book or {}).get("bidQty") or stat.get("bidQty") or "0"),
        best_ask=str(ask),
        best_ask_size=str((book or {}).get("askQty") or stat.get("askQty") or "0"),
        side="",
        last_size=str(stat.get("lastQty", "0")),
        open_24h=str(stat.get("openPrice", "0")),
        high_24h=str(stat.get("highPrice", "0")),
        low_24h=str(stat.get("lowPrice", "0")),
        volume_24h=str(stat.get("volume", "0")),
        volume_30d="0",
        trade_id=int(stat.get("lastId") or 0),
        sequence=0,
        time=_iso_ms(stat.get("closeTime")),
    )


async def poll_ticker_board(
    symbols: list[str],
    on_quote: Callable[[TickerMessage], Awaitable[None]],
    interval: float = 10.0,
    venue: BinanceVenue = SPOT_VENUE,
    products: dict[str, str] | None = None,
) -> None:
    """Publish tickers from the venue's REST board, independently of the WebSocket.

    The socket is the fast path, but a network that blackholes `fstream` would leave the
    agents with no data at all — and the arena would look alive while nothing trades.
    The board carries the same top of book at a coarser cadence: one all-symbols 24h call
    (weight 40) plus the futures bookTicker call (weight 2) per cycle, so 10s costs about
    250 weight/minute against a 2400 budget. See design D7.
    """
    mapping = products or {}
    async with BinanceRESTClient(venue=venue) as client:
        while True:
            try:
                stats = await client.get_24h_ticker()
                rows = stats if isinstance(stats, list) else [stats]
                books: dict[str, dict] = {}
                if venue.book_ticker_path:
                    payload = await client.get_book_ticker()
                    entries = payload if isinstance(payload, list) else [payload]
                    books = {
                        str(entry.get("symbol", "")).upper(): entry
                        for entry in entries
                        if isinstance(entry, dict)
                    }
                by_symbol = {
                    str(row.get("symbol", "")).upper(): row
                    for row in rows
                    if isinstance(row, dict)
                }
                for symbol in symbols:
                    stat = by_symbol.get(symbol.upper())
                    if stat is None:
                        continue
                    book = books.get(symbol.upper())
                    ticker = _ticker_message(
                        mapping.get(symbol, venue.product_id_for(symbol)), stat, book,
                    )
                    if venue.is_perp and book and valid_futures_touch(
                        book.get("bidPrice", ""), book.get("askPrice", "")
                    ):
                        ticker.source = venue.name
                        ticker.time = _iso_ms(book.get("time"))
                    await on_quote(ticker)
            except Exception:
                # A failed cycle must not kill the loop: the dashboard keeps the last
                # price and the next cycle retries.
                logger.exception(
                    "REST ticker board failed for %s; retrying in %.0fs", venue.name, interval
                )
            await asyncio.sleep(interval)


async def _futures_price_board(client: BinanceRESTClient) -> dict[str, dict]:
    """One joined snapshot of the whole futures board: stats plus bid/ask.

    ``/fapi/v1/ticker/24hr`` carries no bid/ask and weighs 40 per *symbol*, so calling it
    per symbol would spend 120 weight per cycle for data the all-symbol form returns in
    one request; ``/fapi/v1/ticker/bookTicker`` (weight 2, all symbols) adds the touch.
    See design D7.
    """
    try:
        stats = await client.get_24h_ticker()
        books = await client.get_book_ticker()
    except Exception:
        # One failed cycle must not kill the poller: the dashboard keeps the last price.
        logger.exception("REST poll failed for the futures ticker board")
        return {}

    board: dict[str, dict] = {}
    for stat in stats or []:
        symbol = str(stat.get("symbol") or "").upper()
        if not symbol:
            continue
        board[symbol] = {
            "price": stat.get("lastPrice", ""),
            "best_bid": "",
            "best_bid_size": "0",
            "best_ask": "",
            "best_ask_size": "0",
            "side": "",
            "last_size": stat.get("lastQty", "0"),
            "volume_24h": stat.get("volume", "0"),
            "time": _iso_ms(stat.get("closeTime")),
        }

    for book in books or []:
        symbol = str(book.get("symbol") or "").upper()
        quote = board.get(symbol)
        if quote is None:
            continue
        quote["best_bid"] = book.get("bidPrice", "")
        quote["best_bid_size"] = book.get("bidQty", "0")
        quote["best_ask"] = book.get("askPrice", "")
        quote["best_ask_size"] = book.get("askQty", "0")

    return board


async def _poll_once(
    client: BinanceRESTClient,
    symbols: list[str],
    price_book: PriceBook,
    candle_book: CandleBook,
    venue: BinanceVenue = SPOT_VENUE,
    products: dict[str, str] | None = None,
) -> None:
    """One poll cycle: multi-timeframe candles plus the current price for each symbol."""
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    mapping = products or {}

    board: dict[str, dict] = {}
    if venue.is_perp:
        board = await _futures_price_board(client)

    for symbol in symbols:
        product_id = mapping.get(symbol, venue.product_id_for(symbol))
        try:
            await _refresh_candles(client, symbol, product_id, candle_book, now_ms)

            if venue.is_perp:
                quote = board.get(symbol.upper())
                if quote is None:
                    continue
                price_book.update({"product_id": product_id, **quote})
                continue

            resp = await client.get_24h_ticker(symbol)
            price_book.update({
                "product_id": product_id,
                "price": resp["lastPrice"],
                "best_bid": resp["bidPrice"],
                "best_bid_size": resp["bidQty"],
                "best_ask": resp["askPrice"],
                "best_ask_size": resp["askQty"],
                "side": "",
                "last_size": resp["lastQty"],
                "volume_24h": resp["volume"],
                "time": datetime.fromtimestamp(
                    resp["closeTime"] / 1000, tz=timezone.utc
                ).isoformat(),
            })
        except Exception:
            logger.exception("REST poll failed for %s", symbol)


async def poll_rest(
    symbols: list[str],
    price_book: PriceBook,
    candle_book: CandleBook,
    interval: float = 60.0,
    venue: BinanceVenue = SPOT_VENUE,
    products: dict[str, str] | None = None,
) -> None:
    """Poll the venue's REST API for multi-timeframe candles and current prices."""
    async with BinanceRESTClient(venue=venue) as client:
        while True:
            await _poll_once(
                client,
                symbols,
                price_book,
                candle_book,
                venue=venue,
                products=products,
            )
            await asyncio.sleep(interval)


class BinanceKafkaConnector:
    """Streams Binance ticker data to an Agent via Kafka.

    Connects to the Binance Exchange WebSocket ticker stream
    and invokes the configured Agent with each price update
    using fire-and-forget publishes via client.agent(topic=...).send().

    When min_publish_interval is set, incoming tickers are buffered per
    symbol. Only the latest data for each symbol is kept. A symbol's
    buffer is flushed once at least min_publish_interval seconds have
    elapsed since that symbol's last invocation.
    """

    def __init__(
        self,
        client: Client,
        agent_topic: str,
        symbols: list[str],
        fee_model: FeeModel,
        min_publish_interval: float = 0.0,
        candle_book: CandleBook | None = None,
        venue: BinanceVenue = SPOT_VENUE,
        board_interval: float = 0.0,
        lean_topic: str | None = None,
        rest_feed_only: bool = False,
    ) -> None:
        if not symbols:
            raise ValueError("At least one symbol must be specified")
        self._client = client
        # Mint the fan-out gateway once. The topic= escape hatch targets the shared
        # broadcast topic (all agents subscribe under distinct consumer groups), NOT a
        # per-agent private topic — see docs/calfkit-0.12-migration.md, constraint #2.
        self._agent_gw = client.agent(topic=agent_topic)
        # Second fan-out gateway for the lean profile (scalper payload).
        self._lean_gw = client.agent(topic=lean_topic) if lean_topic else None
        self._venue = venue
        # `_symbols` stays the venue's own symbols: both the WS stream names and the
        # REST paths are the venue's vocabulary. Everything agent- or ledger-facing
        # speaks product ids instead, hence the mapping (design D1/D7).
        self._symbols = symbols
        self._products = {s.upper(): venue.product_id_for(s) for s in symbols}
        self._min_interval = min_publish_interval
        self._running = True
        self._candle_book = candle_book
        # Fixed for the connector's lifetime, so build the agent-facing line once.
        self._fee_disclosure = fee_model.disclosure_prompt(perp=venue.is_perp)

        # Latest ticker per symbol — patched on every incoming message
        self._latest: dict[str, TickerMessage] = {}
        # REST board as the fallback data path. `_ws_last_frame` is 0 until the socket
        # delivers something, which is what lets `_on_board_quote` tell "the socket is
        # just quiet between ticks" from "this network has no socket at all".
        self._board_interval = board_interval
        self._rest_feed_only = rest_feed_only
        self._ws_last_frame = 0.0
        self._board_noted = False

    async def start(self) -> None:
        """Start the connector. Blocks until shutdown is triggered."""
        await self._client.broker.start()
        logger.info("Kafka broker connected")

        try:
            if self._rest_feed_only:
                await self._run_rest_feed()
                return
            while self._running:
                try:
                    await self._consume_and_publish()
                except websockets.ConnectionClosed:
                    if not self._running:
                        break
                    logger.warning(
                        "WebSocket connection lost. Reconnecting in %ds...",
                        RECONNECT_DELAY_SECONDS,
                    )
                    await asyncio.sleep(RECONNECT_DELAY_SECONDS)
                except Exception:
                    if not self._running:
                        break
                    logger.exception(
                        "Unexpected error. Reconnecting in %ds...",
                        RECONNECT_DELAY_SECONDS,
                    )
                    await asyncio.sleep(RECONNECT_DELAY_SECONDS)
        finally:
            await self._client.aclose()
            logger.info("Kafka broker closed")

    def stop(self) -> None:
        """Signal the connector to shut down gracefully."""
        self._running = False

    async def _run_rest_feed(self) -> None:
        """USD-M ticker streams omit the executable bid and ask used by live gates."""
        tasks = [
            asyncio.create_task(self._periodic_publish()),
            asyncio.create_task(poll_ticker_board(
                symbols=self._symbols, on_quote=self._on_board_quote,
                interval=self._board_interval, venue=self._venue, products=self._products,
            )),
        ]
        if self._candle_book is not None:
            tasks.append(asyncio.create_task(poll_rest(
                symbols=self._symbols, price_book=PriceBook(), candle_book=self._candle_book,
                interval=60.0, venue=self._venue, products=self._products,
            )))
        try:
            while self._running:
                await asyncio.sleep(1)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    def _parse_binance_ticker(self, data: dict) -> TickerMessage | None:
        """Parse Binance 24hr ticker data into TickerMessage."""
        try:
            symbol = data["s"]
            return TickerMessage(
                # fstream's 24hrTicker carries the same fields as spot's, so the parser is
                # shared; only the product id differs (SOLUSDT vs SOLUSDT-PERP).
                product_id=self._products.get(str(symbol).upper(), symbol),
                price=data["c"],
                best_bid=data["b"],
                best_bid_size=data["B"],
                best_ask=data["a"],
                best_ask_size=data["A"],
                side="",
                last_size=data.get("Q", "0"),
                open_24h=data["o"],
                high_24h=data["h"],
                low_24h=data["l"],
                volume_24h=data["v"],
                volume_30d="0",
                trade_id=data.get("n", 0),
                sequence=0,
                time=datetime.fromtimestamp(
                    data["E"] / 1000, tz=timezone.utc
                ).isoformat(),
                source=(
                    self._venue.name if self._venue.is_perp
                    and valid_futures_touch(data["b"], data["a"]) else None
                ),
            )
        except Exception as e:
            logger.error("Failed to parse ticker data: %s (data: %s)", e, data)
            return None

    async def _publish_latest(self) -> None:
        """Snapshot and publish the current latest tickers as a single batch."""
        if not self._latest:
            return

        batch = list(self._latest.values())
        _exclude = {
            "best_bid_size",
            "best_ask_size",
            "last_size",
            "side",
            "trade_id",
            "sequence",
            "open_24h",
            "high_24h",
            "low_24h",
            "volume_24h",
            "volume_30d",
            "time",
            "source",
        }
        batch_json = json.dumps([t.model_dump(exclude=_exclude) for t in batch])

        await self._publish_profiles(batch_json)

        summary = ", ".join(f"{t.product_id} @ ${t.price}" for t in batch)
        logger.info(
            "Published batch of %d ticker(s) to agent: %s",
            len(batch),
            summary,
        )

    async def _publish_profiles(self, batch_json: str) -> None:
        """Fan the batch out to each subscribed profile (full book, and lean if asked)."""
        deps = {"invoked_at": time.time()}
        products = list(self._products.values())
        await self._agent_gw.send(
            build_market_message(
                fee_disclosure=self._fee_disclosure,
                tickers_json=batch_json,
                candle_book=self._candle_book,
                product_ids=products,
            ),
            deps=deps,
        )
        if self._lean_gw is not None:
            await self._lean_gw.send(
                build_market_message(
                    fee_disclosure=self._fee_disclosure,
                    tickers_json=batch_json,
                    candle_book=self._candle_book,
                    product_ids=products,
                    profile="lean",
                ),
                deps=deps,
            )

    async def _periodic_publish(self) -> None:
        """Publish the latest snapshot on a fixed interval."""
        interval = max(self._min_interval, 1.0)
        while self._running:
            await asyncio.sleep(interval)
            try:
                await self._publish_latest()
            except Exception:
                # A transient broker/publish error must NOT kill this loop: otherwise
                # agents stop being invoked for the lifetime of the WebSocket connection
                # (a silent trading halt while prices keep flowing to the dashboard).
                # Log and retry on the next interval.
                logger.exception(
                    "Periodic agent invoke failed; agents were not invoked this cycle. "
                    "Retrying in %.0fs.", interval,
                )

    async def _ping_loop(self, ws: websockets.ClientConnection) -> None:
        """Send ping frames periodically to keep connection alive."""
        while self._running:
            try:
                await asyncio.sleep(PING_INTERVAL_SECONDS)
                if ws.state.name == "OPEN":
                    await ws.ping()
                    logger.debug("Ping sent")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("Ping failed: %s", e)

    async def _lifetime_manager(self, ws: websockets.ClientConnection) -> None:
        """Force reconnection before 24-hour limit."""
        await asyncio.sleep(MAX_CONNECTION_LIFETIME_SECONDS)
        logger.info("Connection approaching 24h limit, forcing reconnection")
        await ws.close()

    async def _connect_with_fallback(self, streams: str) -> websockets.ClientConnection:
        """Connect to the venue's WebSocket with fallback endpoints."""
        urls_to_try = [f"{base}/stream?streams={streams}" for base in self._venue.ws_urls]

        last_error = None
        for url in urls_to_try:
            try:
                logger.info("Connecting to Binance WebSocket: %s", url.replace(streams, "..."))
                ws = await websockets.connect(url, open_timeout=10)
                logger.info("Successfully connected to %s", url.split("/")[2])
                return ws
            except (ConnectionRefusedError, TimeoutError, OSError) as e:
                logger.warning("Failed to connect to %s: %s", url.split("/")[2], e)
                last_error = e
                continue

        raise last_error if last_error else ConnectionError("All Binance endpoints failed")

    async def _on_board_quote(self, ticker: TickerMessage) -> None:
        """Take one REST-board ticker, unless the WebSocket is demonstrably alive.

        The socket delivers a tick per second per symbol, so a REST row that arrives
        while frames are flowing would only be staler than what is already buffered.
        Silence for two board cycles means the socket is not coming back on its own and
        the board becomes the feed.
        """
        if self._board_interval <= 0:
            return
        if self._ws_last_frame and (time.time() - self._ws_last_frame) < 2 * self._board_interval:
            return
        if not self._board_noted:
            self._board_noted = True
            if self._rest_feed_only:
                logger.info("Publishing live %s REST order-book quotes", self._venue.name)
            else:
                logger.warning(
                    "No WebSocket frame from %s; publishing from the REST ticker board "
                    "every %.0fs instead", self._venue.name, self._board_interval,
                )
        self._latest[ticker.product_id] = ticker
        await self._client.broker.publish(ticker, PRICE_TOPIC)

    async def _consume_and_publish(self) -> None:
        """Connect to Binance WebSocket and buffer tickers for periodic publish."""
        self._latest.clear()

        streams = "/".join(f"{s.lower()}@ticker" for s in self._symbols)

        ws = await self._connect_with_fallback(streams)
        async with ws:
            logger.info(
                "Subscribed to %d %s symbols on 24hrTicker: %s",
                len(self._symbols),
                self._venue.label,
                ", ".join(self._symbols),
            )

            flush_task = asyncio.create_task(self._periodic_publish())
            ping_task = asyncio.create_task(self._ping_loop(ws))
            lifetime_task = asyncio.create_task(self._lifetime_manager(ws))

            candle_task: asyncio.Task | None = None
            if self._candle_book is not None:
                candle_task = asyncio.create_task(
                    poll_rest(
                        symbols=self._symbols,
                        price_book=PriceBook(),
                        candle_book=self._candle_book,
                        interval=60.0,
                        venue=self._venue,
                        products=self._products,
                    )
                )

            board_task: asyncio.Task | None = None
            if self._board_interval > 0:
                board_task = asyncio.create_task(
                    poll_ticker_board(
                        symbols=self._symbols,
                        on_quote=self._on_board_quote,
                        interval=self._board_interval,
                        venue=self._venue,
                        products=self._products,
                    )
                )

            try:
                # recv() with a timeout, not `async for`: a socket that connects and then
                # never speaks (a blackholed `fstream`, an idle venue) must still notice
                # SIGTERM, and `async for` would sit on that await forever — leaving a
                # zombie connector that republishes the moment the feed returns.
                while self._running:
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
                    except asyncio.TimeoutError:
                        continue
                    if not self._running:
                        break

                    try:
                        data = json.loads(raw)
                    except json.JSONDecodeError:
                        logger.warning("Failed to decode message: %s", raw[:200])
                        continue

                    # Handle ping/pong (Binance-specific)
                    if "ping" in data:
                        await ws.send(json.dumps({"pong": data["ping"]}))
                        continue

                    # Handle combined stream wrapper
                    if "stream" in data and "data" in data:
                        ticker_data = data["data"]
                    else:
                        ticker_data = data

                    # Only process 24hrTicker events
                    if ticker_data.get("e") != "24hrTicker":
                        continue

                    ticker = self._parse_binance_ticker(ticker_data)
                    if ticker is None:
                        continue

                    self._ws_last_frame = time.time()
                    self._latest[ticker.product_id] = ticker
                    await self._client.broker.publish(ticker, PRICE_TOPIC)
            finally:
                flush_task.cancel()
                ping_task.cancel()
                lifetime_task.cancel()
                try:
                    await flush_task
                except asyncio.CancelledError:
                    pass
                try:
                    await ping_task
                except asyncio.CancelledError:
                    pass
                try:
                    await lifetime_task
                except asyncio.CancelledError:
                    pass
                if candle_task is not None:
                    candle_task.cancel()
                    try:
                        await candle_task
                    except asyncio.CancelledError:
                        pass
                if board_task is not None:
                    board_task.cancel()
                    try:
                        await board_task
                    except asyncio.CancelledError:
                        pass


def parse_args(
    argv: list[str] | None = None, venue: BinanceVenue = SPOT_VENUE
) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog=f"python -m exchanges.{venue.name}",
        description=f"Stream Binance {venue.label} market data to a Kafka topic.",
    )
    parser.add_argument(
        "--bootstrap-servers",
        default=os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092"),
        help="Kafka bootstrap servers (default: $KAFKA_BOOTSTRAP_SERVERS or localhost:9092).",
    )
    parser.add_argument(
        "--config",
        default="config.json",
        help="Path to config file for symbols (default: config.json).",
    )
    parser.add_argument(
        "--symbols",
        nargs="+",
        default=None,
        help="Binance symbols to subscribe to (overrides config).",
    )
    parser.add_argument(
        "--min-interval",
        type=float,
        default=60.0,
        help=(
            "Minimum seconds between publishes per symbol. "
            "Incoming data is buffered and only the latest per symbol is published "
            "when the interval elapses. 0 = publish immediately "
            "(default: 60s; a shorter interval spends the agent's LLM budget faster)."
        ),
    )
    parser.add_argument(
        "--board-interval",
        type=float,
        default=None,
        help=(
            "Seconds between REST ticker-board polls, used when the WebSocket is not "
            "delivering or when live futures uses the REST feed. 0 disables the fallback. "
            "(default: 3 seconds for live futures, 10 otherwise)"
        ),
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging level (default: INFO).",
    )
    return parser.parse_args(argv)


async def run(
    args: argparse.Namespace,
    agent_topic: str = AGENT_INPUT_TOPIC,
    venue: BinanceVenue = SPOT_VENUE,
    lean_topic: str | None = AGENT_INPUT_TOPIC_LEAN,
) -> None:
    # Load the fee rate (and symbols, when not given via CLI) from config, which
    # is the single source of truth shared with the tools node. load_config_strict
    # fails loud if the file exists but is invalid, rather than guessing the fee.
    from config import load_config_strict

    config = load_config_strict(args.config)
    trading = config.trading
    rest_feed_only = (
        venue.is_perp and getattr(trading, "mode", "paper") == "live"
        and trading.live.market == "perp"
    )
    board_interval = getattr(args, "board_interval", None)
    if board_interval is None:
        board_interval = 3.0 if rest_feed_only else 10.0
    if rest_feed_only and not (math.isfinite(board_interval) and board_interval > 0):
        raise ValueError("Live futures requires a positive --board-interval for order-book quotes")
    if venue.is_perp:
        if rest_feed_only:
            account_name = next(iter(trading.live.agent_accounts.values()))
            if trading.live.accounts[account_name].testnet:
                venue = PERP_DEMO_VENUE
        # `binance_perp_symbols` lands with the perp config change; getattr keeps this
        # entrypoint startable against a config that predates it.
        configured = getattr(trading, "binance_perp_symbols", None) or []
    else:
        configured = trading.binance_symbols
    symbols = args.symbols or configured or list(DEFAULT_SYMBOLS)
    fee_model = FeeModel(
        taker_bps=trading.fees.taker_bps,
        perp_taker_bps=getattr(trading.fees, "perp_taker_bps", DEFAULT_PERP_TAKER_BPS),
    )

    candle_book = CandleBook(parse_row=parse_binance_candle)
    # Bind a shared, durable invoker inbox so agent step-events are observable by the
    # standalone response_viewer via client.events() (Phase 2 topology).
    client = Client.connect(args.bootstrap_servers, inbox_topic=AGENT_OUTPUT_TOPIC)
    connector = BinanceKafkaConnector(
        client=client,
        agent_topic=agent_topic,
        symbols=symbols,
        fee_model=fee_model,
        min_publish_interval=args.min_interval,
        lean_topic=lean_topic,
        candle_book=candle_book,
        venue=venue,
        board_interval=board_interval,
        rest_feed_only=rest_feed_only,
    )

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, connector.stop)

    logger.info("Starting Binance %s -> Kafka connector", venue.label)
    logger.info("  Agent topic: %s", agent_topic)
    logger.info("  Lean topic:  %s", lean_topic or "disabled")
    logger.info("  Broker:      %s", args.bootstrap_servers)
    logger.info("  Symbols:     %s", ", ".join(symbols))
    logger.info("  Products:    %s", ", ".join(venue.product_id_for(s) for s in symbols))
    logger.info("  Min interval: %ss", args.min_interval)
    logger.info(
        "  REST board:  %s",
        f"every {board_interval:g}s (fallback when the socket is silent)"
        if board_interval > 0
        else "disabled",
    )
    taker_bps = fee_model.taker_bps_for(perp=venue.is_perp)
    logger.info("  Taker fee:   %d bps (%.2f%%)", taker_bps, taker_bps / 100)

    await connector.start()


def main() -> None:
    args = parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
        datefmt="%H:%M:%S",
    )
    # Fire-and-forget producer bound to the shared inbox (agent_router.output): quiet the
    # per-reply "no pending handle" notices so they don't drown genuine connector logs.
    quiet_shared_inbox_reply_log()

    asyncio.run(run(args))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
