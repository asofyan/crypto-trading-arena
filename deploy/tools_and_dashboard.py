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
from arena.execution import LiveBinanceExecutor
from arena.live_session import HALTED, LiveSession, LiveState
from arena.fees import FeeModel
from arena.instruments import SymbolRules, load_cache, parse_exchange_info
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
from arena.risk import DEFAULT_HALT_PATH, LiveLimits, RiskGates
from config import TradingConfig, load_config_strict
from exchanges.binance_spot import (
    BINANCE_SPOT_REST,
    BINANCE_SPOT_TESTNET_REST,
    BinanceSpotClient,
)

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


def halt(reason: str, *, path: Path = DEFAULT_HALT_PATH) -> None:
    """Disarm live trading durably, from any process.

    Used when the venue tells us to stop (IP ban, credential error) — the one class of
    failure where continuing to retry makes things strictly worse.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{reason}\n", encoding="utf-8")
    logging.getLogger(__name__).error("LIVE TRADING HALTED: %s (flag: %s)", reason, path)


def build_live_runtime(
    store: AccountStore,
    trading: TradingConfig,
    ledger: Ledger | None,
    state: LiveState,
) -> tuple[LiveBinanceExecutor | None, str]:
    """Wire real orders, or explain why not. Never trades without a ledger."""
    live = trading.live
    if ledger is None:
        return None, "live mode needs wallet persistence; remove --no-ledger"
    if trading.exchange != "binance":
        return None, f"live execution is only implemented for Binance, not {trading.exchange}"

    account_name = sorted(set(live.agent_accounts.values()))[0]
    account = live.accounts[account_name]
    base_url = BINANCE_SPOT_TESTNET_REST if account.testnet else BINANCE_SPOT_REST
    client = BinanceSpotClient(
        account.api_key,
        account.api_secret,
        base_url=base_url,
        recv_window_ms=live.recv_window_ms,
    )
    try:
        client.sync_time()
        info = client.exchange_info(sorted(live.allow_symbols))
        client.remember_symbols(info)
    except Exception as exc:  # credentials, network, venue refusal: do not trade blind
        client.close()
        return None, f"could not reach {base_url} to validate live trading: {exc}"

    # The venue's own rules, freshly fetched: the cached file is for paper mode, where
    # the engine may be offline. A live order must be sized against what the venue says
    # right now.
    rules = parse_exchange_info(info)
    store.set_instrument_rules(rules)

    gates = RiskGates(
        LiveLimits(
            allow_symbols=tuple(s.upper() for s in live.allow_symbols),
            max_order_pct_of_equity=live.max_order_pct_of_equity,
            max_position_pct_of_equity=live.max_position_pct_of_equity,
            max_order_notional_usdt=live.max_order_notional_usdt,
            max_daily_loss_pct=live.max_daily_loss_pct,
            max_orders_per_minute=live.max_orders_per_minute,
            max_price_age_seconds=live.max_price_age_seconds,
        ),
        is_halted=state.is_halted,
    )
    executor = LiveBinanceExecutor(
        store,
        client,
        ledger,
        account=account_name,
        agent_accounts=dict(live.agent_accounts),
        rules=rules,
        gates=gates,
        dry_run=live.dry_run,
        on_fatal=lambda exc: (
            state.set(HALTED, f"venue refused further orders: {exc}"),
            halt(f"venue refused further orders: {exc}"),
        ),
    )
    summary = (
        f"live {'(DRY-RUN, no orders sent)' if live.dry_run else '(REAL MONEY)'} "
        f"account={account_name} venue={base_url} "
        f"agents={', '.join(sorted(live.agent_accounts))} "
        f"symbols={', '.join(sorted(rules))} | caps: order "
        f"{live.max_order_pct_of_equity:g}%, position "
        f"{live.max_position_pct_of_equity if live.max_position_pct_of_equity is not None else 'off'}%,"
        f" daily loss {live.max_daily_loss_pct:g}%, "
        f"{live.max_orders_per_minute} orders/min"
    )
    return executor, summary


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


def resolve_instrument_rules(
    trading: TradingConfig,
) -> tuple[dict[str, SymbolRules], str]:
    """Venue rules for the subscribed symbols, or the legacy 1-decimal fallback.

    Returns the rules plus a one-line summary for the startup banner. An empty dict is
    a supported outcome, not an error: every symbol then keeps the coarse paper rule,
    which is what a Coinbase deployment or a cache-less first start needs.
    """
    if trading.paper.instrument_rules == "simple":
        return {}, "simple (legacy 1-decimal rule)"

    cached = load_cache(trading.instrument_cache)
    if not cached:
        return {}, (
            f"simple — no usable cache at {trading.instrument_cache} "
            "(run `uv run python -m arena.instruments --refresh`)"
        )

    subscribed = (
        trading.binance_symbols if trading.exchange == "binance" else trading.coinbase_products
    )
    rules = {s.upper(): cached[s.upper()] for s in subscribed if s.upper() in cached}
    uncached = [s for s in subscribed if s.upper() not in cached]
    summary = f"venue ({len(rules)}/{len(subscribed)} subscribed symbol(s) cached)"
    if uncached:
        # Say it out loud: those symbols silently trade on the coarse rule instead.
        summary += f"; uncached: {', '.join(uncached)}"
    return rules, summary


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
    arena_config = load_config_strict(args.config)
    trading = arena_config.trading
    taker_bps = trading.fees.taker_bps
    store.set_fee_model(FeeModel(taker_bps=taker_bps))
    print(f"Trading fee: {taker_bps} bps ({taker_bps / 100:.2f}% taker) applied to every fill")

    # Venue rules make paper rehearse the real constraints (step size, min notional),
    # so an order this engine accepts is an order the venue accepts.
    instrument_rules, rules_summary = resolve_instrument_rules(trading)
    store.set_instrument_rules(instrument_rules)
    print(f"Instrument rules: {rules_summary}")
    for symbol in sorted(instrument_rules):
        rule = instrument_rules[symbol]
        print(
            f"  - {symbol}: step {rule.step_size:g}, min notional "
            f"{rule.min_notional:g} {rule.quote_asset}"
        )

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

    # ── Live execution (or paper) ────────────────────────────────
    # Built before the worker serves tool nodes: a live executor that cannot validate
    # its credentials must leave the arena in paper mode rather than half-armed.
    mode = trading.mode
    session: LiveSession | None = None
    live_client = None
    if mode == "live":
        live_state = LiveState(ledger) if ledger is not None else None
        executor, live_summary = (
            build_live_runtime(store, trading, ledger, live_state)
            if live_state is not None
            else (None, "live mode needs wallet persistence; remove --no-ledger")
        )
        print(f"Execution: {live_summary}")
        if executor is not None and live_state is not None:
            store.set_executor(executor)
            live_client = executor.client
            session = LiveSession(
                client=live_client,
                store=store,
                ledger=ledger,
                executor=executor,
                rules=executor.rules,
                mapped_agents=tuple(sorted(trading.live.agent_accounts)),
                reserve_usdt=trading.live.reserve_usdt,
                deposit_policy=trading.live.deposit_policy,
                sync_tolerance_usdt=trading.live.sync_tolerance_usdt,
                refresh_seconds=trading.live.balance_refresh_seconds,
                state=live_state,
            )
            report = session.start()
            print(report.summary())
            print(f"Live session: {session.state.state} {session.state.reason}".strip())
        else:
            mode = "paper"
            print("Execution: falling back to PAPER — live trading is not armed")
    else:
        print("Execution: paper (simulated fills at the best bid/ask)")

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
        if session is not None:
            session.stop()
        if live_client is not None:
            live_client.close()
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
