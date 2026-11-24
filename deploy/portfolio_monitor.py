"""Read-only live portfolio monitor for an arena that is already running.

The arena's paper wallets live in the memory of the `deploy.tools_and_dashboard`
process, so a second copy of that entrypoint cannot observe them: it would start
its own wallet at $100k and, because calfkit names consumer groups after the node
(`calfkit/worker/worker.py`), it would join the very same groups and split the
stream — leaving two half-arenas that both look plausible.

This monitor takes the indirect route instead. It replays the trades CSV that the
running process writes, marks the rebuilt positions to market from the same Kafka
price topic, and renders the same `PortfolioView`. Two properties keep the arena
untouched:

* it subscribes to `market_data.prices` under its own consumer group, so Kafka
  fans the feed out to it rather than handing over the arena's partitions;
* it never publishes anything — no fills, no tool calls, no state of its own.

Killing this monitor therefore has no effect on trading, and the wallets it shows
survive it.

Usage:
    uv run python -m deploy.portfolio_monitor --bootstrap-servers localhost:9092
    uv run python -m deploy.portfolio_monitor --once   # print a snapshot, no broker
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import io
import logging
from pathlib import Path

from rich.console import Console
from rich.live import Live

from calfkit import Client, Worker

from arena.account_store import AccountStore
from arena.dashboard import PortfolioView
from arena.fees import FeeModel
from arena.price_book import PriceBook
from config import load_config_strict
from exchanges import PRICE_TOPIC, TickerMessage

# Distinct from the arena's "tools-dashboard" group on purpose: a different
# consumer group means a full copy of the price stream (fan-out), not a split of
# the arena's partitions.
MONITOR_GROUP_ID = "portfolio-monitor"

logger = logging.getLogger(__name__)


def latest_trades_file(data_dir: Path) -> Path | None:
    """Newest `trades_*.csv` — one file per arena session, so the newest wins."""
    files = sorted(data_dir.glob("trades_*.csv"), key=lambda p: p.stat().st_mtime)
    return files[-1] if files else None


class PortfolioMirror:
    """Portfolio state rebuilt from a running arena's trades CSV.

    Positions are replayed through the same `AccountStore.execute_trade` that
    produced the CSV, with the recorded fill price injected into a private price
    book — so the accounting (fees, cost basis, realised P&L) is the arena's own
    code, not a copy of it.
    """

    def __init__(self, data_dir: Path, fee_model: FeeModel) -> None:
        self._data_dir = data_dir
        self._fee_model = fee_model
        self._path: Path | None = None
        self._offset = 0
        self._header_line: str | None = None
        self._rows = 0
        self._fee_warnings = 0
        # Set by _reset_session so every caller sees the active objects.
        self.store = AccountStore(PriceBook(), fee_model)
        self.view = PortfolioView(self.store)

    @property
    def session_file(self) -> Path | None:
        return self._path

    def _reset_session(self, path: Path) -> None:
        logger.info("mirroring session %s", path.name)
        self.store = AccountStore(PriceBook(), self._fee_model)
        self.view = PortfolioView(self.store)
        self._path = path
        self._offset = 0
        self._header_line = None
        self._rows = 0

    def sync(self) -> bool:
        """Pick up the newest session file and replay any rows appended since.

        Returns True when state changed (new session or new fills), so the caller
        can rerender.
        """
        path = latest_trades_file(self._data_dir)
        if path is None:
            return False
        if path != self._path:
            self._reset_session(path)

        # Byte offsets, so a row the recorder is still flushing is simply left
        # for the next poll instead of being parsed half-written.
        with path.open("rb") as f:
            f.seek(self._offset)
            chunk = f.read()

        consumed = 0
        complete = b""
        for line in chunk.splitlines(keepends=True):
            if not line.endswith(b"\n"):
                break
            complete += line
            consumed += len(line)
        if not complete:
            return False

        text = complete.decode("utf-8", errors="replace")
        if self._header_line is None:
            # First read of a session: keep the header so later chunks can be
            # handed to DictReader starting mid-file.
            first, _, rest = text.partition("\n")
            self._header_line = first + "\n"
            text = rest

        changed = False
        for row in csv.DictReader(io.StringIO(self._header_line + text)):
            if self._apply(row):
                changed = True
        self._offset += consumed
        return changed

    def _apply(self, row: dict[str, str]) -> bool:
        try:
            product_id = row["product_id"]
            action = row["action"]
            quantity = float(row["quantity"])
            price = float(row["price"])
            reported_cash = float(row["cash_after"])
            agent_id = row["agent_id"]
        except (KeyError, TypeError, ValueError):
            logger.debug("skipping unparsable trade row: %r", row)
            return False

        # execute_trade fills buys at best_ask and sells at best_bid, so pinning
        # both to the recorded price replays the exact fill.
        self.store.price_book.update(
            {
                "product_id": product_id,
                "price": price,
                "best_bid": price,
                "best_ask": price,
            }
        )
        result = self.store.execute_trade(agent_id, product_id, quantity, action)
        if not result.success:
            logger.warning("could not replay %s %s %s: %s", agent_id, action, product_id,
                           result.message)
            return False

        account = self.store.get_or_create(agent_id)
        if abs(account.cash - reported_cash) > 0.01 and self._fee_warnings < 3:
            # Filled cash is the arena's ground truth; drift here means this
            # monitor's fee rate or config has diverged from the running process.
            self._fee_warnings += 1
            logger.warning(
                "replayed cash %.2f != recorded %.2f for %s — check that "
                "trading.fees.taker_bps matches the running arena",
                account.cash, reported_cash, agent_id,
            )
            account.cash = reported_cash
        self._rows += 1
        return True

    def update_price(self, ticker: TickerMessage) -> None:
        """Fold a live market-data tick into the mirrored price book."""
        self.store.price_book.update(ticker.model_dump())

    @property
    def replayed_contracts(self) -> int:
        return self._rows


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read-only monitor for a running trading arena.",
    )
    parser.add_argument(
        "--bootstrap-servers",
        default="localhost:9092",
        help="Kafka bootstrap servers address (default: localhost:9092)",
    )
    parser.add_argument(
        "--data-dir",
        default="./data",
        help="Directory holding the arena's trades_*.csv (default: ./data)",
    )
    parser.add_argument(
        "--config",
        default="config.json",
        help="Path to config file (default: config.json)",
    )
    parser.add_argument(
        "--refresh",
        type=float,
        default=2.0,
        help="Seconds between trades-CSV polls (default: 2.0)",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Replay the CSV and print one snapshot, then exit (no broker needed)",
    )
    return parser.parse_args()


async def run(args: argparse.Namespace) -> None:
    fee_model = FeeModel(taker_bps=load_config_strict(args.config).trading.fees.taker_bps)
    mirror = PortfolioMirror(Path(args.data_dir), fee_model)

    if not mirror.sync() and mirror.session_file is None:
        print(f"ERROR: no trades_*.csv in {args.data_dir} — nothing to monitor yet.")
        return

    if args.once:
        # The Live layout divides the terminal height between panels; without a tty
        # there is no height to divide, so print the panels stacked instead.
        console = Console(width=140)
        console.print(mirror.view._build_summary_cards())
        console.print(mirror.view._build_positions_table())
        console.print(mirror.view._build_trade_log())
        print(f"replayed {mirror.replayed_contracts} fill(s) from {mirror.session_file}")
        return

    client = Client.connect(args.bootstrap_servers)
    state: dict[str, Live] = {}

    @client.broker.subscriber(PRICE_TOPIC, group_id=MONITOR_GROUP_ID)
    async def handle_price_update(ticker: TickerMessage) -> None:
        mirror.update_price(ticker)
        if state:
            mirror.view.rerender()

    async def poll_trades() -> None:
        while True:
            await asyncio.sleep(args.refresh)
            if mirror.sync():
                if state:
                    # A new session file means a fresh view; reattach and repaint.
                    mirror.view.attach_live(state["live"])
                    mirror.view.rerender()

    print(f"Monitoring {mirror.session_file} (read-only, no wallet of its own).")
    print(f"Price feed: {PRICE_TOPIC} as consumer group '{MONITOR_GROUP_ID}'.")

    # A bare @broker.subscriber is inert until a Worker runs the app; with an empty
    # node list this worker does nothing but keep that price subscriber alive.
    worker = Worker(client, nodes=[])
    poller = asyncio.create_task(poll_trades())
    try:
        with Live(mirror.view._build_layout(), auto_refresh=False, screen=True) as live:
            state["live"] = live
            mirror.view.attach_live(live)
            mirror.view.rerender()
            await worker.run()
    finally:
        poller.cancel()


def main() -> None:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s — %(message)s")
    asyncio.run(run(parse_args()))


if __name__ == "__main__":
    main()
