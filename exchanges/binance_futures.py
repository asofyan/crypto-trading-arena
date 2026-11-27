"""Signed REST client for market orders on Binance USDⓈ-M USDT perpetuals."""

from __future__ import annotations

import time
from decimal import Decimal, InvalidOperation
from typing import Any, Callable

import httpx

from exchanges.binance_spot import (
    CODE_ORDER_NOT_FOUND,
    BinanceApiError,
    BinanceError,
    BinanceSpotClient,
    UnknownOutcome,
    VenueFill,
)

BINANCE_FUTURES_REST = "https://fapi.binance.com"
BINANCE_FUTURES_DEMO_REST = "https://demo-fapi.binance.com"
_ORDER_PATH = "/fapi/v1/order"
_ORDER_STATUSES = frozenset({
    "NEW", "PARTIALLY_FILLED", "FILLED", "CANCELED", "REJECTED", "EXPIRED", "EXPIRED_IN_MATCH",
})
_UNKNOWN_EXECUTION_CODES = frozenset({-1000, -1006, -1007, -4116})


class _BinanceFuturesTransport(BinanceSpotClient):
    """Reuse the signer, timestamp recovery and rate budget behind the futures API."""

    def server_time_ms(self) -> int:
        return int(self.public_object("/fapi/v1/time")["serverTime"])

    def public_object(self, path: str) -> dict[str, Any]:
        response = self._http.get(f"{self.base_url}{path}")
        self._raise_for_status(response)
        return _object(response.json(), path)

    def _error_from(self, response: httpx.Response) -> BinanceError:
        # A gateway error or backend timeout may follow an accepted order. Preserve
        # that uncertainty so the journal retains the original client ID.
        if (
            response.status_code == 408
            or 300 <= response.status_code < 400
            or response.status_code >= 500
        ):
            return UnknownOutcome(f"HTTP {response.status_code} from {response.request.url.path}")
        try:
            error = super()._error_from(response)
        except AttributeError:
            return UnknownOutcome(f"Unexpected error payload from {response.request.url.path}")
        if isinstance(error, BinanceApiError):
            if type(error.code) is not int:
                return UnknownOutcome(f"Unverified HTTP error from {response.request.url.path}")
            if error.code in _UNKNOWN_EXECUTION_CODES:
                return UnknownOutcome(f"Binance {error.code} from {response.request.url.path}")
        return error

    def signed(
        self, method: str, path: str, params: dict[str, Any] | None = None
    ) -> Any:
        try:
            value = self._signed(method, path, params)
        except (httpx.TransportError, ValueError, UnknownOutcome) as exc:
            raise UnknownOutcome(f"Unreadable response to {method} {path}") from exc
        # In particular, a 200 containing an error object is not evidence of an empty
        # list of positions or open orders.
        if isinstance(value, dict) and "code" in value:
            raise UnknownOutcome(f"Unexpected error payload for {method} {path}")
        return value

    def submit_market_order(self, params: dict[str, Any]) -> dict[str, Any]:
        self._consume_order_budget()
        return _object(self.signed("POST", _ORDER_PATH, params), _ORDER_PATH)


class BinanceFuturesClient:
    def __init__(
        self,
        api_key: str,
        api_secret: str,
        *,
        base_url: str = BINANCE_FUTURES_REST,
        recv_window_ms: int = 5000,
        timeout: float = 10.0,
        transport: httpx.BaseTransport | None = None,
        now: Callable[[], float] = time.time,
    ) -> None:
        if not api_key or not api_secret:
            raise ValueError("Binance futures credentials are required (api_key/api_secret)")
        self._api = _BinanceFuturesTransport(
            api_key,
            api_secret,
            base_url=base_url,
            recv_window_ms=recv_window_ms,
            timeout=timeout,
            transport=transport,
            now=now,
        )

    @property
    def base_url(self) -> str:
        return self._api.base_url

    def close(self) -> None:
        self._api.close()

    def __enter__(self) -> BinanceFuturesClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def sync_time(self) -> float:
        return self._api.sync_time()

    def exchange_info(self, symbols: list[str] | None = None) -> dict[str, Any]:
        payload = self._api.public_object("/fapi/v1/exchangeInfo")
        self._api.configure_rate_limits(payload)
        requested = {s.upper() for s in symbols} if symbols is not None else None
        eligible = []
        for row in _rows(payload.get("symbols"), "exchangeInfo symbols"):
            if requested is not None and row.get("symbol") not in requested:
                continue
            if (
                row.get("contractType") not in ("PERPETUAL", "TRADIFI_PERPETUAL")
                or row.get("quoteAsset") != "USDT"
                or row.get("marginAsset") != "USDT"
                or row.get("status") != "TRADING"
                or "MARKET" not in (row.get("orderTypes") or [])
            ):
                continue
            eligible.append(row)
        return {**payload, "symbols": eligible}

    def _get_rows(
        self, path: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        return _rows(self._api.signed("GET", path, params), path)

    def _get_object(self, path: str) -> dict[str, Any]:
        return _object(self._api.signed("GET", path), path)

    def hedge_mode(self) -> bool:
        return self._get_flag("/fapi/v1/positionSide/dual", "dualSidePosition")

    def _get_flag(self, path: str, name: str) -> bool:
        value = self._get_object(path).get(name)
        if not isinstance(value, bool):
            raise UnknownOutcome(f"Binance did not report {name}")
        return value

    def bnb_fee_burn(self) -> bool:
        return self._get_flag("/fapi/v1/feeBurn", "feeBurn")

    def account_config(self) -> dict[str, Any]:
        return self._get_object("/fapi/v1/accountConfig")

    def symbol_config(self, symbol: str | None = None) -> list[dict[str, Any]]:
        return self._get_rows("/fapi/v1/symbolConfig", _symbol_params(symbol))

    def account(self) -> dict[str, Any]:
        return self._get_object("/fapi/v3/account")

    def futures_balances(self) -> list[dict[str, Any]]:
        return self._get_rows("/fapi/v3/balance")

    def position_risk(self, symbol: str | None = None) -> list[dict[str, Any]]:
        return self._get_rows("/fapi/v3/positionRisk", _symbol_params(symbol))

    def open_orders(self) -> list[dict[str, Any]]:
        return self._get_rows("/fapi/v1/openOrders")

    def open_algo_orders(self) -> list[dict[str, Any]]:
        return self._get_rows("/fapi/v1/openAlgoOrders")

    def funding_income(self, start_time_ms: int, end_time_ms: int) -> list[dict[str, Any]]:
        return self._get_rows("/fapi/v1/income", {
            "incomeType": "FUNDING_FEE",
            "startTime": start_time_ms,
            "endTime": end_time_ms,
            "limit": 1000,
        })

    def place_market_order(
        self,
        symbol: str,
        side: str,
        quantity: Decimal | float,
        client_order_id: str,
        *,
        reduce_only: bool,
    ) -> VenueFill:
        symbol, side = symbol.upper(), side.upper()
        try:
            qty = _decimal(quantity)
        except UnknownOutcome as exc:
            raise ValueError("A futures market order requires a finite quantity") from exc
        if side not in ("BUY", "SELL") or qty <= 0 or not client_order_id:
            raise ValueError("A futures market order requires BUY or SELL, quantity and client ID")
        quantity_text = format(qty, "f")
        if "." in quantity_text:
            quantity_text = quantity_text.rstrip("0").rstrip(".")
        payload = self._api.submit_market_order({
            "symbol": symbol,
            "side": side,
            "type": "MARKET",
            "quantity": quantity_text,
            "positionSide": "BOTH",
            "reduceOnly": str(reduce_only).lower(),
            "newClientOrderId": client_order_id,
            "newOrderRespType": "RESULT",
        })
        if payload.get("side") != side or payload.get("reduceOnly") is not reduce_only:
            raise UnknownOutcome("Binance order response has conflicting trade direction")
        fill = self._read_fill(payload, symbol, client_order_id)
        if _decimal(payload.get("executedQty")) > qty:
            raise UnknownOutcome("Binance executed more than the requested quantity")
        return fill

    def query_order(self, symbol: str, client_order_id: str) -> VenueFill | None:
        symbol = symbol.upper()
        try:
            result = self._api.signed("GET", _ORDER_PATH, {
                "symbol": symbol, "origClientOrderId": client_order_id,
            })
        except BinanceApiError as exc:
            if exc.code == CODE_ORDER_NOT_FOUND:
                return None
            raise
        return self._read_fill(_object(result, _ORDER_PATH), symbol, client_order_id)

    def _read_fill(
        self, payload: dict[str, Any], symbol: str, client_order_id: str
    ) -> VenueFill:
        status, side = payload.get("status"), payload.get("side")
        order_id = payload.get("orderId")
        if (
            payload.get("symbol") != symbol
            or payload.get("clientOrderId") != client_order_id
            or not isinstance(order_id, int)
            or order_id <= 0
            or not isinstance(status, str)
            or status not in _ORDER_STATUSES
            or side not in ("BUY", "SELL")
            or payload.get("positionSide") != "BOTH"
        ):
            raise UnknownOutcome("Binance futures order response could not be identified")
        executed = _decimal(payload.get("executedQty"))
        if executed < 0 or (executed == 0 and status in ("FILLED", "PARTIALLY_FILLED")):
            raise UnknownOutcome("Binance reported inconsistent executed quantity")
        price, notional, fee = Decimal(0), Decimal(0), Decimal(0)
        if executed:
            try:
                trades = self._get_rows("/fapi/v1/userTrades", {
                    "symbol": symbol, "orderId": order_id, "limit": 1000,
                })
            except BinanceError as exc:
                # A failure reading trades cannot undo the order's execution.
                raise UnknownOutcome("Could not verify Binance futures order trades") from exc
            notional, fee = _trade_totals(trades, executed, symbol, side, order_id)
            price = notional / executed
        return VenueFill(
            client_order_id=client_order_id,
            venue_order_id=str(order_id),
            symbol=symbol,
            side=side,
            status=status,
            executed_qty=float(executed),
            credited_qty=float(executed),
            cum_notional=float(notional),
            price=float(price),
            fee=float(fee),
            fee_asset="USDT" if executed else None,
            raw=payload,
        )


def _object(value: Any, source: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise UnknownOutcome(f"{source} did not return a JSON object")
    return value


def _rows(value: Any, source: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        raise UnknownOutcome(f"{source} did not return a list of JSON objects")
    return value


def _symbol_params(symbol: str | None) -> dict[str, Any] | None:
    return {"symbol": symbol.upper()} if symbol is not None else None


def _decimal(value: Any) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except InvalidOperation as exc:
        raise UnknownOutcome("Binance futures numeric value is missing or invalid") from exc
    if not parsed.is_finite():
        raise UnknownOutcome("Binance futures numeric value is not finite")
    return parsed


def _trade_totals(
    trades: list[dict[str, Any]], executed: Decimal, symbol: str, side: str, order_id: int
) -> tuple[Decimal, Decimal]:
    quantity, notional, fee = Decimal(0), Decimal(0), Decimal(0)
    seen: set[int] = set()
    for trade in trades:
        trade_id = trade.get("id")
        if (
            trade.get("symbol") != symbol
            or trade.get("side") != side
            or trade.get("orderId") != order_id
            or trade.get("positionSide") != "BOTH"
            or trade.get("commissionAsset") != "USDT"
            or not isinstance(trade_id, int)
            or trade_id in seen
        ):
            raise UnknownOutcome("Binance returned a duplicate or unsupported futures trade")
        seen.add(trade_id)
        qty, quote, commission = (
            _decimal(trade.get(field)) for field in ("qty", "quoteQty", "commission")
        )
        if qty <= 0 or quote <= 0 or commission < 0:
            raise UnknownOutcome("Binance returned invalid futures trade amounts")
        quantity += qty
        notional += quote
        fee += commission
    if quantity != executed:
        raise UnknownOutcome("Binance futures trades do not account for all executed quantity")
    return notional, fee
