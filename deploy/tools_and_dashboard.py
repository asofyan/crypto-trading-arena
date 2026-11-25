import argparse
import asyncio
import logging
import sqlite3
import sys
from pathlib import Path

from dotenv import load_dotenv
from rich.live import Live

from calfkit import Client, Worker
from exchanges import (
    PRICE_TOPIC,
    TickerMessage,
)
from arena.account_store import AccountStore
from arena.fees import FeeModel
from arena.ledger import DEFAULT_DB_PATH, Ledger, LedgerInUseError, LedgerSchemaError
from arena.models import INITIAL_CASH, AgentAccount
from arena.tools import (
    calculator,
    execute_trade,
    get_portfolio,
    price_book,
    store,
    view,
)
from config import load_config_strict

# Tools & Price Feed — Deploys trading tool workers and subscribes
# to the Kafka price topic published by the connector.
#
# The price subscriber hydrates the shared price book that the trading
# tools read from when executing trades.
#
# Usage:
#     uv run python -m deploy.tools_and_dashboard
#
# Prerequisites:
#     - Kafka broker running at localhost:9092

load_dotenv()


def parse_args():
    parser = argparse.ArgumentParser(
        description="Deploy trading tools, price feed, and dashboard.",
    )
    parser.add_argument(
        "--bootstrap-servers",
        required=True,
        help="Kafka bootstrap servers address",
    )
    parser.add_argument(
        "--snapshot-interval",
        type=float,
        default=600.0,
        help="Seconds between portfolio snapshots (0 disables recording)",
    )
    parser.add_argument(
        "--data-dir",
        type=str,
        default="./data",
        help="Output directory for CSV data files",
    )
    parser.add_argument(
        "--config",
        default="config.json",
        help="Path to config file (default: config.json).",
    )
    parser.add_argument(
        "--db",
        default=str(DEFAULT_DB_PATH),
        help=(
            "SQLite wallet ledger path (default: %(default)s). The ledger is what lets a "
            "restart keep cash/positions instead of resetting every agent."
        ),
    )
    parser.add_argument(
        "--no-ledger",
        action="store_true",
        help="Disable wallet persistence (volatile wallets; state resets on restart)",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run without the Rich Live dashboard, for detached/supervised runs",
    )
    return parser.parse_args()


def describe_wallet(accounts: dict[str, AgentAccount], path: str | Path) -> str:
    """Startup status block, so an operator can tell a resumed session from a fresh one."""
    if not accounts:
        return f"Fresh ledger at {path} — every agent starts at ${INITIAL_CASH:,.0f}"

    lines = [f"Hydrated {len(accounts)} agent(s) from {path}:"]
    for agent_id, account in sorted(accounts.items()):
        lines.append(
            f"  - {agent_id}: ${account.cash:,.2f} cash, "
            f"{len(account.positions)} open position(s)"
        )
    return "\n".join(lines)


def open_ledger_or_exit(path: str | Path) -> Ledger:
    """Open the writer ledger or exit 1 with an actionable message.

    A raw traceback here is unreadable for an operator whose only mistake was
    starting a second engine (or pointing ``--db`` at the wrong file).
    """
    try:
        return Ledger(path)
    except LedgerInUseError:
        print(
            f"\nERROR: another engine already owns this ledger ({path}).\n"
            f"  Only one engine may write a ledger at a time.\n"
            f"  Stop the running engine (manage_bot.py stop), pass --db <other-path> "
            f"for a separate ledger, or pass --no-ledger to run without persistence."
        )
    except (LedgerSchemaError, sqlite3.Error, OSError) as exc:
        print(
            f"\nERROR: cannot open the wallet ledger at {path}: {exc}\n"
            f"  Move the unusable file aside, pass --db <other-path> to use a "
            f"different ledger, or pass --no-ledger to run without persistence."
        )
    sys.exit(1)


def clear_degraded_marker_when_serving(worker: Worker, store: AccountStore) -> None:
    """Clear a stale degraded marker only once this startup has really succeeded.

    ``store.attach_ledger`` hydrates from disk, which proves nothing about the rest of
    the boot: a refused Kafka bootstrap aborts the process before the first fill, and
    clearing the marker there would erase the operator's only cross-process record of
    a past divergence (ADR 0003 promises it is cleared on the next *successful*
    startup). ``after_startup`` runs with the broker up, so the engine is serving by
    then — that is the earliest honest point.
    """
    worker.after_startup(lambda _ctx: store.mark_ledger_healthy())


async def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
        datefmt="%H:%M:%S",
    )

    args = parse_args()

    print("=" * 50)
    print("Tools & Price Feed Deployment")
    print("=" * 50)

    # Resolve the taker fee from config — the single source of truth shared with
    # the price-feed connector, so the fee charged here always matches the fee the
    # connector advertises to agents.
    taker_bps = load_config_strict(args.config).trading.fees.taker_bps
    store.set_fee_model(FeeModel(taker_bps=taker_bps))
    print(f"Trading fee: {taker_bps} bps ({taker_bps / 100:.2f}% taker) applied to every fill")

    # ── Wallet persistence ───────────────────────────────────────
    # Hydrate before the worker starts serving fills, so a restart resumes the
    # previous session rather than handing every agent a fresh $100.
    ledger = None
    if args.no_ledger:
        print("\nWARNING: persistence disabled (--no-ledger) — wallets reset on every restart")
    else:
        ledger = open_ledger_or_exit(args.db)
        store.attach_ledger(ledger)
        print(f"\n{describe_wallet(store.accounts, ledger.path)}")

    print(f"\nConnecting to Kafka broker at {args.bootstrap_servers}...")
    client = Client.connect(args.bootstrap_servers)

    tools = [execute_trade, get_portfolio, calculator]
    worker = Worker(client, nodes=tools)
    if ledger is not None:
        clear_degraded_marker_when_serving(worker, store)

    # ── Tool nodes ───────────────────────────────────────────────
    print("\nRegistering trading tool nodes...")
    for tool in tools:
        print(f"  - {tool.tool_schema.name} (topic: {tool.subscribe_topics[0]})")

    # ── Price subscriber ─────────────────────────────────────────
    @client.broker.subscriber(PRICE_TOPIC, group_id="tools-dashboard")
    async def handle_price_update(ticker: TickerMessage) -> None:
        price_book.update(ticker.model_dump())
        view.rerender()

    # ── Data recorder (optional) ────────────────────────────────
    recorder = None
    if args.snapshot_interval > 0:
        from arena.recorder import DataRecorder

        recorder = DataRecorder(data_dir=args.data_dir)
        store.attach_recorder(recorder)
        print(f"\nCSV recording enabled → {args.data_dir}/ (snapshots every {args.snapshot_interval}s)")

    if args.headless:
        # view.rerender() no-ops without an attached Live, so price callbacks stay cheap.
        print("\nHeadless mode: live dashboard disabled (logs only)")
    else:
        print("\nStarting portfolio dashboard (prices via Kafka)...")

    try:
        if args.headless:
            if recorder is not None:
                recorder.start_snapshot_loop(store, interval=args.snapshot_interval)
            await worker.run()
        else:
            with Live(view._build_layout(), auto_refresh=False, screen=True) as live:
                view.attach_live(live)
                if recorder is not None:
                    recorder.start_snapshot_loop(store, interval=args.snapshot_interval)
                await worker.run()
    finally:
        if recorder is not None:
            await recorder.close()
        # Close after the recorder so the final CSV snapshot reads coherent state.
        if ledger is not None:
            ledger.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nTools and price feed stopped.")
