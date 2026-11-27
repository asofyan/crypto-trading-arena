"""Read-only live portfolio monitor for an arena that is already running.

The monitor reads the arena's SQLite wallet ledger and marks positions using
the same Kafka price feed under its own consumer group. CSV replay is available
for sessions that run without a ledger. The monitor never publishes to Kafka.

Usage:
    uv run python -m deploy.portfolio_monitor --bootstrap-servers localhost:9092
    uv run python -m deploy.portfolio_monitor --once   # print a snapshot, no broker
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import csv
import io
import logging
import os
import signal
from pathlib import Path

from rich.console import Console
from rich.live import Live

from calfkit import Client, Worker

from arena.account_store import AccountStore
from arena.dashboard import PortfolioView
from arena.fees import FeeModel
from arena.instruments import SymbolRules
from arena.ledger import DEFAULT_DB_PATH, Ledger
from arena.models import AgentAccount, TradeLogEntry
from arena.price_book import PriceBook
from config import load_config_strict
from exchanges import PRICE_TOPIC, TickerMessage

# Distinct from the arena's "tools-dashboard" group on purpose: a different
# consumer group means a full copy of the price stream (fan-out), not a split of
# the arena's partitions.
MONITOR_GROUP_ID = "portfolio-monitor"
# Every run gets its own group id. A fixed group resumes from whatever offset a previous
# run committed, and this topic keeps history: a monitor started hours later would replay
# that backlog (262k ticks in one case) and show prices from the wrong session while the
# live edge raced away from it. Wallet state is loaded independently of Kafka.

logger = logging.getLogger(__name__)


def monitor_group_id() -> str:
    """The consumer group for this run: unique per process, so it starts at the live edge."""
    return f"{MONITOR_GROUP_ID}-{os.getpid()}"


def latest_trades_file(data_dir: Path) -> Path | None:
    """Newest `trades_*.csv` — one file per arena session, so the newest wins."""
    files = sorted(data_dir.glob("trades_*.csv"), key=lambda p: p.stat().st_mtime)
    return files[-1] if files else None


def latest_snapshots_file(data_dir: Path) -> Path | None:
    """Newest `snapshots_*.csv` — the sibling the recorder writes per session."""
    files = sorted(data_dir.glob("snapshots_*.csv"), key=lambda p: p.stat().st_mtime)
    return files[-1] if files else None


class PortfolioMirror:
    """Portfolio state rebuilt from a running arena's trades CSV.

    Positions are replayed through the same `AccountStore.execute_trade` that
    produced the CSV, with the recorded fill price injected into a private price
    book — so the accounting (fees, cost basis, realised P&L) is the arena's own
    code, not a copy of it.
    """

    def __init__(
        self,
        data_dir: Path,
        fee_model: FeeModel,
        rules: dict[str, SymbolRules] | None = None,
    ) -> None:
        self._data_dir = data_dir
        self._fee_model = fee_model
        # Venue rules make the replay accept exactly what the arena accepted: without
        # them a legal perp size (0.55 SOLUSDT-PERP) trips the legacy 1-decimal rule and
        # the position silently disappears from this view.
        self._rules = dict(rules or {})
        self._path: Path | None = None
        self._snapshot_path: Path | None = None
        self._offset = 0
        self._header_line: str | None = None
        self._rows = 0
        self._fee_warnings = 0
        # Set by _reset_session so every caller sees the active objects.
        self.store = self._new_store()
        self.view = PortfolioView(self.store)

    def _new_store(self) -> AccountStore:
        """A fresh store over the same price book, fee model and venue rules."""
        store = AccountStore(PriceBook(), self._fee_model)
        store.set_instrument_rules(self._rules)
        return store

    @property
    def session_file(self) -> Path | None:
        return self._path

    @property
    def snapshot_file(self) -> Path | None:
        return self._snapshot_path

    def _reset_session(self, path: Path) -> None:
        logger.info("mirroring session %s", path.name)
        self.store = self._new_store()
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
            # An arena can run for a while before anyone fills: keep the idle
            # agents visible from the snapshot rows instead of showing nothing.
            return self._seed_idle_agents(None)
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
        # Seed unconditionally: `changed or seed()` would skip the roster sync
        # on exactly the polls that replay a fill.
        seeded = self._seed_idle_agents(path)
        return changed or seeded

    def _seed_idle_agents(self, trades_path: Path | None) -> bool:
        """Add accounts for agents that appear in the snapshot but never filled.

        A fills-only roster hides exactly the agents that are behaving: a trend
        follower that sits in cash for a week has no trade row, yet it is a live
        wallet the arena snapshots every cycle. Snapshot rows are the ONLY
        evidence the recorder leaves for a zero-trade agent, so they seed a card
        at its snapshotted cash (positions stay empty — fills are what built
        them, and there are none).

        Returns True when a new account was added, so the caller repaints once.
        """
        if trades_path is not None:
            path = trades_path.with_name(
                trades_path.name.replace("trades_", "snapshots_", 1)
            )
        else:
            path = latest_snapshots_file(self._data_dir)
        self._snapshot_path = path
        if path is None or not path.exists():
            return False

        latest: dict[str, dict[str, str]] = {}
        with path.open(newline="") as f:
            for row in csv.DictReader(f):
                agent_id = row.get("agent_id")
                if agent_id:
                    latest[agent_id] = row

        changed = False
        for agent_id, row in latest.items():
            if agent_id in self.store.accounts:
                continue
            account = self.store.get_or_create(agent_id)
            try:
                account.cash = float(row["cash"])
            except (KeyError, TypeError, ValueError):
                pass  # A truncated row still earns the agent a card at $100.
            changed = True
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
        result = self.store.execute_trade(
            agent_id, product_id, quantity, action, replay=True
        )
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


class LedgerPortfolioMirror:
    """Read the current wallet without joining the writer or replaying fills."""

    def __init__(self, path: Path, data_dir: Path) -> None:
        self.session_file = path
        self.snapshot_file = None
        self.price_book = PriceBook()
        self.accounts: dict[str, AgentAccount] = {}
        self.trade_log: list[TradeLogEntry] = []
        self.ledger_degraded = False
        self.view = PortfolioView(self)

        # Seed marks before Kafka delivers its first fresh quote, including in --once.
        ticks = max(data_dir.glob("ticks_*.csv"), key=lambda file: file.name, default=None)
        if ticks is not None:
            latest = {}
            with ticks.open(newline="") as source:
                for row in csv.DictReader(source):
                    if all(row.get(field) for field in ("product_id", "price", "best_bid", "best_ask")):
                        latest[row["product_id"]] = row
            for row in latest.values():
                self.price_book.update(row)

    def sync(self) -> bool:
        # Reopen each poll so an engine restart that replaces the DB is visible.
        with Ledger(self.session_file, read_only=True) as ledger:
            accounts, fills, degraded = ledger.read_portfolio()
        changed = (self.accounts, self.trade_log, self.ledger_degraded) != (accounts, fills, degraded)
        self.accounts, self.trade_log, self.ledger_degraded = accounts, fills, degraded
        return changed

    def update_price(self, ticker: TickerMessage) -> None:
        self.price_book.update(ticker.model_dump())


def build_csv_mirror(data_dir: Path, config_path: str) -> PortfolioMirror:
    from deploy.tools_and_dashboard import resolve_instrument_rules

    config = load_config_strict(config_path)
    fee_model = FeeModel(
        taker_bps=config.trading.fees.taker_bps,
        perp_taker_bps=config.trading.fees.perp_taker_bps,
    )
    rules, _summary = resolve_instrument_rules(config.trading)
    return PortfolioMirror(data_dir, fee_model, rules)


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
        help="Directory holding the ledger and recorded CSVs (default: ./data)",
    )
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--db", type=Path, help="Ledger path (default: <data-dir>/arena.db)")
    source.add_argument(
        "--no-ledger", action="store_true", help="Replay CSVs for an arena without persistence"
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
        help="Seconds between wallet refreshes (default: 2.0)",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Print one snapshot, then exit (no broker needed)",
    )
    return parser.parse_args()


async def run(args: argparse.Namespace) -> None:
    # Signals first, before any await: a Ctrl-C while the config, the instrument cache
    # or the Kafka connection is still booting must take the clean path too. Installing
    # this after the boot left an early interrupt to the default handler, which cancels
    # the loop mid-connect and hangs the process inside the TUI.
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError, RuntimeError):
            loop.add_signal_handler(sig, stop.set)

    data_dir = Path(args.data_dir)
    db = args.db or data_dir / DEFAULT_DB_PATH.name
    mirror: LedgerPortfolioMirror | PortfolioMirror
    if not args.no_ledger and (args.db is not None or db.exists()):
        mirror = LedgerPortfolioMirror(db, data_dir)
        mirror.sync()
    else:
        mirror = build_csv_mirror(data_dir, args.config)
        if not mirror.sync() and mirror.session_file is None:
            print(f"ERROR: no wallet ledger or trades_*.csv in {data_dir}")
            return

    if args.once:
        # The Live layout divides the terminal height between panels; without a tty
        # there is no height to divide, so print the panels stacked instead.
        console = Console(width=140)
        console.print(mirror.view._build_summary_cards())
        console.print(mirror.view._build_positions_table())
        console.print(mirror.view._build_trade_log())
        print(f"Wallet source: {mirror.session_file or mirror.snapshot_file}")
        return

    client = Client.connect(args.bootstrap_servers)
    state: dict[str, Live] = {}

    group_id = monitor_group_id()

    @client.broker.subscriber(PRICE_TOPIC, group_id=group_id)
    async def handle_price_update(ticker: TickerMessage) -> None:
        mirror.update_price(ticker)
        if state:
            mirror.view.rerender()

    async def poll_wallet() -> None:
        while True:
            await asyncio.sleep(args.refresh)
            if mirror.sync():
                if state:
                    # A new session file means a fresh view; reattach and repaint.
                    mirror.view.attach_live(state["live"])
                    mirror.view.rerender()

    print(f"Monitoring {mirror.session_file or mirror.snapshot_file} (read-only, no wallet of its own).")
    print(f"Price feed: {PRICE_TOPIC} as consumer group '{group_id}' (always the live edge).")

    # A bare @broker.subscriber is inert until a Worker runs the app; with an empty
    # node list this worker does nothing but keep that price subscriber alive.
    worker = Worker(client, nodes=[])
    poller = asyncio.create_task(poll_wallet())

    if stop.is_set():
        # The operator interrupted the boot; nothing is running yet, so just leave.
        await client.aclose()
        return

    try:
        # start()/stop(), not run(): run() installs its own signal handlers, and inside
        # a TUI that owns the screen its shutdown never completes — Ctrl-C looks dead.
        # The documented embedded surface leaves the signals to us.
        await worker.start()
        with Live(mirror.view._build_layout(), auto_refresh=False, screen=True) as live:
            state["live"] = live
            mirror.view.attach_live(live)
            mirror.view.rerender()
            await stop.wait()
    finally:
        # Idempotent, and safe even when start() failed before _app existed.
        await worker.stop()
        poller.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await poller
        # Stop the consumer explicitly: the Kafka group is per run, and a half-closed
        # client would hold the partition until the session times out.
        await client.aclose()


def main() -> None:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s — %(message)s")
    try:
        asyncio.run(run(parse_args()))
    except KeyboardInterrupt:
        # A SIGINT that lands before the loop installs its handler is still a clean exit.
        pass


if __name__ == "__main__":
    main()
