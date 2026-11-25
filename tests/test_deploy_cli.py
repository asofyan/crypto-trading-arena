"""Tests for the deploy entrypoint's CLI surface.

These stay off the network: only argument parsing and the startup wallet summary are
exercised, never Kafka. The argv drift guard matters because ``manage_bot.py start``
builds the child process command from its own flag list.
"""

from __future__ import annotations

import sqlite3
import sys

import pytest

from arena.ledger import DEFAULT_DB_PATH, Ledger
from arena.models import INITIAL_CASH, AgentAccount
from deploy import tools_and_dashboard as deploy_cli


def test_manage_bot_command_is_accepted_by_deploy_parser(monkeypatch, tmp_path):
    import manage_bot

    db = tmp_path / "arena.db"
    command = manage_bot.build_command(
        headless=True,
        bootstrap_servers="kafka:9092",
        config="config.json",
        data_dir=str(tmp_path),
        db=str(db),
    )
    # Drop ["python", "-m", "deploy.tools_and_dashboard"] to get the module's argv.
    monkeypatch.setattr(sys, "argv", ["tools_and_dashboard", *command[3:]])

    args = deploy_cli.parse_args()

    assert args.headless is True
    assert args.no_ledger is False
    assert args.bootstrap_servers == "kafka:9092"
    assert args.db == str(db)


def test_parse_args_defaults_to_persistent_ledger(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["tools_and_dashboard", "--bootstrap-servers", "b:9092"])

    args = deploy_cli.parse_args()

    assert args.db == str(DEFAULT_DB_PATH)
    assert args.no_ledger is False
    assert args.headless is False


def test_parse_args_accepts_opt_out_flags(monkeypatch, tmp_path):
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "tools_and_dashboard",
            "--bootstrap-servers",
            "b:9092",
            "--db",
            str(tmp_path / "custom.db"),
            "--no-ledger",
            "--headless",
        ],
    )

    args = deploy_cli.parse_args()

    assert args.db == str(tmp_path / "custom.db")
    assert args.no_ledger is True
    assert args.headless is True


def test_describe_wallet_reports_fresh_ledger(tmp_path):
    text = deploy_cli.describe_wallet({}, tmp_path / "arena.db")

    assert "Fresh ledger" in text
    assert f"${INITIAL_CASH:,.0f}" in text


def test_describe_wallet_lists_hydrated_agents(tmp_path):
    accounts = {
        "momentum": AgentAccount(cash=99_000.0),
        "value": AgentAccount(cash=101_250.0),
    }
    accounts["momentum"].positions["BTC-USD"] = 0.02

    text = deploy_cli.describe_wallet(accounts, tmp_path / "arena.db")

    lines = text.splitlines()
    assert lines[0].startswith("Hydrated 2 agent(s)")
    assert any("momentum: $99,000.00 cash, 1 open position(s)" in line for line in lines)
    assert any("value: $101,250.00 cash, 0 open position(s)" in line for line in lines)


# ── Ledger startup failures are reported, not raised as tracebacks ───


def test_open_ledger_or_exit_returns_a_usable_ledger(tmp_path):
    ledger = deploy_cli.open_ledger_or_exit(tmp_path / "arena.db")
    try:
        assert ledger.load_accounts() == {}
    finally:
        ledger.close()


def test_open_ledger_or_exit_reports_a_second_writer(tmp_path, capsys):
    path = tmp_path / "arena.db"
    with Ledger(path):
        with pytest.raises(SystemExit) as excinfo:
            deploy_cli.open_ledger_or_exit(path)

    assert excinfo.value.code == 1
    out = capsys.readouterr().out
    assert "another engine already owns this ledger" in out
    assert "--db" in out
    assert "--no-ledger" in out


def test_open_ledger_or_exit_reports_a_schema_mismatch(tmp_path, capsys):
    path = tmp_path / "arena.db"
    with Ledger(path):
        pass
    raw = sqlite3.connect(path)
    raw.execute("UPDATE meta SET value = '999' WHERE key = 'schema_version'")
    raw.commit()
    raw.close()

    with pytest.raises(SystemExit) as excinfo:
        deploy_cli.open_ledger_or_exit(path)

    assert excinfo.value.code == 1
    out = capsys.readouterr().out
    assert str(path) in out
    assert "--db" in out
    assert "Traceback" not in out


def test_open_ledger_or_exit_reports_an_unusable_path(tmp_path, capsys):
    """A directory where the DB should be is an OSError/sqlite3 error, not a crash."""
    blocked = tmp_path / "not-a-file"
    blocked.mkdir()

    with pytest.raises(SystemExit) as excinfo:
        deploy_cli.open_ledger_or_exit(blocked)

    assert excinfo.value.code == 1
    out = capsys.readouterr().out
    assert str(blocked) in out
    assert "--no-ledger" in out


# ── The degraded marker is cleared only once the engine really serves ───


class _RecordingWorker:
    """Stands in for ``calfkit.Worker``: records lifecycle hooks instead of Kafka I/O."""

    def __init__(self) -> None:
        self.after_startup_hooks = []

    def after_startup(self, fn):
        self.after_startup_hooks.append(fn)
        return fn

    def go_live(self) -> None:
        """Model the broker coming up: FastStream's ``after_startup`` runs now."""
        assert self.after_startup_hooks, "no hook registered; the marker would never clear"
        for hook in self.after_startup_hooks:
            assert hook(None) is None


def test_degraded_marker_survives_a_startup_that_never_serves(make_store, make_ledger):
    """S1: a refused Kafka bootstrap must not erase the previous divergence record.

    Hydrating from disk (``attach_ledger``) happens before the broker is anywhere in
    sight, so clearing the marker there makes ``manage_bot.py status`` claim all is
    well after a start that never traded a single fill.
    """
    ledger = make_ledger()
    ledger.mark_degraded("momentum", "2025-01-01T00:00:00")
    store = make_store(taker_bps=0)
    store.attach_ledger(ledger)  # startup step 1: hydrate from disk
    worker = _RecordingWorker()
    deploy_cli.clear_degraded_marker_when_serving(worker, store)

    # Startup step 2 (Client.connect / broker start) would fail right here.
    assert ledger.degraded_marker() is not None, "a failed start erased the record"

    worker.go_live()  # the broker is up: this startup really succeeded
    assert ledger.degraded_marker() is None


# ── Instrument rules resolution (venue-accurate paper fills) ────


_SOLUSDT_INFO = {
    "symbols": [
        {
            "symbol": "SOLUSDT",
            "status": "TRADING",
            "baseAsset": "SOL",
            "quoteAsset": "USDT",
            "filters": [
                {"filterType": "LOT_SIZE", "stepSize": "0.00100000", "minQty": "0.00100000"},
                {
                    "filterType": "NOTIONAL",
                    "minNotional": "5.00000000",
                    "applyMinToMarket": True,
                },
            ],
        }
    ]
}


def _cached_rules(tmp_path) -> str:
    from arena.instruments import parse_exchange_info, save_cache

    path = tmp_path / "instruments.json"
    save_cache(parse_exchange_info(_SOLUSDT_INFO), path)
    return str(path)


def test_resolve_instrument_rules_keeps_only_subscribed_symbols(tmp_path):
    from config import TradingConfig

    trading = TradingConfig.model_validate(
        {
            "exchange": "binance",
            "binance_symbols": ["SOLUSDT", "XRPUSDT"],
            "instrument_cache": _cached_rules(tmp_path),
        }
    )

    rules, summary = deploy_cli.resolve_instrument_rules(trading)

    assert set(rules) == {"SOLUSDT"}
    assert "1/2" in summary
    assert "uncached: XRPUSDT" in summary


def test_resolve_instrument_rules_simple_mode_ignores_the_cache(tmp_path):
    from config import TradingConfig

    trading = TradingConfig.model_validate(
        {
            "paper": {"instrument_rules": "simple"},
            "instrument_cache": _cached_rules(tmp_path),
        }
    )

    rules, summary = deploy_cli.resolve_instrument_rules(trading)

    assert rules == {}
    assert summary.startswith("simple")


def test_resolve_instrument_rules_missing_cache_tells_the_operator_how_to_fix_it(tmp_path):
    from config import TradingConfig

    trading = TradingConfig.model_validate(
        {"instrument_cache": str(tmp_path / "absent.json"), "exchange": "binance"}
    )

    rules, summary = deploy_cli.resolve_instrument_rules(trading)

    assert rules == {}
    assert "arena.instruments --refresh" in summary
