"""Signed Binance Spot client, exercised through httpx.MockTransport (no network).

The signing, the error mapping and the fee normalisation are the three places where a
mistake costs real money, so each is pinned here rather than trusted.
"""

from __future__ import annotations

import hashlib
import hmac
from urllib.parse import parse_qsl

import httpx
import pytest

from exchanges.binance_spot import (
    BinanceApiError,
    BinanceSpotClient,
    InsufficientBalance,
    IpBanned,
    OrderFilterError,
    RateLimited,
    UnknownOutcome,
)

API_KEY = "key-123"
API_SECRET = "secret-456"


class Clock:
    """Controllable clock: the client takes `now` so timestamps are deterministic."""

    def __init__(self, start: float = 1_764_000_000.0) -> None:
        self.value = start

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def _client(handler, clock: Clock | None = None) -> BinanceSpotClient:
    return BinanceSpotClient(
        API_KEY,
        API_SECRET,
        transport=httpx.MockTransport(handler),
        now=clock or Clock(),
    )


def _order_payload(**overrides) -> dict:
    payload = {
        "symbol": "SOLUSDT",
        "orderId": 4242,
        "clientOrderId": "arena-a-1",
        "transactTime": 1_764_000_000_000,
        "price": "0.00000000",
        "origQty": "0.00000000",
        "executedQty": "0.10000000",
        "cummulativeQuoteQty": "10.02000000",
        "status": "FILLED",
        "timeInForce": "GTC",
        "type": "MARKET",
        "side": "BUY",
        "fills": [
            {
                "price": "100.10000000",
                "qty": "0.06000000",
                "commission": "0.00006000",
                "commissionAsset": "SOL",
                "tradeId": 1,
            },
            {
                "price": "100.30000000",
                "qty": "0.04000000",
                "commission": "0.00004000",
                "commissionAsset": "SOL",
                "tradeId": 2,
            },
        ],
    }
    payload.update(overrides)
    return payload


def _error(code: int, msg: str, http_status: int = 400) -> httpx.Response:
    return httpx.Response(http_status, json={"code": code, "msg": msg})


# ── Signing ─────────────────────────────────────────────────────


def test_requests_are_hmac_signed_with_the_api_key_header():
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["query"] = request.url.query.decode()
        seen["api_key"] = request.headers.get("X-MBX-APIKEY")
        return httpx.Response(200, json={"balances": []})

    with _client(handler) as client:
        client.balances()

    query, _, signature = str(seen["query"]).rpartition("&signature=")
    expected = hmac.new(API_SECRET.encode(), query.encode(), hashlib.sha256).hexdigest()
    assert signature == expected
    assert seen["api_key"] == API_KEY
    params = dict(parse_qsl(query))
    assert "timestamp" in params and "recvWindow" in params


def test_sync_time_records_the_offset_and_future_timestamps_use_it():
    clock = Clock()  # local clock is 5s ahead of the venue

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"serverTime": int((clock.value - 5) * 1000)})

    with _client(handler, clock) as client:
        offset = client.sync_time()
        captured: dict[str, str] = {}

        def capture(request: httpx.Request) -> httpx.Response:
            captured.update(dict(parse_qsl(request.url.query.decode())))
            return httpx.Response(200, json={"balances": []})

        client._http = httpx.Client(transport=httpx.MockTransport(capture))
        client.balances()

    assert offset == pytest.approx(5000.0, abs=1)
    assert int(captured["timestamp"]) == int((clock.value - 5) * 1000)


# ── Error mapping ───────────────────────────────────────────────


def test_timestamp_rejection_resyncs_and_retries_once():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/api/v3/time":
            return httpx.Response(200, json={"serverTime": 1_764_000_000_000})
        if calls.count("/api/v3/account") == 1:
            return _error(-1021, "Timestamp for this request is outside of the recvWindow.")
        return httpx.Response(200, json={"balances": [{"asset": "USDT", "free": "10", "locked": "0"}]})

    with _client(handler) as client:
        balances = client.balances()

    assert balances["USDT"].free == pytest.approx(10.0)
    assert calls.count("/api/v3/account") == 2
    assert "/api/v3/time" in calls


def test_http_429_becomes_rate_limited_with_retry_after():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "17"}, json={"code": -1003, "msg": "x"})

    with _client(handler) as client:
        with pytest.raises(RateLimited) as exc:
            client.balances()
    assert exc.value.retry_after == pytest.approx(17.0)


def test_http_418_becomes_ip_banned():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(418, json={"code": -1003, "msg": "banned"})

    with _client(handler) as client:
        with pytest.raises(IpBanned):
            client.balances()


def test_insufficient_balance_and_filter_errors_are_distinguished():
    codes = iter([-2010, -1013])

    def handler(request: httpx.Request) -> httpx.Response:
        code = next(codes)
        return _error(code, "Account has insufficient balance" if code == -2010 else "Filter failure")

    with _client(handler) as client:
        with pytest.raises(InsufficientBalance):
            client.balances()
        with pytest.raises(OrderFilterError):
            client.balances()


def test_a_network_error_on_an_order_is_an_unknown_outcome():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out")

    with _client(handler) as client:
        with pytest.raises(UnknownOutcome):
            client.place_market_buy("SOLUSDT", 10.0, "arena-a-1")


def test_an_unknown_error_code_still_surfaces_code_and_message():
    def handler(request: httpx.Request) -> httpx.Response:
        return _error(-9000, "nope")

    with _client(handler) as client:
        with pytest.raises(BinanceApiError) as exc:
            client.balances()
    assert exc.value.code == -9000
    assert "nope" in exc.value.message


# ── Order placement ────────────────────────────────────────────


def test_market_buy_spends_a_quote_amount_and_asks_for_the_full_response():
    captured: dict[str, list[tuple[str, str]]] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["params"] = parse_qsl(request.url.query.decode())
        return httpx.Response(200, json=_order_payload())

    with _client(handler) as client:
        fill = client.place_market_buy("SOLUSDT", 10.0, "arena-a-1")

    params = dict(captured["params"])
    assert params["type"] == "MARKET"
    assert params["side"] == "BUY"
    assert params["quoteOrderQty"] == "10"
    assert params["newOrderRespType"] == "FULL"
    assert params["newClientOrderId"] == "arena-a-1"
    assert "quantity" not in params
    assert fill.status == "FILLED"


def test_market_sell_sends_a_quantity_instead_of_a_quote_amount():
    captured: dict[str, dict] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["params"] = dict(parse_qsl(request.url.query.decode()))
        return httpx.Response(200, json=_order_payload(side="SELL"))

    with _client(handler) as client:
        client.place_market_sell("SOLUSDT", 0.100, "arena-a-2")

    assert captured["params"]["quantity"] == "0.1"
    assert "quoteOrderQty" not in captured["params"]


def test_a_buy_commission_in_the_base_asset_is_converted_to_quote():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_order_payload())

    with _client(handler) as client:
        fill = client.place_market_buy("SOLUSDT", 10.02, "arena-a-1")

    assert fill.executed_qty == pytest.approx(0.1)
    # The 0.0001 SOL of commission is charged out of what was bought, so the wallet is
    # credited 0.0999 — the number the ledger must hold.
    assert fill.credited_qty == pytest.approx(0.0999)
    assert fill.cum_notional == pytest.approx(10.02)
    assert fill.price == pytest.approx(100.2)
    # 0.0001 SOL of commission at the 100.2 average price.
    assert fill.fee == pytest.approx(0.01002)
    assert fill.fee_asset == "SOL"


def test_a_sell_commission_in_the_quote_asset_is_taken_as_is():
    payload = _order_payload(
        side="SELL",
        executedQty="0.1",
        cummulativeQuoteQty="9.99000000",
        fills=[
            {"price": "99.9", "qty": "0.1", "commission": "0.00999", "commissionAsset": "USDT"}
        ],
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    with _client(handler) as client:
        fill = client.place_market_sell("SOLUSDT", 0.1, "arena-a-2")

    assert fill.fee == pytest.approx(0.00999)
    assert fill.fee_asset == "USDT"
    assert fill.credited_qty == pytest.approx(0.1)  # nothing taken from the base asset


def test_two_commission_assets_are_flagged_as_mixed():
    payload = _order_payload(
        fills=[
            {"price": "100", "qty": "0.05", "commission": "0.005", "commissionAsset": "USDT"},
            {"price": "100", "qty": "0.05", "commission": "0.00001", "commissionAsset": "BNB"},
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    with _client(handler) as client:
        fill = client.place_market_buy("SOLUSDT", 10.0, "arena-a-1")

    assert fill.fee_asset == "MIXED"
    assert fill.fee > 0.005


# ── Lookup & reconciliation helpers ────────────────────────────


def test_an_order_the_venue_never_saw_reads_back_as_none():
    def handler(request: httpx.Request) -> httpx.Response:
        return _error(-2013, "Order does not exist.")

    with _client(handler) as client:
        assert client.get_order("SOLUSDT", "arena-a-9") is None


def test_looking_up_a_filled_order_recovers_the_commission_from_my_trades():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v3/order":
            payload = _order_payload()
            payload.pop("fills")
            return httpx.Response(200, json=payload)
        return httpx.Response(
            200,
            json=[
                {"orderId": 4242, "price": "100.2", "qty": "0.1", "commission": "0.01002",
                 "commissionAsset": "USDT"}
            ],
        )

    with _client(handler) as client:
        fill = client.get_order("SOLUSDT", "arena-a-1")

    assert fill is not None
    assert fill.status == "FILLED"
    assert fill.fee == pytest.approx(0.01002)


def test_balances_drop_empty_assets_and_sum_locked_funds():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "balances": [
                    {"asset": "USDT", "free": "95.5", "locked": "0"},
                    {"asset": "SOL", "free": "0.7", "locked": "0.1"},
                    {"asset": "BNB", "free": "0", "locked": "0"},
                ]
            },
        )

    with _client(handler) as client:
        balances = client.balances()

    assert set(balances) == {"USDT", "SOL"}
    assert balances["SOL"].total == pytest.approx(0.8)


def test_open_orders_returns_the_raw_list():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{"symbol": "SOLUSDT", "orderId": 1}])

    with _client(handler) as client:
        assert client.open_orders() == [{"symbol": "SOLUSDT", "orderId": 1}]


# ── Rate limits ────────────────────────────────────────────────


def test_rate_limits_are_adopted_from_exchange_info():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "symbols": [
                    {
                        "symbol": "SOLUSDT",
                        "baseAsset": "SOL",
                        "quoteAsset": "USDT",
                        "status": "TRADING",
                    }
                ],
                "rateLimits": [
                    {"rateLimitType": "ORDERS", "interval": "SECOND", "intervalNum": 10,
                     "limit": 2},
                    {"rateLimitType": "REQUEST_WEIGHT", "interval": "MINUTE",
                     "intervalNum": 1, "limit": 6000},
                ],
            },
        )

    clock = Clock()
    with _client(handler, clock) as client:
        info = client.exchange_info(["SOLUSDT"])
        client.remember_symbols(info)
        client.place_market_buy("SOLUSDT", 6.0, "a-1")
        clock.advance(1)
        client.place_market_buy("SOLUSDT", 6.0, "a-2")
        clock.advance(1)
        with pytest.raises(RateLimited):
            client.place_market_buy("SOLUSDT", 6.0, "a-3")
        clock.advance(10)
        client.place_market_buy("SOLUSDT", 6.0, "a-4")


def test_credentials_are_required():
    with pytest.raises(ValueError, match="credentials"):
        BinanceSpotClient("", "", transport=httpx.MockTransport(lambda r: httpx.Response(200)))
