from datetime import UTC, date, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from deploy.market_monitor import (
    Band,
    DailyMonitor,
    Metric,
    RiskSettings,
    candle_ranges,
    load_quotes,
    quote_metrics,
)

NOW = datetime(2026, 11, 27, tzinfo=UTC)


def test_quote_replay_scores_bid_ask_and_rejects_gap():
    quotes = [
        (NOW, 99.9, 100),
        (NOW + timedelta(minutes=1), 101.1, 101.2),
        (NOW + timedelta(minutes=2), 99.3, 99.4),
        (NOW + timedelta(minutes=5), 100, 100.1),
    ]
    metrics = quote_metrics(
        quotes, NOW, NOW + timedelta(minutes=5), Band(100, 50, 120, 10), RiskSettings()
    )
    assert metrics["spread"].samples == 1
    assert metrics["long-target"].value == 100
    assert metrics["long-target"].net_bps == 90
    assert metrics["short-target"].value == 0
    assert metrics["short-target"].net_bps == -60

    incomplete = quote_metrics(
        quotes, NOW + timedelta(minutes=5), NOW + timedelta(minutes=10),
        Band(100, 50, 240, 10), RiskSettings(),
    )
    assert "long-target" not in incomplete


@pytest.mark.asyncio
async def test_candle_paging_and_closed_ranges():
    client = AsyncMock()
    end = NOW + timedelta(minutes=10)
    def row(minute):
        return [int((NOW + timedelta(minutes=minute)).timestamp() * 1000),
                "100", "103", "99", "101", "1"]
    client.get_klines.side_effect = [[row(0)], [row(5), row(10)]]
    candles = await candle_ranges(client, "ZECUSDT", NOW, end)
    assert candles == {NOW: 400, NOW + timedelta(minutes=5): 400}
    assert client.get_klines.await_count == 2


@pytest.mark.asyncio
async def test_persistence_uses_frozen_baseline_and_retries_email(tmp_path):
    path = tmp_path / "alerts.sqlite"
    settings = RiskSettings()
    mail = AsyncMock(side_effect=[OSError("retry"), None, None, None])
    monitor = DailyMonitor(path, settings, Band(100, 50, 3600, 10))
    monitor.evaluate("ZECUSDT-PERP", date(2026, 11, 25),
                     {"spread": Metric(15, 288)}, {"spread": Metric(10, 4032)})
    with pytest.raises(OSError, match="retry"):
        await monitor.deliver(mail)
    monitor.close()

    monitor = DailyMonitor(path, settings, Band(100, 50, 3600, 10))
    await monitor.deliver(mail)
    for day in (26, 27):
        monitor.evaluate("ZECUSDT-PERP", date(2026, 11, day),
                         {"spread": Metric(15, 288)}, {"spread": Metric(15, 4032)})
    await monitor.deliver(mail)
    monitor.evaluate("ZECUSDT-PERP", date(2026, 11, 27),
                     {"spread": Metric(1, 288)}, {"spread": Metric(1, 4032)})
    await monitor.deliver(mail)
    monitor.evaluate("ZECUSDT-PERP", date(2026, 11, 28),
                     {"spread": Metric(10, 288)}, {})
    await monitor.deliver(mail)
    subjects = [call.args[0] for call in mail.await_args_list]
    assert len(subjects) == 4
    assert subjects[0] == subjects[1]
    assert "persistent" in subjects[2]
    assert "recovered" in subjects[3]
    assert "10.00" in mail.await_args_list[2].args[1]
    monitor.close()


@pytest.mark.asyncio
async def test_missed_day_resets_streak_and_opposite_range_is_new_signal(tmp_path):
    monitor = DailyMonitor(tmp_path / "alerts.sqlite", RiskSettings(), Band(100, 50, 3600, 10))
    send = AsyncMock()
    for day, value in [(1, 15), (3, 15), (4, 15), (5, 4)]:
        monitor.evaluate("SNDKUSDT-PERP", date(2026, 11, day),
                         {"candle-range": Metric(value, 288)}, {"candle-range": Metric(10, 4032)})
    await monitor.deliver(send)
    subjects = [call.args[0] for call in send.await_args_list]
    assert len(subjects) == 2
    assert all("anomaly" in subject for subject in subjects)
    assert "down" in send.await_args_list[1].args[1]
    monitor.close()



def test_quote_reader_requires_mainnet_and_rejects_demo_even_with_legacy_enabled(tmp_path):
    symbol = "SNDKUSDT-PERP"
    (tmp_path / "ticks_20261127_000000.csv").write_text(
        "timestamp,product_id,best_bid,best_ask,source\n"
        f"2026-11-27T00:00:00+00:00,{symbol},100,101,binance_perp\n"
        f"2026-11-27T00:01:00+00:00,{symbol},10,11,binance-futures-demo\n"
        f"2026-11-27T00:02:00+00:00,{symbol},1000,1001,\n"
        f"2026-11-27T00:03:00+00:00,{symbol},101,100,binance_perp\n"
    )
    (tmp_path / "ticks_20261127_000400.csv").write_text(
        "timestamp,product_id,best_bid,best_ask\n"
        f"2026-11-27T00:04:00+00:00,{symbol},2000,2001\n"
    )
    args = (tmp_path, {symbol}, NOW, NOW + timedelta(hours=1), UTC)
    assert [quote[1] for quote in load_quotes(*args)[symbol]] == [100]
    assert [quote[1] for quote in load_quotes(*args, allow_unlabeled=True)[symbol]] == [100, 2000]


def test_quote_reader_uses_exchange_time_to_dedupe_replayed_quotes(tmp_path):
    symbol = "ZECUSDT-PERP"
    (tmp_path / "ticks_20261127_000000.csv").write_text(
        "timestamp,product_id,best_bid,best_ask,source,quote_time\n"
        f"2026-11-27T00:00:00+00:00,{symbol},100,101,binance_perp,2026-11-27T00:00:00+00:00\n"
        f"2026-11-27T00:10:00+00:00,{symbol},100,101,binance_perp,2026-11-27T00:00:00+00:00\n"
        f"2026-11-27T00:20:00+00:00,{symbol},99,100,binance_perp,2026-11-26T23:00:00+00:00\n"
        f"2026-11-27T00:30:00+00:00,{symbol},100,101,binance_perp,\n"
    )
    assert load_quotes(tmp_path, {symbol}, NOW, NOW + timedelta(hours=1), UTC) == {
        symbol: [(NOW, 100, 101)],
    }


def test_cli_dry_run_evaluates_configured_pair_without_mail_or_state(tmp_path, monkeypatch, capsys):
    from deploy import market_monitor

    config = tmp_path / "config.json"
    config.write_text('{"trading": {"binance_perp_symbols": ["ZECUSDT"]}}')
    midnight = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    current_start_ms = int((midnight - timedelta(hours=2)).timestamp() * 1000)

    async def get_klines(symbol, interval, *, start_time, end_time):
        assert (symbol, interval) == ("ZECUSDT", "5m")
        return [
            [t, "100", "101" if t < current_start_ms else "102", "100", "100", "1"]
            for t in range(start_time, end_time, 300_000)
        ]

    client = AsyncMock()
    client.__aenter__.return_value = client
    client.get_klines.side_effect = get_klines
    monkeypatch.setattr(market_monitor, "BinanceRESTClient", lambda venue: client)
    monkeypatch.setattr("sys.argv", [
        "market_monitor", "--config-path", str(config), "--data-dir", str(tmp_path),
        "--window-hours", "2", "--baseline-days", "1", "--persistent-evaluations", "1",
        "--dry-run",
    ])
    market_monitor.main()
    assert "ZECUSDT-PERP candle-range persistent" in capsys.readouterr().out
    assert not (tmp_path / "market-monitor.sqlite").exists()


def test_cli_once_signals_candle_failure(tmp_path, monkeypatch):
    import httpx

    from deploy import market_monitor

    client = AsyncMock()
    client.__aenter__.return_value = client
    client.get_klines.side_effect = httpx.ConnectError("exchange unavailable")
    monkeypatch.setattr(market_monitor, "BinanceRESTClient", lambda venue: client)
    monkeypatch.setattr("sys.argv", [
        "market_monitor", "--config-path", str(tmp_path / "missing"), "--data-dir", str(tmp_path),
        "--symbol", "ZECUSDT", "--dry-run",
    ])
    with pytest.raises(SystemExit) as error:
        market_monitor.main()
    assert error.value.code == 1


@pytest.mark.asyncio
async def test_target_drop_alert_waits_for_coverage_and_reports_net_bps(tmp_path):
    monitor = DailyMonitor(tmp_path / "alerts.sqlite", RiskSettings(), Band(100, 50, 3600, 10))
    send = AsyncMock()
    enough = {"long-target": Metric(25, 275, -15)}
    undercovered = {"long-target": Metric(25, 50, -15, 0.2)}
    baseline = {"long-target": Metric(40, 3900, 0)}
    monitor.evaluate("ZECUSDT-PERP", NOW.date(), undercovered, baseline)
    monitor.evaluate("ZECUSDT-PERP", NOW.date(), enough, {})
    await monitor.deliver(send)
    send.assert_not_called()
    monitor.evaluate("ZECUSDT-PERP", NOW.date(), enough, baseline)
    await monitor.deliver(send)
    subject, body = send.await_args.args
    assert "long-target anomaly" in subject
    assert "25.00 %" in body
    assert "Mean move after 10 bps round-trip fee: -15.00 vs 0.00 bps" in body
    monitor.close()


@pytest.mark.parametrize(
    ("config", "options", "error_text"),
    [
        ('{"trading":{"exit_floor":{"enabled":false}}}', [], "enabled target"),
        ('{}', ["--persistent-evaluations", "0"], "persistent_evaluations"),
        ('{}', ["--window-hours", "1"], "longer than the hold limit"),
        ('{}', ["--quote-timezone", "Invalid/Zone"], "Unknown quote timezone"),
    ],
)
def test_cli_reports_invalid_options(tmp_path, monkeypatch, capsys, config, options, error_text):
    from deploy import market_monitor

    config_path = tmp_path / "config.json"
    config_path.write_text(config)
    monkeypatch.setattr("sys.argv", [
        "market_monitor", "--config-path", str(config_path), "--dry-run", *options,
    ])
    with pytest.raises(SystemExit) as error:
        market_monitor.main()
    assert error.value.code == 2
    assert error_text in capsys.readouterr().err
