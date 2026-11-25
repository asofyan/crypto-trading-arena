"""Reconciliation and equity sync: the rules that decide whether real money moves.

Pure comparison logic only — no venue, no threads, no clock. The session that drives it
is tested in test_live_session.py.
"""

from __future__ import annotations

import pytest

from arena.instruments import parse_exchange_info
from arena.reconcile import EquitySync, compare

SOLUSDT_INFO = {
    "symbols": [
        {
            "symbol": "SOLUSDT",
            "status": "TRADING",
            "baseAsset": "SOL",
            "quoteAsset": "USDT",
            "filters": [
                {"filterType": "LOT_SIZE", "minQty": "0.001", "stepSize": "0.001"},
                {"filterType": "NOTIONAL", "minNotional": "5", "applyMinToMarket": True},
            ],
        },
        {
            "symbol": "XRPUSDT",
            "status": "TRADING",
            "baseAsset": "XRP",
            "quoteAsset": "USDT",
            "filters": [
                {"filterType": "LOT_SIZE", "minQty": "0.1", "stepSize": "0.1"},
                {"filterType": "NOTIONAL", "minNotional": "5", "applyMinToMarket": True},
            ],
        },
    ]
}

RULES = parse_exchange_info(SOLUSDT_INFO)
BASELINE = {"USDT": 0.0, "SOL": 0.0, "XRP": 0.0}


def _compare(**overrides):
    params = {
        "balances": {"USDT": 95.0, "SOL": 0.5},
        "baseline": dict(BASELINE),
        "ledger_positions": {"SOLUSDT": 0.5},
        "ledger_cash": 95.0,
        "rules": RULES,
        "reserve_usdt": 0.0,
        "open_orders": 0,
    }
    params.update(overrides)
    return compare(**params)


# ── Comparison ─────────────────────────────────────────────────


def test_a_matching_wallet_reconciles_and_arms():
    report = _compare()

    assert report.armed
    assert report.blocking == ()
    assert report.notes == ()
    assert "reconciled" in report.summary()


def test_the_baseline_excludes_holdings_the_arena_never_bought():
    # 2 SOL were in the wallet before go-live; the arena owns 0.5 of them.
    report = _compare(
        balances={"USDT": 95.0, "SOL": 2.5},
        baseline={"USDT": 0.0, "SOL": 2.0},
    )

    assert report.armed
    assert report.notes == ()


def test_a_shortfall_blocks_with_the_missing_amount():
    report = _compare(balances={"USDT": 95.0, "SOL": 0.2})

    assert not report.armed
    drift = report.blocking[0]
    assert drift.kind == "shortfall"
    assert drift.subject == "SOLUSDT"
    assert "short 0.3" in drift.detail
    assert "0.5" in report.first_blocker()


def test_a_surplus_in_a_base_asset_is_reported_but_not_adopted():
    report = _compare(balances={"USDT": 95.0, "SOL": 0.9})

    assert report.armed  # a surplus cannot lose money on its own
    assert report.notes and report.notes[0].kind == "surplus"
    assert report.quote_surplus == 0.0  # never treated as a deposit


def test_a_missing_baseline_blocks_every_order():
    report = _compare(baseline={})

    assert not report.armed
    assert report.blocking[0].kind == "missing_baseline"
    assert "live-init" in report.blocking[0].detail


def test_one_step_of_venue_truncation_is_not_drift():
    # The venue floors to 0.001; a position that reads 0.0005 lower is not a shortfall.
    report = _compare(balances={"USDT": 95.0, "SOL": 0.4995})

    assert report.armed


def test_a_quote_shortfall_blocks_because_the_ledger_promises_money():
    report = _compare(balances={"USDT": 80.0, "SOL": 0.5})

    assert not report.armed
    drift = report.blocking[0]
    assert drift.subject == "USDT"
    assert "short 15.00" in drift.detail


def test_the_reserve_is_not_spendable():
    report = _compare(balances={"USDT": 95.0 + 5.0, "SOL": 0.5}, reserve_usdt=5.0)

    assert report.armed  # 100 − 5 reserve = 95 spendable


def test_a_quote_surplus_becomes_a_deposit_candidate():
    report = _compare(balances={"USDT": 145.0, "SOL": 0.5})

    assert report.armed
    assert report.quote_surplus == pytest.approx(50.0)


def test_open_orders_block_because_this_bot_places_none():
    report = _compare(open_orders=1)

    assert not report.armed
    assert report.blocking[0].kind == "unaligned_open_orders"
    assert "cancel" in report.blocking[0].detail


def test_a_symbol_without_rules_is_called_out_rather_than_ignored():
    report = _compare(ledger_positions={"SOLUSDT": 0.5, "DOGEUSDT": 100.0})

    assert any(note.kind == "uncovered" for note in report.notes)
    assert "DOGEUSDT" in report.summary()


# ── Equity sync ────────────────────────────────────────────────


def _surplus_report(amount: float):
    return _compare(balances={"USDT": 95.0 + amount, "SOL": 0.5})


def test_one_sighting_is_not_enough_to_credit_money():
    sync = EquitySync(tolerance_usdt=1.0, mapped_agents=("a1",))

    assert sync.observe(_surplus_report(50.0), {"a1": 95.0}) is None


def test_two_matching_sightings_credit_the_sole_agent():
    sync = EquitySync(tolerance_usdt=1.0, mapped_agents=("a1",))
    sync.observe(_surplus_report(50.0), {"a1": 95.0})

    adoption = sync.observe(_surplus_report(50.0), {"a1": 95.0})

    assert adoption is not None
    assert adoption.amount == pytest.approx(50.0)
    assert adoption.allocations == {"a1": pytest.approx(50.0)}


def test_a_moving_surplus_keeps_waiting():
    sync = EquitySync(tolerance_usdt=1.0, mapped_agents=("a1",))
    sync.observe(_surplus_report(50.0), {"a1": 95.0})

    assert sync.observe(_surplus_report(80.0), {"a1": 95.0}) is None
    assert sync.observe(_surplus_report(80.0), {"a1": 95.0}) is not None


def test_dust_below_the_tolerance_is_ignored():
    sync = EquitySync(tolerance_usdt=1.0, mapped_agents=("a1",))

    assert sync.observe(_surplus_report(0.4), {"a1": 95.0}) is None
    assert sync.observe(_surplus_report(0.4), {"a1": 95.0}) is None


def test_ambiguity_between_agents_leaves_the_money_unallocated():
    sync = EquitySync(policy="credit_sole_agent", mapped_agents=("a1", "a2"))
    sync.observe(_surplus_report(50.0), {"a1": 50.0, "a2": 45.0})

    adoption = sync.observe(_surplus_report(50.0), {"a1": 50.0, "a2": 45.0})

    assert adoption is not None
    assert adoption.allocations == {}
    assert "unallocated" in adoption.note


def test_the_proportional_policy_splits_by_current_equity():
    sync = EquitySync(policy="proportional", mapped_agents=("a1", "a2"))
    equity = {"a1": 75.0, "a2": 25.0}
    sync.observe(_surplus_report(40.0), equity)

    adoption = sync.observe(_surplus_report(40.0), equity)

    assert adoption is not None
    assert adoption.allocations["a1"] == pytest.approx(30.0)
    assert adoption.allocations["a2"] == pytest.approx(10.0)


def test_the_unallocated_policy_never_credits_an_agent():
    sync = EquitySync(policy="unallocated", mapped_agents=("a1",))
    sync.observe(_surplus_report(50.0), {"a1": 95.0})

    adoption = sync.observe(_surplus_report(50.0), {"a1": 95.0})

    assert adoption is not None and adoption.allocations == {}


def test_an_unknown_policy_is_refused():
    with pytest.raises(ValueError, match="unknown deposit policy"):
        EquitySync(policy="spend-it-all")


def test_a_shortfall_is_never_an_adoption():
    sync = EquitySync(mapped_agents=("a1",))
    report = _compare(balances={"USDT": 20.0, "SOL": 0.5})

    assert sync.observe(report, {"a1": 95.0}) is None
    assert sync.observe(report, {"a1": 95.0}) is None
