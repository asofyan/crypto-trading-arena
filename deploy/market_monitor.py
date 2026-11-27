"""Read-only Binance perpetual market monitor for the scalper's exit band."""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import logging
import math
import sqlite3
from bisect import bisect_right
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta, tzinfo
from pathlib import Path
from statistics import median
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from pydantic import BaseModel, Field

from arena.instruments import PERP_SUFFIX
from config import load_config_strict
from exchanges.binance import PERP_VENUE, BinanceRESTClient, parse_binance_candle
from scripts.band_audit import Outcome, score_episode, summarise

logger = logging.getLogger(__name__)
INTERVAL = timedelta(minutes=5)
DEFAULT_SYMBOLS = ("ZECUSDT", "1000PEPEUSDT", "SNDKUSDT")
Quote = tuple[datetime, float, float]
SendAlert = Callable[[str, str], Awaitable[None]]


class RiskSettings(BaseModel):
    window_hours: int = Field(24, gt=0)
    baseline_days: int = Field(14, gt=0)
    persistent_evaluations: int = Field(3, gt=0)
    spread_increase_pct: float = Field(50, gt=0, allow_inf_nan=False)
    target_drop_pp: float = Field(15, gt=0, le=100)
    candle_change_pct: float = Field(50, gt=0, allow_inf_nan=False)
    minimum_coverage: float = Field(0.8, gt=0, le=1)
    max_quote_gap_seconds: float = Field(180, gt=0, allow_inf_nan=False)


@dataclass(frozen=True)
class Band:
    target: float
    stop: float
    hold_seconds: float
    fee_bps: float


@dataclass(frozen=True)
class Metric:
    value: float
    samples: int
    net_bps: float | None = None
    coverage: float = 1.0


def load_quotes(
    data_dir: Path, products: set[str], start: datetime, end: datetime, zone: tzinfo | None,
    *, allow_unlabeled: bool = False,
) -> dict[str, list[Quote]]:
    by_product: dict[str, dict[datetime, Quote]] = defaultdict(dict)
    for path in sorted(data_dir.glob("ticks_*.csv")):
        with path.open(newline="") as stream:
            reader = csv.DictReader(stream)
            trusted_legacy = allow_unlabeled and "source" not in (reader.fieldnames or [])
            for row in reader:
                if not trusted_legacy and row.get("source") != PERP_VENUE.name:
                    continue
                product = row.get("product_id")
                if product not in products:
                    continue
                try:
                    when = datetime.fromisoformat(row.get("quote_time", row["timestamp"]))
                    if when.tzinfo is None and zone is not None:
                        when = when.replace(tzinfo=zone)
                    when = when.astimezone(UTC)
                    if not start <= when < end:
                        continue
                    bid, ask = float(row["best_bid"]), float(row["best_ask"])
                except (ValueError, TypeError, KeyError):
                    continue
                if 0 < bid <= ask and math.isfinite(ask):
                    by_product[product][when] = (when, bid, ask)
    return {product: sorted(quotes.values()) for product, quotes in by_product.items()}


def quote_metrics(
    quotes: list[Quote], start: datetime, end: datetime, band: Band, settings: RiskSettings
) -> dict[str, Metric]:
    """Enter at the first recorded touch in each five-minute interval."""
    quotes = [quote for quote in quotes if start <= quote[0] < end]
    hold = timedelta(seconds=band.hold_seconds)
    max_gap = timedelta(seconds=settings.max_quote_gap_seconds)
    timestamps = [quote[0] for quote in quotes]
    spreads: list[float] = []
    outcomes: dict[int, list[Outcome]] = {1: [], -1: []}
    sampled_bucket = -1
    for index, (when, bid, ask) in enumerate(quotes):
        bucket = (when - start) // INTERVAL
        if bucket == sampled_bucket:
            continue
        sampled_bucket = bucket
        spreads.append((ask - bid) / ((bid + ask) / 2) * 10_000)
        horizon = when + hold
        if horizon > end:
            continue
        stop_index = bisect_right(timestamps, horizon, lo=index)
        # An outage can hide either barrier. Exclude the whole sample if any
        # part of its observation window is missing.
        times = [*timestamps[index:stop_index], horizon]
        if any(right - left > max_gap for left, right in zip(times, times[1:])):
            continue
        for side, entry in ((1, ask), (-1, bid)):
            episode = {"product": "", "side": side, "entry": entry, "t_open": when}
            outcome = score_episode(
                episode, quotes[index:stop_index], band.target, band.stop, band.hold_seconds
            )
            if outcome is not None:
                outcomes[side].append(outcome)

    metrics = {}
    if spreads:
        coverage = len(spreads) / math.ceil((end - start) / INTERVAL)
        metrics["spread"] = Metric(median(spreads), len(spreads), coverage=coverage)
    opportunities = max(1, math.ceil((end - start - hold) / INTERVAL))
    for side, series in outcomes.items():
        if series:
            stats = summarise(series, band.target, band.stop, band.fee_bps)
            name = "long-target" if side == 1 else "short-target"
            metrics[name] = Metric(
                stats["tp_pct"], len(series), stats["ev_bps"], min(1.0, len(series) / opportunities)
            )
    return metrics


async def candle_ranges(
    client: BinanceRESTClient, symbol: str, start: datetime, end: datetime
) -> dict[datetime, float]:
    ranges = {}
    cursor = start
    while cursor < end:
        page_end = min(end, cursor + 1000 * INTERVAL)
        rows = await client.get_klines(
            symbol, "5m", start_time=int(cursor.timestamp() * 1000),
            end_time=int(page_end.timestamp() * 1000) - 1,
        )
        latest = None
        for row in rows:
            candle = parse_binance_candle(row)
            if not cursor <= candle.time < page_end:
                continue
            latest = candle.time if latest is None else max(latest, candle.time)
            if (
                candle.time + INTERVAL <= end
                and all(math.isfinite(x) and x > 0 for x in (candle.open, candle.high, candle.low))
                and candle.high >= candle.low
            ):
                ranges[candle.time] = (candle.high - candle.low) / candle.open * 10_000
        cursor = page_end if latest is None else latest + INTERVAL
    return ranges


def candle_metric(
    candles: dict[datetime, float], start: datetime, end: datetime
) -> dict[str, Metric]:
    values = [value for when, value in candles.items() if start <= when < end]
    if not values:
        return {}
    coverage = len(values) / math.ceil((end - start) / INTERVAL)
    return {"candle-range": Metric(median(values), len(values), coverage=coverage)}


def signal(metric: str, value: float, baseline: float, settings: RiskSettings) -> str:
    if metric.endswith("-target"):
        return "down" if value <= baseline - settings.target_drop_pp else ""
    threshold = (
        settings.spread_increase_pct if metric == "spread" else settings.candle_change_pct
    )
    if value > baseline and value >= baseline * (1 + threshold / 100):
        return "up"
    if metric == "candle-range" and value < baseline and value <= baseline * (1 - threshold / 100):
        return "down"
    return ""


class DailyMonitor:
    """Record each metric's episode and queue its alerts in the same transaction."""

    def __init__(self, path: Path, settings: RiskSettings, band: Band) -> None:
        self.settings = settings
        self.band = band
        self.config_key = json.dumps([settings.model_dump(), asdict(band)], sort_keys=True)
        self.db = sqlite3.connect(path)
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS observations (
                product TEXT, metric TEXT, config TEXT, evaluated TEXT NOT NULL,
                direction TEXT NOT NULL, streak INTEGER NOT NULL, status TEXT NOT NULL,
                baseline TEXT NOT NULL, PRIMARY KEY (product, metric, config)
            );
            CREATE TABLE IF NOT EXISTS alerts (
                id INTEGER PRIMARY KEY, subject TEXT NOT NULL, body TEXT NOT NULL
            );
            """
        )

    def close(self) -> None:
        self.db.close()

    def evaluate(
        self, product: str, day: date, current: dict[str, Metric], baseline: dict[str, Metric]
    ) -> None:
        with self.db:
            for name, value in current.items():
                if value.coverage < self.settings.minimum_coverage:
                    continue
                self._evaluate_metric(product, name, day, value, baseline.get(name))

    def _evaluate_metric(
        self, product: str, name: str, day: date, current: Metric, baseline: Metric | None
    ) -> None:
        key = (product, name, self.config_key)
        previous = self.db.execute(
            "SELECT evaluated, direction, streak, status, baseline FROM observations "
            "WHERE product = ? AND metric = ? AND config = ?", key
        ).fetchone()
        last_day, last_direction, streak, last_status, frozen = previous or ("", "", 0, "", "")
        if last_day >= day.isoformat():
            return

        if last_direction:
            baseline = Metric(**json.loads(frozen))
        if baseline is None or baseline.coverage < self.settings.minimum_coverage:
            return

        direction = signal(name, current.value, baseline.value, self.settings)
        if direction and direction == last_direction:
            if last_day == (day - timedelta(days=1)).isoformat():
                streak += 1
            elif last_status != "persistent":
                streak = 1
        else:
            streak = int(bool(direction))
        status = ""
        if direction:
            status = "persistent" if streak >= self.settings.persistent_evaluations else "anomaly"

        self.db.execute(
            "INSERT OR REPLACE INTO observations VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (*key, day.isoformat(), direction, streak, status, json.dumps(asdict(baseline))),
        )
        if (status, direction) != (last_status, last_direction):
            self._queue_alert(product, name, day, status or "recovered", direction, current, baseline)

    def _queue_alert(
        self, product: str, name: str, day: date, status: str, direction: str,
        current: Metric, baseline: Metric,
    ) -> None:
        units = "%" if name.endswith("-target") else "bps"
        subject = f"[Arena] {product} {name} {status}"
        body = (
            f"UTC evaluation {day}\n"
            f"Signal {name} {direction or 'back within threshold'}\n"
            f"Current ({self.settings.window_hours} h): {current.value:.2f} {units} "
            f"(n={current.samples}, coverage={current.coverage:.0%})\n"
            f"Baseline ({self.settings.baseline_days} days): {baseline.value:.2f} {units} "
            f"(n={baseline.samples}, coverage={baseline.coverage:.0%})"
        )
        if current.net_bps is not None and baseline.net_bps is not None:
            body += (
                f"\nSampled {name.split('-')[0]} entries, band +{self.band.target:g}/"
                f"-{self.band.stop:g} bps, hold {self.band.hold_seconds / 60:g} min\n"
                f"Mean move after {self.band.fee_bps:g} bps round-trip fee: "
                f"{current.net_bps:.2f} vs {baseline.net_bps:.2f} bps."
            )
        self.db.execute("INSERT INTO alerts(subject, body) VALUES (?, ?)", (subject, body))

    async def deliver(self, send: SendAlert) -> None:
        for alert_id, subject, body in self.db.execute(
            "SELECT id, subject, body FROM alerts ORDER BY id"
        ).fetchall():
            await send(subject, body)
            with self.db:
                self.db.execute("DELETE FROM alerts WHERE id = ?", (alert_id,))


async def run_monitor(
    args: argparse.Namespace, monitor: DailyMonitor, send: SendAlert
) -> bool:
    settings = monitor.settings
    last_market_day: dict[str, date] = {}
    async with BinanceRESTClient(PERP_VENUE) as client:
        while True:
            now = datetime.now(UTC)
            end = now.replace(hour=0, minute=0, second=0, microsecond=0)
            day = end.date()
            current_start = end - timedelta(hours=settings.window_hours)
            baseline_start = current_start - timedelta(days=settings.baseline_days)
            symbols = [symbol for symbol in args.symbol if last_market_day.get(symbol) != day]
            if symbols:
                products = {symbol + PERP_SUFFIX for symbol in symbols}
                quotes = await asyncio.to_thread(
                    load_quotes, args.data_dir, products, baseline_start, end, args.quote_timezone,
                    allow_unlabeled=args.allow_unlabeled_quotes,
                )
            for symbol in symbols:
                product = symbol + PERP_SUFFIX
                series = quotes.get(product, [])
                current = quote_metrics(series, current_start, end, monitor.band, settings)
                baseline = quote_metrics(series, baseline_start, current_start, monitor.band, settings)
                try:
                    ranges = await candle_ranges(client, symbol, baseline_start, end)
                except (httpx.HTTPError, IndexError, TypeError, ValueError):
                    logger.exception("Could not load %s closed candles; will retry", symbol)
                else:
                    current.update(candle_metric(ranges, current_start, end))
                    baseline.update(candle_metric(ranges, baseline_start, current_start))
                    last_market_day[symbol] = day
                log_evaluation(product, day, current, baseline, settings.minimum_coverage)
                monitor.evaluate(product, day, current, baseline)
            try:
                await monitor.deliver(send)
            except (OSError, ValueError):
                if args.once:
                    raise
                logger.exception("Market alert delivery failed; queued alerts will be retried")
            if args.once:
                return all(last_market_day.get(symbol) == day for symbol in symbols)
            await asyncio.sleep(15 * 60)


def log_evaluation(
    product: str, day: date, current: dict[str, Metric], baseline: dict[str, Metric],
    minimum_coverage: float,
) -> None:
    for metric in ("candle-range", "spread", "long-target", "short-target"):
        recent = current.get(metric, Metric(0, 0, coverage=0))
        history = baseline.get(metric, Metric(0, 0, coverage=0))
        if min(recent.coverage, history.coverage) < minimum_coverage:
            logger.info(
                "%s %s %s coverage current %.0f%% (n=%d), baseline %.0f%% (n=%d)",
                day, product, metric, recent.coverage * 100, recent.samples,
                history.coverage * 100, history.samples,
            )
        else:
            logger.info(
                "%s %s %s current %.2f (n=%d), baseline %.2f (n=%d)",
                day, product, metric, recent.value, recent.samples, history.value, history.samples,
            )


def _timezone(value: str) -> ZoneInfo:
    try:
        return ZoneInfo(value)
    except ZoneInfoNotFoundError as error:
        raise argparse.ArgumentTypeError(f"Unknown quote timezone {value!r}") from error


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config-path", type=Path, default=Path("config.json"))
    parser.add_argument("--data-dir", type=Path, default=Path("data"), help="Directory of ticks_*.csv")
    parser.add_argument("--state-path", type=Path, help="Default: DATA_DIR/market-monitor.sqlite")
    parser.add_argument("--symbol", action="append", help="Binance futures symbol; repeat for pairs")
    parser.add_argument("--quote-timezone", type=_timezone, help="Naive quote timestamps; host timezone by default")
    parser.add_argument(
        "--allow-unlabeled-quotes", action="store_true",
        help="Include legacy CSV files without source labels from a verified mainnet-only directory",
    )
    parser.add_argument("--window-hours", type=int, default=24)
    parser.add_argument("--baseline-days", type=int, default=14)
    parser.add_argument("--persistent-evaluations", type=int, default=3)
    parser.add_argument("--spread-increase-pct", type=float, default=50)
    parser.add_argument("--target-drop-pp", type=float, default=15)
    parser.add_argument("--candle-change-pct", type=float, default=50)
    parser.add_argument("--minimum-coverage", type=float, default=0.8)
    parser.add_argument("--max-quote-gap-seconds", type=float, default=180)
    parser.add_argument("--once", action="store_true", help="Evaluate once and exit")
    parser.add_argument("--dry-run", action="store_true", help="Print alerts using temporary state")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    trading = load_config_strict(args.config_path).trading
    args.symbol = list(dict.fromkeys(
        symbol.upper().removesuffix(PERP_SUFFIX)
        for symbol in (args.symbol or trading.binance_perp_symbols or DEFAULT_SYMBOLS)
    ))
    floor = trading.exit_floor
    if not (floor.enabled and floor.min_profit_bps and floor.stop_bps and floor.max_hold_seconds):
        parser.error("trading.exit_floor needs an enabled target, stop, and hold limit")

    band = Band(
        floor.min_profit_bps, floor.stop_bps, floor.max_hold_seconds, 2 * trading.fees.perp_taker_bps
    )
    try:
        settings = RiskSettings.model_validate(vars(args))
    except ValueError as error:
        parser.error(str(error))
    if settings.window_hours * 3600 <= band.hold_seconds or settings.baseline_days * 86400 <= band.hold_seconds:
        parser.error("The current and baseline windows must both be longer than the hold limit")

    async def print_alert(subject: str, body: str) -> None:
        print(f"{subject}\n{body}")

    if args.dry_run:
        send = print_alert
        state = Path(":memory:")
        args.once = True
    else:
        from arena.alerts import GmailMailer

        try:
            send = GmailMailer.from_env().send
        except ValueError as error:
            parser.error(str(error))
        state = args.state_path or args.data_dir / "market-monitor.sqlite"
        state.parent.mkdir(parents=True, exist_ok=True)

    monitor = DailyMonitor(state, settings, band)
    try:
        if not asyncio.run(run_monitor(args, monitor, send)):
            raise SystemExit(1)
    except KeyboardInterrupt:
        pass
    finally:
        monitor.close()


if __name__ == "__main__":
    main()
