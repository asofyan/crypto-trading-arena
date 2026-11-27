"""The band audit: does the recorded price path actually pay the configured band?"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from scripts.band_audit import episodes, main, required_hit_rate, score_episode, summarise

T0 = datetime(2026, 11, 26, 12, 0, 0)


def _fill(minutes, sign, price, qty=1.0, product="SOLUSDT-PERP"):
    return {
        "t": T0 + timedelta(minutes=minutes),
        "agent": "scalper-perp",
        "product": product,
        "qty": qty,
        "price": price,
        "sign": sign,
        "fee": price * qty * 5 / 1e4,
    }


def test_required_hit_rate_is_the_break_even_probability():
    """EV=0 at p = (SL + C)/(TP + SL): the skill premium the band demands."""
    assert required_hit_rate(100.0, 50.0, 11.5) == pytest.approx(0.41, abs=1e-3)
    assert required_hit_rate(50.0, 25.0, 11.5) == pytest.approx(0.487, abs=1e-3)


def test_episodes_pair_an_open_and_a_close():
    out = episodes([_fill(0, +1, 100.0), _fill(5, -1, 101.0)])

    assert len(out) == 1
    assert out[0]["side"] == 1
    assert out[0]["entry"] == pytest.approx(100.0)
    assert out[0]["exit"] == pytest.approx(101.0)
    assert out[0]["held_s"] == pytest.approx(300)


def test_a_flipping_fill_closes_one_episode_and_opens_the_next():
    """Sell 3 against a long of 1: the long closes here, and a short of 2 opens."""
    out = episodes(
        [_fill(0, +1, 100.0), _fill(5, -1, 101.0, qty=3.0), _fill(9, +1, 99.0, qty=2.0)]
    )

    assert len(out) == 2
    assert out[0]["side"] == 1 and out[0]["exit"] == pytest.approx(101.0)
    assert out[1]["side"] == -1 and out[1]["entry"] == pytest.approx(101.0)
    assert out[1]["exit"] == pytest.approx(99.0)


def test_two_agents_on_the_same_product_stay_separate_episodes():
    """Keying on product alone invents positions that never existed."""
    out = episodes(
        [
            _fill(0, +1, 100.0),
            {**_fill(1, +1, 110.0), "agent": "other"},
            _fill(5, -1, 105.0),
            {**_fill(6, -1, 106.0), "agent": "other"},
        ]
    )

    assert len(out) == 2
    assert {(e["agent"], round(e["entry"], 1), round(e["exit"], 1)) for e in out} == {
        ("scalper-perp", 100.0, 105.0),
        ("other", 110.0, 106.0),
    }


def _quotes(prices, product="SOLUSDT-PERP"):
    """Quotes with a 2 bps spread around each price."""
    return [
        (T0 + timedelta(minutes=i), price * 0.9999, price * 1.0001)
        for i, price in enumerate(prices)
    ]


def _episode(side=1, entry=100.0):
    return {"product": "SOLUSDT-PERP", "side": side, "entry": entry, "t_open": T0, "exit": None}


def test_a_target_touch_is_scored_as_a_win():
    outcome = score_episode(_episode(), _quotes([100.0, 100.5, 101.2]), tp=100, sl=50, hold_seconds=3600)

    assert outcome.result == "tp"
    assert outcome.bps == pytest.approx(100.0)


def test_a_stop_touch_is_scored_as_a_loss():
    outcome = score_episode(_episode(), _quotes([100.0, 99.6, 99.4]), tp=100, sl=50, hold_seconds=3600)

    assert outcome.result == "sl"
    assert outcome.bps == pytest.approx(-50.0)


def test_neither_barrier_inside_the_hold_limit_exits_at_the_last_quote():
    outcome = score_episode(_episode(), _quotes([100.0, 100.2, 100.1]), tp=100, sl=50, hold_seconds=120)

    assert outcome.result == "hold"
    assert outcome.bps == pytest.approx(9.0, abs=0.5)  # +0.09% on the bid


def test_a_long_exits_into_the_bid_so_a_touch_on_the_ask_does_not_pay():
    """Scoring barriers on mid price is how a backtest invents profit."""
    quotes = [(T0, 100.0, 100.0), (T0 + timedelta(minutes=1), 100.85, 101.1)]
    outcome = score_episode(_episode(side=1, entry=100.0), quotes, tp=90, sl=50, hold_seconds=600)

    assert outcome.result == "hold", "the bid never reached +90 bps, only the ask did"


def test_a_quote_that_touches_both_barriers_is_scored_as_the_stop():
    """Between two quotes we cannot order the touches, and pessimism is the honest side."""
    quotes = [(T0, 100.0, 100.0), (T0 + timedelta(minutes=1), 99.0, 101.5)]
    outcome = score_episode(_episode(), quotes, tp=100, sl=50, hold_seconds=600)

    assert outcome.result == "sl"


def test_summary_reports_the_required_rate_next_to_the_observed_one():
    outcomes = [
        score_episode(_episode(), _quotes([100.0, 101.2]), tp=100, sl=50, hold_seconds=600),
        score_episode(_episode(), _quotes([100.0, 99.3]), tp=100, sl=50, hold_seconds=600),
        score_episode(_episode(), _quotes([100.0, 100.2]), tp=100, sl=50, hold_seconds=60),
    ]
    stats = summarise(outcomes, tp=100, sl=50, cost_bps=10.0)

    assert stats["n"] == 3
    assert stats["tp_pct"] == pytest.approx(33.3, abs=0.1)
    assert stats["sl_pct"] == pytest.approx(33.3, abs=0.1)
    assert stats["required_pct"] == pytest.approx(40.0, abs=0.1)


def test_main_explains_itself_when_no_session_was_recorded(tmp_path, capsys):
    assert main(["--data-dir", str(tmp_path)]) == 1
    assert "ticks_*.csv" in capsys.readouterr().out


def test_main_scores_a_recorded_session(tmp_path, capsys):
    (tmp_path / "ticks_20261126_120000.csv").write_text(
        "timestamp,product_id,price,best_bid,best_ask,spread_bps\n"
        f"{T0.isoformat()},SOLUSDT-PERP,100.0,99.99,100.01,2.0\n"
        f"{(T0 + timedelta(minutes=3)).isoformat()},SOLUSDT-PERP,101.0,100.99,101.01,2.0\n"
    )
    (tmp_path / "trades_20261126_120000.csv").write_text(
        "timestamp,agent_id,action,product_id,quantity,price,total_value,fee,cash_after,latency\n"
        f"{T0.isoformat()},a,buy,SOLUSDT-PERP,1,100.0,100,0.05,0,\n"
        f"{(T0 + timedelta(minutes=5)).isoformat()},a,sell,SOLUSDT-PERP,1,101.0,101,0.05,0,\n"
    )

    assert main(["--data-dir", str(tmp_path), "--tp-bps", "100", "--sl-bps", "50"]) == 0
    out = capsys.readouterr().out
    assert "required hit rate" in out
    assert "SOLUSDT-PERP" in out
