"""Tests for the deploy entrypoint's CLI surface.

These stay off the network: only argument parsing and the startup wallet summary are
exercised, never Kafka. The argv drift guard matters because ``manage_bot.py start``
builds the child process command from its own flag list.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import time

import pytest

from arena.ledger import DEFAULT_DB_PATH, Ledger
from arena.models import INITIAL_CASH, AgentAccount
from arena.trade_policy import ExitFloor
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


# ── Agent entrypoint: config.json owns the strategy assignment ──
# `--strategy` used to be required even with --from-config, so a launcher that
# passed the wrong name silently ran a strategy the config never assigned.


def _agent_config_file(tmp_path, *, strategy="scalper", model="hungerg-free"):
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "llm_providers": {
                    "p": {"api_key": "sk-test", "base_url": "https://llm.example/v1"}
                },
                "agents": [
                    {
                        "name": "hungerg",
                        "provider": "p",
                        "model": model,
                        "strategy": strategy,
                    }
                ],
            }
        )
    )
    return path


def _parse_from_config(monkeypatch, path, *extra):
    import deploy.agent as agent_module

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "agent",
            "--from-config", "hungerg",
            "--bootstrap-servers", "b:9092",
            "--config-path", str(path),
            *extra,
        ],
    )
    return agent_module, agent_module.parse_args()


def test_from_config_supplies_the_strategy_so_it_cannot_drift(monkeypatch, tmp_path):
    from config import load_config

    path = _agent_config_file(tmp_path, strategy="scalper")
    module, args = _parse_from_config(monkeypatch, path)

    assert module.apply_agent_config(args, load_config(path)) is True
    assert args.strategy == "scalper"
    assert args.name == "hungerg"
    assert args.model_ids == ["hungerg-free"]
    assert args.base_url == "https://llm.example/v1"


def test_explicit_strategy_overrides_config_with_a_notice(monkeypatch, tmp_path, capsys):
    from config import load_config

    path = _agent_config_file(tmp_path, strategy="scalper")
    module, args = _parse_from_config(monkeypatch, path, "--strategy", "default")

    assert module.apply_agent_config(args, load_config(path)) is True
    assert args.strategy == "default"
    assert "overrides config strategy" in capsys.readouterr().out


def test_from_config_carries_a_model_pool(monkeypatch, tmp_path):
    from config import load_config

    path = _agent_config_file(tmp_path, model=["hungerg-free", "hungerg-mini"])
    module, args = _parse_from_config(monkeypatch, path)

    assert module.apply_agent_config(args, load_config(path)) is True
    assert args.model_ids == ["hungerg-free", "hungerg-mini"]


def test_payload_flag_selects_the_lean_topic(monkeypatch):
    import deploy.agent as agent_module
    from exchanges import AGENT_INPUT_TOPIC, AGENT_INPUT_TOPIC_LEAN

    monkeypatch.setattr(
        sys, "argv", ["agent", "--bootstrap-servers", "b:9092", "--model-id", "m", "--payload", "lean"]
    )

    args = agent_module.parse_args()

    assert args.payload == "lean"
    assert agent_module.input_topic_for(args.payload) == AGENT_INPUT_TOPIC_LEAN
    assert agent_module.input_topic_for("full") == AGENT_INPUT_TOPIC


def test_an_explicit_payload_overrides_config_with_a_notice(monkeypatch, tmp_path, capsys):
    """The flag is documented as an override; it must not be overwritten by the config."""
    from config import load_config

    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "llm_providers": {"p": {"api_key": "sk-test", "base_url": "https://llm.example/v1"}},
                "agents": [{"name": "hungerg", "provider": "p", "model": "m", "payload": "lean"}],
            }
        )
    )
    module, args = _parse_from_config(monkeypatch, path, "--payload", "full")

    assert module.apply_agent_config(args, load_config(path)) is True
    assert args.payload == "full"
    assert "payload" in capsys.readouterr().out


def test_from_config_carries_the_payload_profile(monkeypatch, tmp_path):
    from config import load_config

    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "llm_providers": {"p": {"api_key": "sk-test", "base_url": "https://llm.example/v1"}},
                "agents": [
                    {"name": "hungerg", "provider": "p", "model": "m", "payload": "lean"}
                ],
            }
        )
    )
    module, args = _parse_from_config(monkeypatch, path)

    assert module.apply_agent_config(args, load_config(path)) is True
    assert args.payload == "lean"


def test_model_id_flag_is_repeatable(monkeypatch):
    """A pool is one agent with several models, so the flag repeats rather than mutates."""
    import deploy.agent as agent_module

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "agent",
            "--bootstrap-servers", "b:9092",
            "--model-id", "gpt-a",
            "--model-id", "gpt-b",
        ],
    )

    args = agent_module.parse_args()

    assert args.model_ids == ["gpt-a", "gpt-b"]


def test_a_lone_model_keeps_a_plain_client():
    """One model must not pay for pool machinery it cannot use."""
    from calfkit import OpenAIModelClient

    import deploy.agent as agent_module

    client = agent_module.build_model_client(
        ["gpt-a"], base_url="https://llm.example/v1", api_key="sk-test"
    )

    assert isinstance(client, OpenAIModelClient)
    assert client.model_name == "gpt-a"


def test_build_model_client_refuses_a_pool_that_cannot_rotate():
    import deploy.agent as agent_module

    with pytest.raises(ValueError, match="repeats"):
        agent_module.build_model_client(["gpt-a", "gpt-a"], api_key="sk-test")


def test_pooled_clients_hand_retrying_to_the_pool():
    """The OpenAI SDK retries twice and sleeps on Retry-After inside the tick."""
    import deploy.agent as agent_module

    pool = agent_module.build_model_client(["gpt-a", "gpt-b"], api_key="sk-test")

    assert [model.client.max_retries for model in pool.models] == [0, 0]


def test_a_negative_cooldown_is_rejected(monkeypatch):
    import deploy.agent as agent_module

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "agent",
            "--bootstrap-servers", "b:9092",
            "--model-id", "gpt-a",
            "--model-cooldown-seconds", "-1",
        ],
    )

    with pytest.raises(SystemExit):
        agent_module.parse_args()


def test_an_unusable_cooldown_value_is_rejected(monkeypatch):
    import deploy.agent as agent_module

    for value in ("-1", "nan", "inf"):
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "agent",
                "--bootstrap-servers", "b:9092",
                "--model-id", "gpt-a",
                "--model-cooldown-seconds", value,
            ],
        )
        with pytest.raises(SystemExit):
            agent_module.parse_args()


def test_a_single_model_says_the_cooldown_does_nothing(capsys):
    import deploy.agent as agent_module

    agent_module.build_model_client(["gpt-a"], api_key="sk-test", model_cooldown_seconds=90)

    assert "only applies to a model pool" in capsys.readouterr().out


def test_a_whitespace_padded_model_is_not_an_override(monkeypatch, tmp_path, capsys):
    from config import load_config

    path = _agent_config_file(tmp_path)
    module, args = _parse_from_config(monkeypatch, path, "--model-id", " hungerg-free ")

    assert module.apply_agent_config(args, load_config(path)) is True
    assert args.model_ids == ["hungerg-free"]
    assert "overrides config model" not in capsys.readouterr().out


def test_an_explicit_model_pool_overrides_config_with_a_notice(monkeypatch, tmp_path, capsys):
    from config import load_config

    path = _agent_config_file(tmp_path)
    module, args = _parse_from_config(monkeypatch, path, "--model-id", "other-model")

    assert module.apply_agent_config(args, load_config(path)) is True
    assert args.model_ids == ["other-model"]
    assert "overrides config model" in capsys.readouterr().out


def test_several_models_become_a_round_robin_pool():
    import deploy.agent as agent_module
    from arena.model_pool import RoundRobinModel

    client = agent_module.build_model_client(
        ["gpt-a", "gpt-b"],
        base_url="https://llm.example/v1",
        api_key="sk-test",
        model_cooldown_seconds=5,
    )

    assert isinstance(client, RoundRobinModel)
    assert [model.model_name for model in client.models] == ["gpt-a", "gpt-b"]
    assert client.cooldown_seconds == 5


def test_from_config_reports_an_unknown_agent(monkeypatch, tmp_path):
    from config import load_config

    path = _agent_config_file(tmp_path)
    module, args = _parse_from_config(monkeypatch, path)
    args.from_config = "nobody"

    assert module.apply_agent_config(args, load_config(path)) is False


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


# ── The hold watchdog: the half of the escape hatch that submits ─
#
# The exit floor permits a close past the hold limit and stops there. In the measured
# run nothing else pulled, so four round trips sat 800-3100 s past a 3600 s limit and
# the scorecard — correctly — called it a guard that did not fire. These tests pin the
# deploy wiring: armed from the same config number as the floor, published in the startup
# banner, and refused when the operator turned the limit off.


def test_hold_watchdog_is_armed_from_the_configured_hold_limit(make_store, capsys):
    watchdog = deploy_cli.arm_hold_watchdog(
        make_store(),
        ExitFloor(min_profit_bps=50.0, stop_bps=25.0, max_hold_seconds=3600.0),
    )

    assert watchdog is not None
    banner = capsys.readouterr().out
    assert "Hold watchdog:" in banner
    assert "3600" in banner


def test_hold_watchdog_stays_off_without_an_exit_floor(make_store, capsys):
    """No floor means no band and no limit: a watchdog here would invent a rule."""
    assert deploy_cli.arm_hold_watchdog(make_store(), None) is None
    assert "Hold watchdog:" not in capsys.readouterr().out


def test_hold_watchdog_stays_off_when_the_timeout_is_removed(make_store, capsys):
    """``max_hold_seconds: null`` is the operator saying "hold it" — not "close it"."""
    assert deploy_cli.arm_hold_watchdog(make_store(), ExitFloor(max_hold_seconds=None)) is None
    assert "Hold watchdog:" not in capsys.readouterr().out


async def test_the_armed_watchdog_closes_a_position_past_the_limit(make_store):
    """The wiring, end to end: the banner's watchdog is the one that books the close."""
    store = make_store(taker_bps=10)
    assert store.execute_trade("a", "SOL-USD", 0.5, "buy").success
    store.get_or_create("a").avg_entry_ts["SOL-USD"] = time.time() - 4000.0
    watchdog = deploy_cli.arm_hold_watchdog(
        store, ExitFloor(min_profit_bps=50.0, stop_bps=25.0, max_hold_seconds=3600.0)
    )

    [result] = await watchdog.tick()

    assert result.success, result.message
    assert store.get_or_create("a").positions.get("SOL-USD", 0.0) == pytest.approx(0.0)


def test_the_rate_limit_cooldown_is_configurable(monkeypatch):
    """A 429 is a quota window, not a blip: the operator must be able to widen it."""
    import deploy.agent as agent_module

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "agent",
            "--bootstrap-servers", "b:9092",
            "--model-id", "free-model",
            "--model-id", "paid-model",
            "--model-rate-limit-cooldown-seconds", "1800",
        ],
    )

    args = agent_module.parse_args()
    pool = agent_module.build_model_client(
        args.model_ids,
        api_key="sk-test",
        rate_limit_cooldown_seconds=args.model_rate_limit_cooldown_seconds,
    )

    assert pool.rate_limit_cooldown_seconds == 1800


def test_help_renders(monkeypatch):
    """argparse interpolates help text with %, so a literal % must be escaped."""
    import deploy.agent as agent_module

    monkeypatch.setattr(sys, "argv", ["agent", "--help"])
    with pytest.raises(SystemExit):
        agent_module.parse_args()


def test_live_futures_requires_a_dedicated_persistent_ledger(tmp_path):
    with pytest.raises(ValueError, match="dedicated"):
        deploy_cli.validate_ledger_target(str(DEFAULT_DB_PATH.resolve()), futures=True, no_ledger=False)
    with pytest.raises(ValueError, match="persistence"):
        deploy_cli.validate_ledger_target(str(tmp_path / "futures.db"), futures=True, no_ledger=True)
    deploy_cli.validate_ledger_target(str(tmp_path / "futures.db"), futures=True, no_ledger=False)
    deploy_cli.validate_ledger_target(str(DEFAULT_DB_PATH), futures=False, no_ledger=False)


@pytest.mark.parametrize("source, age, expected", [
    ("binance-futures-demo", 1, True),
    ("binance_perp", 1, False),
    ("binance-futures-demo", 6, False),
    ("binance-futures-demo", -10, False),
    (None, 1, False),
])
def test_live_futures_quote_must_match_venue_and_be_fresh(source, age, expected):
    from datetime import datetime, timedelta, timezone
    from types import SimpleNamespace

    now = datetime.now(timezone.utc)
    ticker = SimpleNamespace(
        product_id="ZECUSDT-PERP", source=source,
        time=(now - timedelta(seconds=age)).isoformat(),
        best_bid="99", best_ask="100",
    )
    assert deploy_cli.accept_futures_ticker(
        ticker, "binance-futures-demo", max_age=5, now=now,
    ) is expected


def test_live_futures_builder_routes_long_and_short_via_demo(monkeypatch, tmp_path):
    from decimal import Decimal

    from arena.account_store import AccountStore
    from arena.live_session import ARMED, LiveState
    from arena.price_book import PriceBook
    from config import TradingConfig
    from exchanges import binance_futures
    from exchanges.binance_spot import VenueFill

    class DemoVenue:
        def __init__(self, *args, base_url, **kwargs):
            assert base_url == binance_futures.BINANCE_FUTURES_DEMO_REST
            self.positions = Decimal("0")
            self.balance = Decimal("100")
            self.calls = []

        def sync_time(self):
            pass

        def exchange_info(self, symbols):
            assert symbols == ["BTCUSDT"]
            return {"symbols": [{
                "symbol": "BTCUSDT", "status": "TRADING", "baseAsset": "BTC",
                "quoteAsset": "USDT", "filters": [
                    {"filterType": "MARKET_LOT_SIZE", "stepSize": ".01", "minQty": ".01",
                     "maxQty": "100"},
                ],
            }]}

        def account_config(self):
            return {"canTrade": True, "dualSidePosition": False, "multiAssetsMargin": False}

        def bnb_fee_burn(self):
            return False

        def symbol_config(self, symbol=None):
            assert symbol in (None, "BTCUSDT")
            return [{"symbol": "BTCUSDT", "leverage": 1, "marginType": "CROSSED"}]

        def position_risk(self, symbol=None):
            assert symbol in (None, "BTCUSDT")
            return [] if not self.positions else [{
                "symbol": "BTCUSDT", "positionSide": "BOTH",
                "positionAmt": str(self.positions), "entryPrice": "100",
            }]

        def futures_balances(self):
            return [{
                "asset": "USDT", "balance": str(self.balance),
                "availableBalance": str(self.balance), "accountAlias": "demo-wallet",
            }]

        def open_orders(self):
            return []

        def open_algo_orders(self):
            return []

        def funding_income(self, *args, **kwargs):
            return []

        def place_market_order(self, symbol, side, quantity, client_id, *, reduce_only):
            assert symbol == "BTCUSDT"
            self.calls.append((side, reduce_only))
            self.positions += quantity if side == "buy" else -quantity
            self.balance -= Decimal(".005")
            return VenueFill(client_id, str(len(self.calls)), symbol, side, "FILLED",
                             float(quantity), float(quantity), float(100 * quantity),
                             100., .005, "USDT")

        def close(self):
            pass

    monkeypatch.setattr(binance_futures, "BinanceFuturesClient", DemoVenue)
    monkeypatch.setattr(deploy_cli, "LiveState", lambda ledger: LiveState(
        ledger, halt_path=tmp_path / "LIVE_HALT",
    ))
    config = TradingConfig.model_validate({
        "mode": "live", "exchange": "binance", "binance_perp_symbols": ["BTCUSDT"],
        "live": {
            "market": "perp", "dry_run": False, "allow_symbols": ["BTCUSDT"],
            "accounts": {"demo": {"api_key": "demo", "api_secret": "demo", "testnet": True}},
            "agent_accounts": {"scalper-perp": "demo"}, "max_orders_per_minute": 10,
        },
    })
    book = PriceBook()
    product = "BTCUSDT-PERP"
    book.update({"product_id": product, "price": "100", "best_bid": "100", "best_ask": "100"})
    store = AccountStore(book)
    store.set_agent_markets({"scalper-perp": ["perp"]})
    with Ledger(tmp_path / "futures.db") as ledger:
        store.attach_ledger(ledger)
        session, client = deploy_cli.build_futures_session(store, config, ledger)
        try:
            assert session.start().armed
            assert session.state.state == ARMED
            for side, quantity in (("buy", .1), ("sell", -.1)):
                store.record_portfolio_read(
                    "scalper-perp", trade_count=store.accounts["scalper-perp"].trade_count,
                )
                assert store.execute_trade(
                    "scalper-perp", product, .1, side, allow_flip=False,
                ).success
                assert store.accounts["scalper-perp"].positions[product] == quantity
                assert store.close_position("scalper-perp", product).success
            assert session.recheck().armed
            assert client.calls == [("buy", False), ("sell", True), ("sell", False), ("buy", True)]
            assert len(ledger.recent_fills()) == 4
        finally:
            session.stop()
            client.close()


def test_retirement_monitor_only_manages_the_scalper_openrouter_pool():
    from deploy.agent import monitors_openrouter

    models = ["some/model:free", "openai/gpt-oss-20b"]
    assert monitors_openrouter("scalper-perp", "https://openrouter.ai/api/v1/", models)
    assert not monitors_openrouter("other", "https://openrouter.ai/api/v1", models)
    assert not monitors_openrouter("scalper-perp", "https://openrouter.ai.example/api/v1", models)
    assert not monitors_openrouter("scalper-perp", "https://openrouter.ai/api/v1", models[:1])
    assert not monitors_openrouter(
        "scalper-perp", "https://openrouter.ai/api/v1", [*models, "other/paid"]
    )


async def test_agent_hot_swaps_a_catalog_delisting_without_restarting(monkeypatch, tmp_path):
    import asyncio
    from types import SimpleNamespace

    import httpx

    from deploy import agent as module

    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    state = tmp_path / "data" / "openrouter-scalper-perp.json"
    state.write_text(json.dumps({"old/model:free": {"missing_checks": 1}}))
    monkeypatch.setattr(sys, "argv", [
        "agent", "--name", "scalper-perp", "--strategy", "scalper-perp",
        "--bootstrap-servers", "kafka:9092", "--api-key", "sk-test",
        "--base-url", "https://openrouter.ai/api/v1",
        "--model-id", "old/model:free", "--model-id", "openai/gpt-oss-20b",
    ])
    notified = asyncio.Event()
    alerts = []

    async def send(subject, body):
        alerts.append((subject, body))
        notified.set()

    def router(request):
        assert request.headers["Authorization"] == "Bearer sk-test"
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{
                "id": "new/model:free", "context_length": 32_000,
                "supported_parameters": ["tools"], "pricing": {"prompt": "0", "completion": "0"},
            }]})
        payload = json.loads(request.content)
        assert payload["model"] == "new/model:free"
        instruction = payload["messages"][-1]["content"]
        name = "calculator" if "calculator" in instruction else "get_portfolio"
        arguments = {"expression": "123 * 7"} if name == "calculator" else {}
        return httpx.Response(200, json={"choices": [{
            "finish_reason": "tool_calls", "message": {"tool_calls": [{
                "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)},
            }]},
        }]})

    def make_http(**kwargs):
        return httpx.AsyncClient(transport=httpx.MockTransport(router), **kwargs)

    class Worker:
        def __init__(self, client, *, nodes, group_id):
            self.pool = nodes[0].model_client

        async def run(self):
            assert [model.model_name for model in self.pool.models] == [
                "old/model:free", "openai/gpt-oss-20b",
            ]
            await asyncio.wait_for(notified.wait(), timeout=3)
            assert [model.model_name for model in self.pool.models] == [
                "new/model:free", "openai/gpt-oss-20b",
            ]

    monkeypatch.setattr(module, "httpx", SimpleNamespace(AsyncClient=make_http))
    monkeypatch.setattr(module, "send_monitor_email", send)
    monkeypatch.setattr(module, "Client", SimpleNamespace(connect=lambda _: object()))
    monkeypatch.setattr(module, "Agent", lambda name, **kwargs: SimpleNamespace(**kwargs))
    monkeypatch.setattr(module, "Worker", Worker)

    await module.main()

    assert "new/model:free" in alerts[0][1]
    assert json.loads(state.read_text())["old/model:free"]["replacement"] == "new/model:free"


def test_demo_ling_flash_uses_pinned_structured_fallback():
    from deploy.agent import build_model_client, monitors_openrouter

    base_url = "https://openrouter.ai/api/v1"
    ling = "inclusionai/ling-3.0-flash"
    models = ["some/model:free", ling]
    pool = build_model_client(models, base_url=base_url, api_key="test", single_attempt=True)

    assert pool._paid_fallback == ling
    assert monitors_openrouter("scalper-perp", base_url, models)
    free, paid = pool.models
    assert "extra_body" not in free.settings
    body = paid.settings["extra_body"]
    assert body["provider"] == {
        "only": ["Ant Digital"], "allow_fallbacks": False, "require_parameters": True,
    }
    assert "reasoning" not in body
    schema = body["response_format"]["json_schema"]
    assert schema["strict"] is True
    assert [branch["required"] for branch in schema["schema"]["anyOf"]] == [
        ["action"], ["action", "product_id"], ["action", "product_id", "quantity"],
        ["action", "product_id", "quantity"],
    ]


@pytest.mark.asyncio
async def test_demo_restart_waits_until_reload_needed_and_operator_allows(tmp_path):
    import asyncio
    from types import SimpleNamespace

    flag = tmp_path / "LIVE_HALT"
    flag.touch()
    session = SimpleNamespace(
        ledger_reload_required=False,
        state=SimpleNamespace(halt_file_reason=lambda: "operator" if flag.exists() else None),
    )
    restart = asyncio.create_task(deploy_cli.wait_for_demo_ledger_reload(session, interval=0))
    try:
        await asyncio.sleep(0)
        assert not restart.done()
        session.ledger_reload_required = True
        await asyncio.sleep(0)
        assert not restart.done()
        flag.unlink()
        await asyncio.wait_for(restart, timeout=1)
    finally:
        restart.cancel()
        await asyncio.gather(restart, return_exceptions=True)
