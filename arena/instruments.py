"""Venue instrument rules (Binance Spot): parse, quantise, and cache.

Trading sizes are only legal in the venue's own steps: SOLUSDT floors to 0.001 and
refuses orders under 5 USDT, so any quantity the arena books must be snapped to those
rules first. Parsing and quantising are pure decimal arithmetic over an exchangeInfo
payload; only `refresh_cache`/`main` touch the network, so the trading path and the
unit tests never do.
"""

from __future__ import annotations

import argparse
import json
import logging
from dataclasses import asdict, dataclass
from decimal import ROUND_DOWN, Decimal, InvalidOperation
from pathlib import Path

import httpx

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class SymbolRules:
    """What one venue symbol will accept: step sizes, minimums, price tick."""

    symbol: str
    base_asset: str
    quote_asset: str
    step_size: Decimal
    min_qty: Decimal
    min_notional: Decimal
    tick_size: Decimal
    min_notional_applies_to_market: bool = True

    def quantize(self, quantity: float | Decimal) -> Decimal | None:
        """Floor `quantity` to step_size, or None when the result is not tradable."""
        qty = _to_decimal(quantity)
        if qty is None or qty <= 0:
            return None
        stepped = (qty / self.step_size).to_integral_value(rounding=ROUND_DOWN) * self.step_size
        if stepped <= 0 or stepped < self.min_qty:
            return None
        return stepped

    def notional_ok(self, quantity: Decimal, price: float | Decimal) -> bool:
        """Whether quantity*price clears the venue's minimum notional.

        A market order is exempt when the venue sets applyMinToMarket false, which is
        why the flag is carried on the rules instead of being assumed by callers.
        """
        if self.min_notional <= 0 or not self.min_notional_applies_to_market:
            return True
        qty = _to_decimal(quantity)
        px = _to_decimal(price)
        if qty is None or px is None:
            return False
        return qty * px >= self.min_notional


def parse_exchange_info(payload: dict) -> dict[str, SymbolRules]:
    """Build rules from a GET /api/v3/exchangeInfo body, skipping untradable symbols.

    A symbol missing LOT_SIZE (or carrying a zero step) cannot be sized, so it is
    dropped rather than allowed to raise: one unknown listing must not take the engine
    down at startup.
    """
    rules: dict[str, SymbolRules] = {}
    for entry in payload.get("symbols") or []:
        if not isinstance(entry, dict):
            # A hand-edited or truncated cache must degrade to the fallback rules,
            # never take the engine down at startup.
            log.warning("instruments: skipping malformed symbol entry %r", entry)
            continue
        symbol_rules = _parse_symbol(entry)
        if symbol_rules is not None:
            rules[symbol_rules.symbol] = symbol_rules
    return rules


def load_cache(path: str | Path) -> dict[str, SymbolRules]:
    """Read cached rules; any problem yields {} so the caller can fall back."""
    cache_path = Path(path)
    try:
        raw = cache_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        log.debug("instrument cache %s not found; using fallback rules", cache_path)
        return {}
    except OSError as exc:
        log.warning("instrument cache %s unreadable (%s); using fallback rules", cache_path, exc)
        return {}

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        log.warning("instrument cache %s is corrupt (%s); using fallback rules", cache_path, exc)
        return {}

    if not isinstance(payload, dict):
        log.warning("instrument cache %s is not a JSON object; using fallback rules", cache_path)
        return {}

    if "symbols" in payload:
        # The cache may hold the venue payload verbatim, not our own serialisation.
        return parse_exchange_info(payload)

    try:
        return {symbol: _rules_from_dict(fields) for symbol, fields in payload.items()}
    except (KeyError, TypeError, ValueError, InvalidOperation) as exc:
        log.warning("instrument cache %s is malformed (%s); using fallback rules", cache_path, exc)
        return {}


def save_cache(rules: dict[str, SymbolRules], path: str | Path) -> None:
    """Write rules as JSON, Decimals as strings so no precision is lost on reload."""
    cache_path = Path(path)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {symbol: _rules_to_dict(rule) for symbol, rule in sorted(rules.items())}
    cache_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


# ── internals ───────────────────────────────────────────────


def _to_decimal(value: float | Decimal | None) -> Decimal | None:
    """Decimal(str(x)) only: Decimal(0.29) is 0.28999999999999998 and would floor short."""
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value if value.is_finite() else None
    try:
        converted = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return converted if converted.is_finite() else None


def _filter_value(filters: dict, filter_type: str) -> dict | None:
    entry = filters.get(filter_type)
    return entry if isinstance(entry, dict) else None


def _number(filter_entry: dict | None, key: str) -> Decimal:
    """Missing or malformed numbers read as 0; the caller then decides to skip or allow."""
    if filter_entry is None:
        return Decimal(0)
    converted = _to_decimal(filter_entry.get(key))
    return converted if converted is not None else Decimal(0)


def _parse_symbol(entry: dict) -> SymbolRules | None:
    symbol = entry.get("symbol")
    if not symbol or entry.get("status") != "TRADING":
        log.debug("instruments: skipping %s (status %s)", symbol, entry.get("status"))
        return None

    filters = {
        item["filterType"]: item
        for item in entry.get("filters") or []
        if isinstance(item, dict) and item.get("filterType")
    }

    # Binance reports MARKET_LOT_SIZE.stepSize "0.00000000" for most spot symbols; that
    # filter is deliberately not read, because trusting the zero would floor every order
    # to nothing. LOT_SIZE is the only step that is ever usable.
    lot = _filter_value(filters, "LOT_SIZE")
    step_size = _number(lot, "stepSize")
    min_qty = _number(lot, "minQty")
    if step_size <= 0 or min_qty <= 0:
        log.debug("instruments: skipping %s (no usable LOT_SIZE)", symbol)
        return None

    notional = _filter_value(filters, "NOTIONAL")
    if notional is not None:
        min_notional = _number(notional, "minNotional")
        applies_to_market = bool(notional.get("applyMinToMarket", True))
    else:
        # Older payloads only carry the legacy filter, whose flag is spelled differently.
        legacy = _filter_value(filters, "MIN_NOTIONAL")
        min_notional = _number(legacy, "minNotional")
        applies_to_market = bool((legacy or {}).get("applyToMarket", True))

    return SymbolRules(
        symbol=symbol,
        base_asset=str(entry.get("baseAsset", "")),
        quote_asset=str(entry.get("quoteAsset", "")),
        step_size=step_size,
        min_qty=min_qty,
        min_notional=min_notional,
        tick_size=_number(_filter_value(filters, "PRICE_FILTER"), "tickSize"),
        min_notional_applies_to_market=applies_to_market,
    )


def _rules_to_dict(rule: SymbolRules) -> dict:
    data = asdict(rule)
    for field in ("step_size", "min_qty", "min_notional", "tick_size"):
        data[field] = str(data[field])
    return data


def _rules_from_dict(data: dict) -> SymbolRules:
    if not isinstance(data, dict):
        raise TypeError(f"expected an object, got {type(data).__name__}")
    return SymbolRules(
        symbol=str(data["symbol"]),
        base_asset=str(data["base_asset"]),
        quote_asset=str(data["quote_asset"]),
        step_size=Decimal(str(data["step_size"])),
        min_qty=Decimal(str(data["min_qty"])),
        min_notional=Decimal(str(data["min_notional"])),
        tick_size=Decimal(str(data["tick_size"])),
        min_notional_applies_to_market=bool(data["min_notional_applies_to_market"]),
    )


# ── cache refresh (the only part that touches the network) ───────

BINANCE_SPOT_REST = "https://api.binance.com"
BINANCE_SPOT_TESTNET_REST = "https://testnet.binance.vision"


def fetch_exchange_info(
    *,
    base_url: str = BINANCE_SPOT_REST,
    symbols: list[str] | None = None,
    client: httpx.Client | None = None,
) -> dict:
    """GET /api/v3/exchangeInfo, optionally narrowed to `symbols` to keep it small."""
    params = {}
    if symbols:
        # Compact separators: the venue rejects the API with `", "` between symbols
        # (HTTP 400), so this is not cosmetic.
        params["symbols"] = json.dumps(
            [s.strip().upper() for s in symbols], separators=(",", ":")
        )
    own_client = client is None
    http = client or httpx.Client(timeout=15.0)
    try:
        response = http.get(f"{base_url.rstrip('/')}/api/v3/exchangeInfo", params=params)
        response.raise_for_status()
        return response.json()
    finally:
        if own_client:
            http.close()


def refresh_cache(
    path: str | Path,
    *,
    base_url: str = BINANCE_SPOT_REST,
    symbols: list[str] | None = None,
    client: httpx.Client | None = None,
) -> dict[str, SymbolRules]:
    """Fetch the venue's rules and persist them, so the engine can start offline."""
    payload = fetch_exchange_info(base_url=base_url, symbols=symbols, client=client)
    rules = parse_exchange_info(payload)
    if symbols:
        # A requested symbol that came back without usable rules is worth saying out
        # loud: the engine will silently fall back to the coarse legacy rule for it.
        missing = {s.upper() for s in symbols} - set(rules)
        if missing:
            log.warning(
                "instruments: no usable rules for %s; those symbols keep the legacy rule",
                ", ".join(sorted(missing)),
            )
    save_cache(rules, path)
    return rules


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m arena.instruments",
        description="Refresh the cached Binance instrument rules the paper engine snaps to.",
    )
    parser.add_argument(
        "--out",
        default="data/instruments-binance.json",
        help="cache path (default: %(default)s)",
    )
    parser.add_argument(
        "--symbols",
        nargs="*",
        default=None,
        help="symbols to cache (default: every tradable symbol the venue returns)",
    )
    parser.add_argument("--base-url", default=BINANCE_SPOT_REST, help="REST base URL")
    parser.add_argument(
        "--testnet",
        action="store_true",
        help="use testnet.binance.vision instead of the live venue",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="accepted for clarity; refreshing is this command's only action",
    )
    args = parser.parse_args(argv)

    base_url = BINANCE_SPOT_TESTNET_REST if args.testnet else args.base_url
    try:
        rules = refresh_cache(args.out, base_url=base_url, symbols=args.symbols)
    except httpx.HTTPError as exc:
        # A wall of httpx traceback is not an operator error message.
        print(f"ERROR: could not read exchangeInfo from {base_url}: {exc}")
        return 1
    print(f"Cached {len(rules)} symbol(s) from {base_url} → {args.out}")
    for symbol in sorted(rules):
        rule = rules[symbol]
        print(
            f"  {symbol:<10} step {rule.step_size:g}  min qty {rule.min_qty:g}  "
            f"min notional {rule.min_notional:g} {rule.quote_asset}"
        )
    return 0 if rules else 1


if __name__ == "__main__":
    raise SystemExit(main())
