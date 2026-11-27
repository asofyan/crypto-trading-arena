from decimal import Decimal

import httpx
import pytest

from exchanges.binance_futures import BINANCE_FUTURES_DEMO_REST, BinanceFuturesClient
from exchanges.binance_spot import BinanceApiError, UnknownOutcome


def client(handler):
    return BinanceFuturesClient(
        "key", "secret",
        base_url=BINANCE_FUTURES_DEMO_REST,
        transport=httpx.MockTransport(handler),
        now=lambda: 1.0,
    )


def order_payload(status="FILLED"):
    return {
        "clientOrderId": "arena-a-1",
        "orderId": 481,
        "symbol": "ZECUSDT",
        "side": "BUY",
        "status": status,
        "executedQty": "0.020",
        "cumQuote": "14.030",
        "positionSide": "BOTH",
        "avgPrice": "701.5",
    }


def account_trades():
    return [
        {
            "symbol": "ZECUSDT", "orderId": 481, "id": 10, "side": "BUY",
            "positionSide": "BOTH", "qty": "0.01", "quoteQty": "7.010",
            "commission": "0.003505", "commissionAsset": "USDT",
        },
        {
            "symbol": "ZECUSDT", "orderId": 481, "id": 11, "side": "BUY",
            "positionSide": "BOTH", "qty": "0.01", "quoteQty": "7.020",
            "commission": "0.003510", "commissionAsset": "USDT",
        },
    ]


@pytest.mark.parametrize("reduce_only", [False, True])
def test_market_order_uses_contract_quantity_and_actual_trades(reduce_only):
    requests = []

    def handler(request):
        requests.append(request)
        assert request.url.host == "demo-fapi.binance.com"
        assert request.headers["X-MBX-APIKEY"] == "key"
        assert request.url.params["timestamp"] == "1000"
        assert request.url.params["signature"]
        if request.url.path == "/fapi/v1/order":
            assert request.method == "POST"
            assert request.url.params["newClientOrderId"] == "arena-a-1"
            assert request.url.params["positionSide"] == "BOTH"
            assert request.url.params["newOrderRespType"] == "RESULT"
            assert request.url.params["quantity"] == "0.02"
            assert request.url.params["reduceOnly"] == str(reduce_only).lower()
            return httpx.Response(200, json={**order_payload(), "reduceOnly": reduce_only})
        assert request.url.path == "/fapi/v1/userTrades"
        assert request.method == "GET"
        assert request.url.params["orderId"] == "481"
        return httpx.Response(200, json=account_trades())

    with client(handler) as api:
        fill = api.place_market_order("zecusdt", "buy", Decimal("0.020"), "arena-a-1",
                                      reduce_only=reduce_only)
    assert len(requests) == 2
    assert fill.executed_qty == fill.credited_qty == 0.02
    assert fill.price == 701.5
    assert fill.fee == pytest.approx(0.007015)
    assert fill.fee_asset == "USDT"


def test_query_recovers_expired_order_that_filled_partially():
    def handler(request):
        if request.url.path == "/fapi/v1/order":
            assert request.url.params["origClientOrderId"] == "arena-a-1"
            return httpx.Response(200, json=order_payload("EXPIRED"))
        return httpx.Response(200, json=account_trades())

    with client(handler) as api:
        fill = api.query_order("ZECUSDT", "arena-a-1")
    assert fill.status == "EXPIRED"
    assert fill.executed_qty == 0.02


@pytest.mark.parametrize("trades", [
    [],
    account_trades()[:1],
    [account_trades()[0], account_trades()[0]],
    [{**t, "commissionAsset": "BNB"} for t in account_trades()],
])
def test_missing_or_unusable_trade_evidence_has_unknown_outcome(trades):
    def handler(request):
        if request.url.path == "/fapi/v1/order":
            return httpx.Response(200, json={**order_payload(), "reduceOnly": True})
        return httpx.Response(200, json=trades)

    with client(handler) as api, pytest.raises(UnknownOutcome):
        api.place_market_order("ZECUSDT", "BUY", Decimal("0.02"), "arena-a-1",
                               reduce_only=True)


@pytest.mark.parametrize("status,code", [
    (503, -1007), (502, None), (408, -1121), (307, None),
    (400, -1000), (400, -1006), (400, -4116), (400, None), (400, "-1007"),
])
def test_ambiguous_response_never_reposts(status, code):
    def handler(request):
        requests.append(request)
        return httpx.Response(status, json={"code": code, "msg": "execution status unknown"})

    requests = []
    with client(handler) as api, pytest.raises(UnknownOutcome):
        api.place_market_order("ZECUSDT", "BUY", Decimal("0.02"), "arena-a-1",
                               reduce_only=False)
    assert len(requests) == 1


@pytest.mark.parametrize("code,expected", [(-2013, None), (-2015, BinanceApiError)])
def test_recovery_distinguishes_absence_from_read_failure(code, expected):
    def handler(request):
        return httpx.Response(400, json={"code": code, "msg": "error"})

    with client(handler) as api:
        if expected is None:
            assert api.query_order("ZECUSDT", "arena-a-1") is None
        else:
            with pytest.raises(expected):
                api.query_order("ZECUSDT", "arena-a-1")


def test_recovers_signed_account_state_with_futures_endpoints():
    responses = {
        "/fapi/v1/time": {"serverTime": 1000},
        "/fapi/v1/positionSide/dual": {"dualSidePosition": False},
        "/fapi/v1/feeBurn": {"feeBurn": False},
        "/fapi/v1/accountConfig": {"canTrade": True},
        "/fapi/v1/symbolConfig": [{"leverage": 1}],
        "/fapi/v3/account": {"availableBalance": "100.00"},
        "/fapi/v3/balance": [{"asset": "USDT", "accountAlias": "arena"}],
        "/fapi/v3/positionRisk": [{"positionAmt": "-0.02"}],
        "/fapi/v1/openOrders": [{"orderId": 1}],
        "/fapi/v1/openAlgoOrders": [{"algoId": 2}],
    }

    def handler(request):
        if request.url.path != "/fapi/v1/time":
            assert request.url.params["signature"]
            assert "symbol" not in request.url.params
        return httpx.Response(200, json=responses[request.url.path])

    with client(handler) as api:
        assert api.sync_time() == 0
        assert api.hedge_mode() is False
        assert api.bnb_fee_burn() is False
        assert api.account_config() == {"canTrade": True}
        assert api.symbol_config() == [{"leverage": 1}]
        assert api.account() == {"availableBalance": "100.00"}
        assert api.futures_balances() == [{"asset": "USDT", "accountAlias": "arena"}]
        assert api.position_risk() == [{"positionAmt": "-0.02"}]
        assert api.open_orders() == [{"orderId": 1}]
        assert api.open_algo_orders() == [{"algoId": 2}]


def test_exchange_info_selects_only_tradable_usdt_perpetuals():
    symbol = {
        "symbol": "ZECUSDT", "contractType": "PERPETUAL", "quoteAsset": "USDT",
        "marginAsset": "USDT", "status": "TRADING", "orderTypes": ["MARKET"],
        "filters": [
            {"filterType": "LOT_SIZE", "minQty": "0.01", "stepSize": "0.01"},
            {"filterType": "MARKET_LOT_SIZE", "minQty": "0.05", "stepSize": "0.05"},
        ],
    }

    def handler(request):
        assert request.url.path == "/fapi/v1/exchangeInfo"
        assert not request.url.params
        return httpx.Response(200, json={"symbols": [
            symbol,
            {**symbol, "symbol": "FOOUSDT", "status": "PENDING_TRADING"},
            {**symbol, "symbol": "BARUSDT", "contractType": "CURRENT_QUARTER"},
            {**symbol, "symbol": "SNDKUSDT", "contractType": "TRADIFI_PERPETUAL"},
            {**symbol, "symbol": "UNREQUESTEDUSDT"},
        ]})

    with client(handler) as api:
        entries = api.exchange_info(["ZECUSDT", "FOOUSDT", "BARUSDT", "SNDKUSDT"])["symbols"]
    assert [entry["symbol"] for entry in entries] == ["ZECUSDT", "SNDKUSDT"]
    assert entries[0]["filters"] == symbol["filters"]


@pytest.mark.parametrize("status", [418, 429])
def test_rate_limit_during_fee_lookup_cannot_turn_submission_into_rejection(status):
    def handler(request):
        if request.method == "POST":
            return httpx.Response(200, json={**order_payload(), "reduceOnly": False})
        return httpx.Response(status, json={"msg": "rate limit"})

    with client(handler) as api, pytest.raises(UnknownOutcome):
        api.place_market_order("ZECUSDT", "BUY", Decimal("0.02"), "arena-a-1",
                               reduce_only=False)


def test_fill_cannot_prove_reduce_only_if_order_response_conflicts():
    def handler(request):
        assert request.method == "POST"
        return httpx.Response(200, json={**order_payload(), "reduceOnly": False})

    with client(handler) as api, pytest.raises(UnknownOutcome):
        api.place_market_order("ZECUSDT", "BUY", Decimal("0.02"), "arena-a-1",
                               reduce_only=True)


def test_funding_read_uses_current_income_contract():
    def handler(request):
        assert request.url.path == "/fapi/v1/income"
        assert request.url.params["incomeType"] == "FUNDING_FEE"
        assert request.url.params["startTime"] == "101"
        assert request.url.params["endTime"] == "102"
        assert request.url.params["limit"] == "1000"
        assert "page" not in request.url.params
        return httpx.Response(200, json=[])

    with client(handler) as api:
        assert api.funding_income(101, 102) == []


@pytest.mark.parametrize("status,body", [
    (200, {"code": -1007}),
    (200, []),
    (400, []),
])
def test_query_requires_readable_order_object(status, body):
    with client(lambda _: httpx.Response(status, json=body)) as api:
        with pytest.raises(UnknownOutcome):
            api.query_order("ZECUSDT", "arena-a-1")


@pytest.mark.parametrize("quantity", [None, Decimal("NaN"), Decimal("0")])
def test_market_order_with_invalid_quantity_does_not_send_request(quantity):
    def unexpected_request(request):
        pytest.fail(f"Invalid order sent a {request.method} request")

    with client(unexpected_request) as api, pytest.raises(ValueError):
        api.place_market_order("ZECUSDT", "BUY", quantity, "arena-a-1", reduce_only=True)


@pytest.mark.parametrize("patch", [
    {"positionSide": "SHORT"},
    {"executedQty": "0"},
])
def test_query_rejects_order_with_inconsistent_identity_or_quantity(patch):
    body = {**order_payload(), **patch}
    with client(lambda _: httpx.Response(200, json=body)) as api:
        with pytest.raises(UnknownOutcome):
            api.query_order("ZECUSDT", "arena-a-1")


def test_order_rejects_response_exceeding_submitted_quantity():
    def handler(request):
        if request.method == "POST":
            return httpx.Response(200, json={**order_payload(), "reduceOnly": False})
        return httpx.Response(200, json=account_trades())

    with client(handler) as api, pytest.raises(UnknownOutcome, match="more than"):
        api.place_market_order("ZECUSDT", "BUY", Decimal("0.01"), "arena-a-1",
                               reduce_only=False)


def test_trade_requires_usable_execution_numbers():
    def handler(request):
        if request.url.path == "/fapi/v1/order":
            return httpx.Response(200, json=order_payload())
        return httpx.Response(200, json=[{**account_trades()[0], "quoteQty": "-1"}])

    with client(handler) as api, pytest.raises(UnknownOutcome, match="invalid.*amounts"):
        api.query_order("ZECUSDT", "arena-a-1")


def test_signed_account_reads_never_coerce_unusable_state_to_flat():
    def handler(request):
        if request.url.path == "/fapi/v3/positionRisk":
            assert request.url.params["symbol"] == "ZECUSDT"
            return httpx.Response(200, json={})
        return httpx.Response(200, json={"feeBurn": "false"})

    with client(handler) as api:
        assert api.base_url == BINANCE_FUTURES_DEMO_REST
        with pytest.raises(UnknownOutcome):
            api.position_risk("zecusdt")
        with pytest.raises(UnknownOutcome):
            api.bnb_fee_burn()


def test_futures_client_requires_credentials():
    with pytest.raises(ValueError, match="futures credentials"):
        BinanceFuturesClient("key", "")
