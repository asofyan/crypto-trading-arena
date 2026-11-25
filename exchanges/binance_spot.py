"""Binance Spot trading API — signed REST only, market orders only.

Separate from ``exchanges.binance`` on purpose: that module is the read-only market
data connector (WebSocket ticker + REST candles) and never holds a key. This one owns
the account and the order lifecycle, and it is deliberately *synchronous* because it
runs inside an agent tool node, which executes on a worker thread of an asyncio app —
mixing `asyncio.run`/event loops in there would deadlock the tool.

Scope is the minimum that a taker-only spot bot needs:
``GET /api/v3/time|account|exchangeInfo|order|openOrders|myTrades`` and
``POST /api/v3/order``. There is no user-data stream: a MARKET order's own response
already carries ``fills[]`` with the commission, and anything the response cannot
settle is resolved by ``client_order_id`` on the next startup.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import urlencode

import httpx

log = logging.getLogger(__name__)

BINANCE_SPOT_REST = "https://api.binance.com"
BINANCE_SPOT_TESTNET_REST = "https://testnet.binance.vision"

# Fallbacks used until exchangeInfo's rateLimits are applied by configure_rate_limits.
DEFAULT_ORDER_LIMIT = 100
DEFAULT_ORDER_WINDOW_S = 10.0
DEFAULT_WEIGHT_LIMIT = 6000

# Binance error codes this bot reacts to differently. Everything else is a plain
# rejection whose message is already readable.
CODE_TIMESTAMP_OUT_OF_SYNC = -1021
CODE_INSUFFICIENT_BALANCE = -2010
CODE_ORDER_NOT_FOUND = -2013
CODE_FILTER_FAILURE = -1013
CODE_RATE_LIMITED = -1003
CODE_INVALID_SIGNATURE = -1022


class BinanceError(Exception):
    """A venue answer we could not turn into a fill."""


class BinanceApiError(BinanceError):
    def __init__(self, code: int | None, message: str, http_status: int | None = None) -> None:
        self.code = code
        self.message = message
        self.http_status = http_status
        super().__init__(f"[{code}] {message}" if code is not None else message)


class InsufficientBalance(BinanceApiError):
    """The venue says the wallet cannot fund this order."""


class OrderFilterError(BinanceApiError):
    """LOT_SIZE / NOTIONAL / PRICE_FILTER refusal: the size is not legal."""


class RateLimited(BinanceError):
    def __init__(self, retry_after: float, message: str = "rate limited by the venue") -> None:
        self.retry_after = retry_after
        super().__init__(f"{message} (retry after {retry_after:.1f}s)")


class IpBanned(BinanceError):
    """HTTP 418: Binance has banned this IP. Every further order makes it worse."""

    def __init__(self, retry_after: float) -> None:
        self.retry_after = retry_after
        super().__init__(f"IP banned by the venue (retry after {retry_after:.1f}s)")


class UnknownOutcome(BinanceError):
    """Network/timeout: the order may or may not exist at the venue.

    Never retried blindly — the caller journals ``UNKNOWN`` and lets startup
    reconciliation resolve it, because a second POST could double the position.
    """


@dataclass(frozen=True)
class Balance:
    free: float
    locked: float

    @property
    def total(self) -> float:
        return self.free + self.locked


@dataclass(frozen=True)
class VenueFill:
    """What the venue reports about one order, normalised for bookkeeping."""

    client_order_id: str
    venue_order_id: str
    symbol: str
    side: str
    status: str  # FILLED | PARTIALLY_FILLED | EXPIRED | CANCELED | REJECTED
    executed_qty: float
    # Base quantity the wallet actually received. On a buy whose commission is charged
    # in the base asset this is *less* than executed_qty, and booking executed_qty would
    # leave the ledger holding SOL the wallet does not have.
    credited_qty: float
    cum_notional: float
    price: float
    fee: float  # quote-denominated, best effort
    fee_asset: str | None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def is_filled(self) -> bool:
        return self.status in ("FILLED", "PARTIALLY_FILLED") and self.executed_qty > 0


def _as_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _normalise_fill(
    payload: dict[str, Any],
    *,
    base_asset: str,
    quote_asset: str,
    trades: list[dict[str, Any]] | None = None,
) -> VenueFill:
    """Turn an order payload (+ optional myTrades) into quote-denominated fees.

    Binance charges commission in the asset the fee is denominated in: on a buy that is
    usually the *received base* asset (unless BNB is funded and the burn toggle is on),
    on a sell the quote asset. Booking the raw number would count 0.0001 SOL as
    $0.0001, so each commission is converted at the order's average price.
    """
    executed = _as_float(payload.get("executedQty"))
    cum_notional = _as_float(payload.get("cummulativeQuoteQty"))
    avg_price = cum_notional / executed if executed > 0 else _as_float(payload.get("price"))

    entries = trades if trades is not None else payload.get("fills") or []
    fee_quote = 0.0
    fee_assets: dict[str, float] = {}
    base_commission = 0.0
    for entry in entries:
        commission = _as_float(entry.get("commission"))
        asset = str(entry.get("commissionAsset") or "")
        if not commission:
            continue
        if asset == quote_asset:
            fee_quote += commission
        elif asset == base_asset:
            fee_quote += commission * avg_price
            base_commission += commission
        else:
            # BNB (or anything else the account happens to hold): value it at the
            # order's own price. It is an approximation, and fee_asset records it.
            price_hint = _as_float(entry.get("price")) or avg_price
            fee_quote += commission * price_hint
        fee_assets[asset] = fee_assets.get(asset, 0.0) + commission

    fee_asset: str | None
    if not fee_assets:
        fee_asset = None
    elif len(fee_assets) == 1:
        fee_asset = next(iter(fee_assets))
    else:
        fee_asset = "MIXED"

    return VenueFill(
        client_order_id=str(payload.get("clientOrderId") or ""),
        venue_order_id=str(payload.get("orderId") or ""),
        symbol=str(payload.get("symbol") or ""),
        side=str(payload.get("side") or ""),
        status=str(payload.get("status") or ""),
        executed_qty=executed,
        credited_qty=max(0.0, executed - base_commission),
        cum_notional=cum_notional,
        price=avg_price,
        fee=fee_quote,
        fee_asset=fee_asset,
        raw=payload,
    )


class BinanceSpotClient:
    """Signed Binance Spot REST client with local budget + venue error mapping."""

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        *,
        base_url: str = BINANCE_SPOT_REST,
        recv_window_ms: int = 5000,
        timeout: float = 10.0,
        transport: httpx.BaseTransport | None = None,
        now: Callable[[], float] = time.time,
    ) -> None:
        if not api_key or not api_secret:
            raise ValueError("Binance spot credentials are required (api_key/api_secret)")
        self._api_key = api_key
        self._api_secret = api_secret.encode()
        self.base_url = base_url.rstrip("/")
        self._recv_window_ms = recv_window_ms
        self._now = now
        self._offset_ms = 0.0
        self._http = httpx.Client(timeout=timeout, transport=transport)
        # Local budget, seeded from exchangeInfo by configure_rate_limits.
        self._order_limit = DEFAULT_ORDER_LIMIT
        self._order_window_s = DEFAULT_ORDER_WINDOW_S
        self._order_times: deque[float] = deque()
        self._weight_limit = DEFAULT_WEIGHT_LIMIT
        self._used_weight = 0
        self._asset_pairs: dict[str, tuple[str, str]] = {}

    # ── Lifecycle ────────────────────────────────────────────────

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> BinanceSpotClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # ── Time ─────────────────────────────────────────────────────

    def server_time_ms(self) -> int:
        response = self._http.get(f"{self.base_url}/api/v3/time")
        self._raise_for_status(response)
        return int(response.json()["serverTime"])

    def sync_time(self) -> float:
        """Record how far the local clock runs ahead of the venue (-1021 territory).

        ``_timestamp_ms`` subtracts this, so every signed request carries venue time
        even when the host clock has drifted.
        """
        offset = self._now() * 1000.0 - self.server_time_ms()
        self._offset_ms = offset
        if abs(offset) > 1000:
            log.warning("binance_spot: local clock is %.0fms off the venue", offset)
        return offset

    def _timestamp_ms(self) -> int:
        return int(self._now() * 1000.0 - self._offset_ms)

    # ── Rate limits ──────────────────────────────────────────────

    def configure_rate_limits(self, exchange_info: dict[str, Any]) -> None:
        """Adopt the venue's own limits instead of hardcoding numbers."""
        for limit in exchange_info.get("rateLimits") or []:
            kind = limit.get("rateLimitType")
            interval = limit.get("interval")
            interval_num = _as_float(limit.get("intervalNum")) or 1.0
            count = _as_float(limit.get("limit"))
            if not count:
                continue
            if kind == "ORDERS" and interval == "SECOND":
                # Only the tightest short window matters; the daily cap is far away.
                self._order_window_s = interval_num
                self._order_limit = int(count)
            elif kind == "REQUEST_WEIGHT" and interval == "MINUTE":
                self._weight_limit = int(count)
        log.debug(
            "binance_spot limits: %d orders / %.0fs, %d weight / minute",
            self._order_limit, self._order_window_s, self._weight_limit,
        )

    def _consume_order_budget(self) -> None:
        """Refuse locally before the venue does: a 429 is slower and riskier."""
        now = self._now()
        while self._order_times and self._order_times[0] <= now - self._order_window_s:
            self._order_times.popleft()
        if len(self._order_times) >= self._order_limit:
            raise RateLimited(
                self._order_times[0] + self._order_window_s - now,
                f"local order budget spent ({self._order_limit} per "
                f"{self._order_window_s:.0f}s)",
            )
        self._order_times.append(now)

    # ── Signed requests ──────────────────────────────────────────

    def _sign(self, params: dict[str, Any]) -> str:
        query = urlencode(sorted((k, v) for k, v in params.items() if v is not None))
        signature = hmac.new(self._api_secret, query.encode(), hashlib.sha256).hexdigest()
        return f"{query}&signature={signature}"

    def _signed(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        retry_on_timestamp: bool = True,
    ) -> Any:
        payload = dict(params or {})
        payload["timestamp"] = self._timestamp_ms()
        payload["recvWindow"] = self._recv_window_ms
        url = f"{self.base_url}{path}?{self._sign(payload)}"
        try:
            response = self._http.request(method, url, headers={"X-MBX-APIKEY": self._api_key})
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            # GET and POST are both unresolvable from here; the caller decides whether
            # "unknown" means retry (read) or journal-and-stop (order).
            raise UnknownOutcome(f"{method} {path}: {exc}") from exc

        if response.status_code == 200:
            self._track_weight(response)
            return response.json()

        error = self._error_from(response)
        if isinstance(error, BinanceApiError) and error.code == CODE_TIMESTAMP_OUT_OF_SYNC:
            if retry_on_timestamp:
                log.warning("binance_spot: timestamp rejected (-1021), resyncing clock")
                self.sync_time()
                return self._signed(method, path, params, retry_on_timestamp=False)
        raise error

    def _track_weight(self, response: httpx.Response) -> None:
        raw = response.headers.get("X-MBX-USED-WEIGHT-1M")
        if not raw:
            return
        try:
            self._used_weight = int(raw)
        except ValueError:
            return
        if self._used_weight >= self._weight_limit * 0.9:
            # Not fatal yet, but the next burst of reads would be: make it visible
            # before the venue answers 429 with no explanation.
            log.warning(
                "binance_spot: %d/%d request weight used this minute",
                self._used_weight, self._weight_limit,
            )

    def _error_from(self, response: httpx.Response) -> BinanceError:
        retry_after = _as_float(response.headers.get("Retry-After")) or 60.0
        if response.status_code == 429:
            return RateLimited(retry_after, f"HTTP 429 from {response.request.url.path}")
        if response.status_code == 418:
            return IpBanned(retry_after)

        code: int | None = None
        message = response.text
        try:
            body = response.json()
            code = body.get("code", code)
            message = body.get("msg", message)
        except ValueError:
            pass

        if code == CODE_RATE_LIMITED:
            return RateLimited(retry_after, message)
        if code == CODE_INSUFFICIENT_BALANCE:
            return InsufficientBalance(code, message, response.status_code)
        if code == CODE_FILTER_FAILURE:
            return OrderFilterError(code, message, response.status_code)
        return BinanceApiError(code, message, response.status_code)

    def _raise_for_status(self, response: httpx.Response) -> None:
        if response.status_code != 200:
            raise self._error_from(response)

    # ── Account & instruments ───────────────────────────────────

    def balances(self) -> dict[str, Balance]:
        payload = self._signed("GET", "/api/v3/account")
        balances: dict[str, Balance] = {}
        for entry in payload.get("balances") or []:
            free = _as_float(entry.get("free"))
            locked = _as_float(entry.get("locked"))
            if free or locked:
                balances[str(entry.get("asset"))] = Balance(free=free, locked=locked)
        return balances

    def exchange_info(self, symbols: list[str] | None = None) -> dict[str, Any]:
        params = {}
        if symbols:
            # Compact separators: the venue answers 400 when the JSON array is
            # encoded with spaces after the commas.
            params["symbols"] = json.dumps(
                [s.strip().upper() for s in symbols], separators=(",", ":")
            )
        response = self._http.get(f"{self.base_url}/api/v3/exchangeInfo", params=params)
        self._raise_for_status(response)
        payload = response.json()
        self.configure_rate_limits(payload)
        return payload

    # ── Orders ──────────────────────────────────────────────────

    def place_market_buy(
        self, symbol: str, quote_qty: float, client_order_id: str
    ) -> VenueFill:
        """Spend an exact amount of the quote asset (``quoteOrderQty``).

        Sizing in quote terms is what makes percentage-of-equity caps meaningful: the
        agent asks for $10 of SOL, not for a quantity that costs $10 at some price it
        saw a second earlier.
        """
        return self._place_order(
            symbol=symbol,
            side="BUY",
            client_order_id=client_order_id,
            quote_qty=quote_qty,
        )

    def place_market_sell(self, symbol: str, quantity: float, client_order_id: str) -> VenueFill:
        return self._place_order(
            symbol=symbol,
            side="SELL",
            client_order_id=client_order_id,
            quantity=quantity,
        )

    def _place_order(
        self,
        *,
        symbol: str,
        side: str,
        client_order_id: str,
        quantity: float | None = None,
        quote_qty: float | None = None,
    ) -> VenueFill:
        self._consume_order_budget()
        params: dict[str, Any] = {
            "symbol": symbol.upper(),
            "side": side,
            "type": "MARKET",
            "newClientOrderId": client_order_id,
            # MARKET forces FULL on Binance, but state it: the fills[] array is what
            # the bookkeeping needs, and a silent default change would break it.
            "newOrderRespType": "FULL",
        }
        if quote_qty is not None:
            params["quoteOrderQty"] = f"{quote_qty:.8f}".rstrip("0").rstrip(".")
        if quantity is not None:
            params["quantity"] = f"{quantity:.8f}".rstrip("0").rstrip(".")

        return self._place_and_normalise("/api/v3/order", params)

    def _place_and_normalise(self, path: str, params: dict[str, Any]) -> VenueFill:
        payload = self._signed("POST", path, params)
        symbol = str(payload.get("symbol") or params.get("symbol") or "").upper()
        base, quote = self._split_symbol(symbol)
        return _normalise_fill(payload, base_asset=base, quote_asset=quote)

    def _split_symbol(self, symbol: str) -> tuple[str, str]:
        """Base/quote from the symbol's own assets, never by string slicing."""
        cached = self._asset_pairs.get(symbol.upper())
        if cached:
            return cached
        # Unknown symbol: assume the venue's dominant quote. It only affects how a
        # commission is converted, and a wrong guess is logged rather than hidden.
        if symbol.upper().endswith("USDT"):
            return symbol.upper()[:-4], "USDT"
        return symbol.upper(), ""

    def get_order(self, symbol: str, client_order_id: str) -> VenueFill | None:
        """Look an order up by *our* id. ``None`` means the venue never saw it."""
        try:
            payload = self._signed(
                "GET",
                "/api/v3/order",
                {"symbol": symbol.upper(), "origClientOrderId": client_order_id},
            )
        except BinanceApiError as exc:
            if exc.code == CODE_ORDER_NOT_FOUND:
                return None
            raise
        base, quote = self._split_symbol(symbol.upper())
        trades = None
        if payload.get("orderId"):
            # A GET has no fills[]; myTrades is the only way to recover the commission.
            trades = self.my_trades(symbol, order_id=int(payload["orderId"]))
        return _normalise_fill(payload, base_asset=base, quote_asset=quote, trades=trades)

    def open_orders(self, symbol: str | None = None) -> list[dict[str, Any]]:
        params = {"symbol": symbol.upper()} if symbol else {}
        payload = self._signed("GET", "/api/v3/openOrders", params or None)
        return list(payload or [])

    def my_trades(self, symbol: str, *, order_id: int | None = None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"symbol": symbol.upper()}
        if order_id is not None:
            params["orderId"] = order_id
        payload = self._signed("GET", "/api/v3/myTrades", params)
        return list(payload or [])

    def remember_symbols(self, exchange_info: dict[str, Any]) -> None:
        for entry in exchange_info.get("symbols") or []:
            if isinstance(entry, dict) and entry.get("symbol"):
                self._asset_pairs[str(entry["symbol"]).upper()] = (
                    str(entry.get("baseAsset") or ""),
                    str(entry.get("quoteAsset") or ""),
                )
