"""Tests for the hold watchdog: who is stale, and who pulls the escape hatch.

The exit floor in ``arena.trade_policy`` *permits* a close once a position is past its
hold limit. Nothing *compels* one, and in the measured run that gap was the bug: with
the floor armed and refusing every in-band close, four round trips sat 800 s to 3100 s
past the 3600 s limit because the agent — an LLM on a tick — simply never asked again.
The escape hatch existed and nobody pulled it.

So the watchdog is the thing that pulls. Its decisions live in a pure function so the
selection rules can be pinned down without a clock, a store or a broker: the tests here
are about *which* positions are due, and the integration tests below are about whether
booking the close actually flattens them.
"""

from __future__ import annotations

import asyncio
import math
import time

import pytest

from arena.account_store import AccountStore
from arena.fees import FeeModel
from arena.hold_watchdog import HoldWatchdog, StalePosition, stale_positions
from arena.models import INITIAL_CASH, AgentAccount
from arena.price_book import PriceBook
from arena.trade_policy import ExitFloor

NOW = 1_800_000_000.0
LIMIT = 3600.0
PERP = "SOLUSDT-PERP"


def make_account(**overrides) -> AgentAccount:
    """An account holding one perp lot, opened ``held`` seconds before ``NOW``."""
    held = overrides.pop("held", LIMIT + 600.0)
    quantity = overrides.pop("quantity", 0.5)
    entry_ts = overrides.pop("entry_ts", NOW - held)
    account = AgentAccount(**overrides)
    account.positions[PERP] = quantity
    account.cost_basis[PERP] = quantity * 100.0
    if entry_ts is not None:
        account.avg_entry_ts[PERP] = entry_ts
    return account


def due(accounts: dict[str, AgentAccount], **kwargs) -> list:
    kwargs.setdefault("hold_limit_s", LIMIT)
    kwargs.setdefault("now", NOW)
    return stale_positions(accounts, **kwargs)


# ── Which positions are due ──────────────────────────────────────


def test_position_past_the_hold_limit_is_due_to_close():
    """The whole point: a long that has sat past the limit is closed with a sell."""
    [intent] = due({"a": make_account(held=LIMIT + 600.0)})

    assert intent.agent_id == "a"
    assert intent.product_id == PERP
    assert intent.action == "sell"
    assert intent.quantity == pytest.approx(0.5)
    assert intent.held_seconds == pytest.approx(LIMIT + 600.0)


def test_short_is_closed_by_buying():
    """Closing a short is a buy; getting the side backwards would double the position."""
    [intent] = due({"a": make_account(quantity=-0.5)})

    assert intent.action == "buy"
    assert intent.quantity == pytest.approx(0.5)


def test_position_inside_the_limit_is_left_alone():
    """One second short of the limit is still the agent's position to hold."""
    assert due({"a": make_account(held=LIMIT - 1.0)}) == []


def test_limit_is_exact_not_fuzzy():
    """At the limit the close is due, because that is the moment the floor allows it.

    The watchdog reads the clock before ``execute_trade`` reads it again, and time only
    moves forward, so a position due here cannot be refused there as "one tick early".
    """
    [intent] = due({"a": make_account(held=LIMIT)})

    assert intent.held_seconds == pytest.approx(LIMIT)


def test_flat_position_is_never_due():
    """A zero position has nothing to close, whatever its bookkeeping says."""
    assert due({"a": make_account(quantity=0.0)}) == []


def test_dust_within_epsilon_counts_as_flat():
    """A float residue is not a position; closing it would book a fill for nothing."""
    assert due({"a": make_account(quantity=1e-12)}) == []


def test_position_without_an_entry_time_is_not_judged():
    """Hydrated across a restart, a position has no *when*, and guessing is a trap.

    Refusing to close would be worse than waiting: the floor fails open on the same
    input, so the agent keeps its discretion instead of being locked out.
    """
    assert due({"a": make_account(entry_ts=None)}) == []


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
def test_non_finite_entry_time_is_not_judged(bad):
    assert due({"a": make_account(entry_ts=bad)}) == []


def test_non_finite_limit_means_nothing_is_due():
    """A misconfigured limit must disable the watchdog, never close every position."""
    assert due({"a": make_account()}, hold_limit_s=math.nan) == []


def test_no_limit_means_nothing_is_due():
    """``None`` is the operator turning the timeout off, watchdog included."""
    assert due({"a": make_account()}, hold_limit_s=None) == []


def test_every_stale_position_is_reported_once():
    """One tick must see all of them: two products on one agent, two agents."""
    accounts = {
        "a": make_account(),
        "b": make_account(quantity=-1.5),
    }
    accounts["a"].positions["XRPUSDT-PERP"] = 10.0
    accounts["a"].cost_basis["XRPUSDT-PERP"] = 20.0
    accounts["a"].avg_entry_ts["XRPUSDT-PERP"] = NOW - LIMIT - 60.0

    intents = due(accounts)

    assert len(intents) == 3
    # Deterministic order, so a tick's log lines are diffable between runs.
    assert intents == sorted(intents, key=lambda i: (i.agent_id, i.product_id))


def test_mixed_book_only_closes_the_stale_half():
    """A fresh position next to a stale one must not be dragged into the close."""
    accounts = {"a": make_account(held=LIMIT + 600.0)}
    accounts["a"].positions["XRPUSDT-PERP"] = 20.0
    accounts["a"].cost_basis["XRPUSDT-PERP"] = 40.0
    accounts["a"].avg_entry_ts["XRPUSDT-PERP"] = NOW - 30.0

    intents = due(accounts)

    assert [i.product_id for i in intents] == [PERP]


# ── Pulling the hatch: the store and the watchdog together ───────
#
# These use a real AccountStore with the real exit floor: the point is not that the
# watchdog can compute a list, it is that the list becomes a booked fill on a position
# the floor would otherwise hold shut until the agent happened to ask again.


MARK = 100.0


def make_armed_store() -> AccountStore:
    """A store with the 50/25/3600 floor armed and a flat perp quote at 100."""
    book = PriceBook()
    book.update(
        {
            "product_id": PERP,
            "price": str(MARK),
            "best_bid": str(MARK),
            "best_ask": str(MARK),
        }
    )
    store = AccountStore(book, FeeModel(taker_bps=10, perp_taker_bps=5))
    store.set_exit_floor(
        ExitFloor(min_profit_bps=50.0, stop_bps=25.0, max_hold_seconds=LIMIT)
    )
    return store


def open_position(store: AccountStore, *, quantity: float = 0.5, held: float = 0.0) -> str:
    """Book a real position, then age it: ``held`` seconds of holding, without waiting."""
    agent = "a"
    action = "buy" if quantity > 0 else "sell"
    result = store.execute_trade(agent, PERP, abs(quantity), action)
    assert result.success, result.message
    if held:
        account = store.get_or_create(agent)
        account.avg_entry_ts[PERP] = time.time() - held
    return agent


def position_of(store: AccountStore, agent: str) -> float:
    return store.get_or_create(agent).positions.get(PERP, 0.0)


def test_the_floor_already_permits_a_stale_close_so_only_the_submission_was_missing():
    """The measured bug, pinned at the seam where it actually lived.

    Before the limit the same sell is refused — correctly, the position is inside the
    band and has not earned its round trip. Past the limit the floor answers yes. So the
    guard was never what held these positions open: what was missing is an actor that
    submits the order at the moment the answer flips, and that is the watchdog.
    """
    store = make_armed_store()
    agent = open_position(store, held=LIMIT - 60.0)

    refused = store.execute_trade(agent, PERP, 0.5, "sell")
    assert refused.success is False
    assert "exit floor" in refused.message.lower()
    assert position_of(store, agent) == pytest.approx(0.5)

    store.get_or_create(agent).avg_entry_ts[PERP] = time.time() - LIMIT - 1.0
    allowed = store.execute_trade(agent, PERP, 0.5, "sell")

    assert allowed.success is True
    assert position_of(store, agent) == pytest.approx(0.0)


async def test_watchdog_closes_a_position_past_the_hold_limit():
    """Past the limit the same close goes out, because the watchdog asks at the right
    moment: the floor permits it and something finally submits it."""
    store = make_armed_store()
    agent = open_position(store, held=LIMIT + 600.0)

    results = await HoldWatchdog(store, hold_limit_s=LIMIT).tick()

    assert [r.success for r in results] == [True]
    assert position_of(store, agent) == pytest.approx(0.0)
    assert store.get_or_create(agent).trade_count == 2


async def test_watchdog_closes_a_stale_short_by_buying_it_back():
    """The side matters more than the sign: closing a short is a buy, and a sell here
    would double the exposure the watchdog was supposed to end."""
    store = make_armed_store()
    agent = open_position(store, quantity=-0.5, held=LIMIT + 600.0)

    await HoldWatchdog(store, hold_limit_s=LIMIT).tick()

    assert position_of(store, agent) == pytest.approx(0.0)


async def test_watchdog_leaves_a_position_inside_the_limit_alone():
    """The watchdog is not a stop-loss: inside the limit the agent still owns the trade."""
    store = make_armed_store()
    agent = open_position(store, held=LIMIT - 60.0)

    assert await HoldWatchdog(store, hold_limit_s=LIMIT).tick() == []
    assert position_of(store, agent) == pytest.approx(0.5)


async def test_watchdog_survives_a_close_it_cannot_make():
    """A stale position with no quote is refused by the executor, not by a crash.

    A watchdog that raises on the first un-closable position stops watching every
    other one, which is how a fix becomes a new trap.
    """
    store = make_armed_store()
    account = store.get_or_create("a")
    account.positions["LINKUSDT-PERP"] = 3.0
    account.cost_basis["LINKUSDT-PERP"] = 30.0
    account.avg_entry_ts["LINKUSDT-PERP"] = time.time() - LIMIT - 600.0

    [result] = await HoldWatchdog(store, hold_limit_s=LIMIT).tick()

    assert result.success is False
    assert position_of(store, "a").__abs__() == pytest.approx(0.0)
    assert account.positions["LINKUSDT-PERP"] == pytest.approx(3.0)


async def test_the_loop_closes_without_anyone_calling_tick():
    """The shipped path is the loop; a tick() nobody schedules fixes nothing."""
    store = make_armed_store()
    agent = open_position(store, held=LIMIT + 600.0)
    watchdog = HoldWatchdog(store, hold_limit_s=LIMIT, interval=0.01)
    watchdog.start()
    try:
        for _ in range(50):
            await asyncio.sleep(0.01)
            if position_of(store, agent) == 0.0:
                break
    finally:
        await watchdog.close()

    assert position_of(store, agent) == pytest.approx(0.0)


async def test_closing_the_watchdog_stops_it_watching():
    """Shutdown must actually stop the loop, or a restart would trade behind the
    operator's back while the process it belonged to is on its way out."""
    store = make_armed_store()
    watchdog = HoldWatchdog(store, hold_limit_s=LIMIT, interval=0.01)
    watchdog.start()
    await watchdog.close()

    agent = open_position(store, held=LIMIT + 600.0)
    await asyncio.sleep(0.1)

    assert position_of(store, agent) == pytest.approx(0.5)


async def test_tick_with_no_limit_closes_nothing():
    """With the timeout off the operator asked for no escape hatch, watchdog included."""
    store = make_armed_store()
    agent = open_position(store, held=LIMIT + 600.0)

    assert await HoldWatchdog(store, hold_limit_s=None).tick() == []
    assert position_of(store, agent) == pytest.approx(0.5)


def test_initial_cash_is_the_only_thing_the_close_touches():
    """A close is a fill, not a withdrawal: proceeds land in cash minus the exit fee."""
    store = make_armed_store()
    agent = open_position(store, held=LIMIT + 600.0)
    cash_after_open = store.get_or_create(agent).cash

    [result] = asyncio.run(HoldWatchdog(store, hold_limit_s=LIMIT).tick())

    assert result.success is True
    expected = cash_after_open + MARK * 0.5 - (MARK * 0.5 * 5 / 10_000.0)
    assert store.get_or_create(agent).cash == pytest.approx(expected)
    assert store.get_or_create(agent).cash < INITIAL_CASH + 1.0


# ── The intent must not become a trade in its own right ──────────


async def test_a_stale_intent_for_an_already_flat_book_opens_nothing(monkeypatch):
    """The blocker: between deciding and closing, another thread may have closed it.

    The watchdog used to submit the quantity it read, so a book that went flat in the
    meantime made "sell 0.5" an *opening* short of 0.5 — the escape hatch creating the
    exposure it was meant to end, with a fresh hold clock and no band to leave by.
    """
    store = make_armed_store()
    agent = open_position(store, quantity=0.5, held=LIMIT + 600.0)
    stale = StalePosition(agent, PERP, 0.5, "sell", LIMIT + 600.0)
    # The agent's own close lands first; past the limit the floor allows it.
    assert store.execute_trade(agent, PERP, 0.5, "sell").success
    monkeypatch.setattr(store, "stale_positions", lambda *a, **k: [stale])

    [result] = await HoldWatchdog(store, hold_limit_s=LIMIT).tick()

    assert result.success is False
    assert "flat" in result.message.lower()
    assert position_of(store, agent) == pytest.approx(0.0)


async def test_a_stale_intent_closes_only_the_remainder(monkeypatch):
    """If the agent trimmed instead of closing, the watchdog ends what is left."""
    store = make_armed_store()
    agent = open_position(store, quantity=0.9, held=LIMIT + 600.0)
    stale = StalePosition(agent, PERP, 0.9, "sell", LIMIT + 600.0)
    monkeypatch.setattr(store, "stale_positions", lambda *a, **k: [stale])
    assert store.execute_trade(agent, PERP, 0.4, "sell").success

    [result] = await HoldWatchdog(store, hold_limit_s=LIMIT).tick()

    assert result.success, result.message
    assert position_of(store, agent) == pytest.approx(0.0)


async def test_one_unclosable_position_does_not_stop_the_others(monkeypatch):
    """A fill that raises must cost one close, not the whole tick.

    The claim in the loop's own docstring ("a watchdog that dies stops watching") was
    untested: the existing test only exercised a refusal the executor *returned*.
    """
    store = make_armed_store()
    good = open_position(store, held=LIMIT + 600.0)
    account = store.get_or_create(good)
    account.positions["XRPUSDT-PERP"] = 1.0
    account.avg_entry_ts["XRPUSDT-PERP"] = time.time() - LIMIT - 60.0
    monkeypatch.setattr(
        store,
        "stale_positions",
        lambda *a, **k: [
            StalePosition(good, "XRPUSDT-PERP", 1.0, "sell", LIMIT + 600.0),
            StalePosition(good, PERP, 0.5, "sell", LIMIT + 600.0),
        ],
    )
    real_close = store.close_position

    def explode(agent_id, product_id, **kwargs):
        if product_id == "XRPUSDT-PERP":
            raise RuntimeError("book exploded")
        return real_close(agent_id, product_id, **kwargs)

    monkeypatch.setattr(store, "close_position", explode)

    results = await HoldWatchdog(store, hold_limit_s=LIMIT).tick()

    assert [r.success for r in results] == [True]
    assert position_of(store, good) == pytest.approx(0.0)


async def test_the_close_runs_off_the_event_loop():
    """A live venue call inside the loop would stall every consumer of the tick.

    ``execute_trade`` blocks on the venue in live mode, so the watchdog hands the close
    to a worker thread; the test pins the thread hop rather than trusting it.
    """
    import threading

    store = make_armed_store()
    agent = open_position(store, held=LIMIT + 600.0)
    seen: list[str] = []
    real_close = store.close_position

    def record(agent_id, product_id, **kwargs):
        seen.append(threading.current_thread().name)
        return real_close(agent_id, product_id, **kwargs)

    store.close_position = record

    await HoldWatchdog(store, hold_limit_s=LIMIT).tick()

    assert seen and seen[0] != threading.current_thread().name
    assert position_of(store, agent) == pytest.approx(0.0)


# ── The loop itself must survive what it is watching ─────────────


async def test_the_scan_runs_off_the_event_loop_too(monkeypatch):
    """A live close holds the store lock in a worker thread; scanning on the loop stalls.

    ``stale_positions`` takes the same lock, so a synchronous scan in ``tick`` would
    block the event loop for the duration of a venue round trip — the very stall the
    thread hop in the close exists to avoid.
    """
    import threading

    store = make_armed_store()
    open_position(store, held=LIMIT + 600.0)
    seen: list[str] = []
    real = store.stale_positions

    def record(*args, **kwargs):
        seen.append(threading.current_thread().name)
        return real(*args, **kwargs)

    monkeypatch.setattr(store, "stale_positions", record)

    await HoldWatchdog(store, hold_limit_s=LIMIT).tick()

    assert seen and seen[0] != threading.current_thread().name


async def test_the_loop_survives_a_scan_that_raises(monkeypatch):
    """One bad scan must not kill the task: the death would only surface at shutdown."""
    store = make_armed_store()
    agent = open_position(store, held=LIMIT + 600.0)
    real = store.stale_positions
    calls = {"n": 0}

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("book half-written")
        return real(*args, **kwargs)

    monkeypatch.setattr(store, "stale_positions", flaky)
    watchdog = HoldWatchdog(store, hold_limit_s=LIMIT, interval=0.01)
    watchdog.start()
    try:
        for _ in range(100):
            await asyncio.sleep(0.01)
            if position_of(store, agent) == 0.0:
                break
    finally:
        await watchdog.close()

    assert calls["n"] > 1  # the loop kept ticking after the raise
    assert position_of(store, agent) == pytest.approx(0.0)


async def test_closing_an_unstarted_watchdog_is_a_no_op():
    """Shutdown runs unconditionally in deploy; it must not care whether it started."""
    watchdog = HoldWatchdog(make_armed_store(), hold_limit_s=LIMIT)

    await watchdog.close()
    await watchdog.close()
