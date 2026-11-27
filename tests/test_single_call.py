from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from arena.models import AgentAccount
from arena.single_call import SingleCallTick, TradeDecision


@pytest.mark.asyncio
async def test_tick_reads_local_book_and_dispatches_once():
    request = AsyncMock(return_value='{"action":"close_position","product_id":"ZECUSDT-PERP"}')
    publish = AsyncMock()
    ledger = SimpleNamespace(
        read_portfolio=lambda: ({"demo": AgentAccount(cash=100, trade_count=7)}, [], False)
    )
    tick = SingleCallTick("demo", ledger, request, publish)

    await tick.run("market tick")

    request.assert_awaited_once()
    assert "market tick" in request.call_args.args[0]
    assert "100" in request.call_args.args[0]
    publish.assert_awaited_once_with(
        TradeDecision(action="close_position", product_id="ZECUSDT-PERP"), 7
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("output", ['{"action":"hold"}', "invalid", "[]"])
async def test_hold_and_invalid_output_do_not_dispatch(output):
    request = AsyncMock(return_value=output)
    publish = AsyncMock()
    ledger = SimpleNamespace(
        read_portfolio=lambda: ({"demo": AgentAccount(cash=100)}, [], False)
    )
    await SingleCallTick("demo", ledger, request, publish).run("tick")
    request.assert_awaited_once()
    publish.assert_not_awaited()


async def test_degraded_ledger_skips_model():
    request = AsyncMock()
    publish = AsyncMock()
    ledger = SimpleNamespace(read_portfolio=lambda: ({}, [], True))

    await SingleCallTick("demo", ledger, request, publish).run("tick")

    request.assert_not_awaited()
    publish.assert_not_awaited()


async def test_tool_return_cannot_start_a_tick():
    from deploy.agent import single_call_handler

    tick = SimpleNamespace(run=AsyncMock())
    await single_call_handler(tick)(SimpleNamespace(delivery_kind="return"))

    tick.run.assert_not_awaited()


def test_snapshot_decision_rejects_fill_between_read_and_order():
    from arena.account_store import AccountStore
    from arena.price_book import PriceBook
    from arena.single_call import SnapshotDecision

    store = AccountStore(PriceBook())
    store.get_or_create("demo").trade_count = 2
    result = store.execute_snapshot_decision(SnapshotDecision(
        agent_id="demo",
        trade_count=1,
        decision=TradeDecision(action="sell", product_id="ZECUSDT-PERP", quantity=1),
    ))

    assert not result.success
    assert "Portfolio changed" in result.message


@pytest.mark.parametrize("output", [
    '{"action":"hold","product_id":"ZECUSDT-PERP"}',
    '{"action":"buy","product_id":"ZECUSDT-PERP"}',
])
async def test_decision_requires_fields_matching_its_action(output):
    request = AsyncMock(return_value=output)
    publish = AsyncMock()
    ledger = SimpleNamespace(read_portfolio=lambda: ({}, [], False))

    assert await SingleCallTick("demo", ledger, request, publish).run("tick") == "Invalid decision."
    publish.assert_not_awaited()


async def test_live_tick_pauses_during_halt_and_resumes_on_next_batch():
    request = AsyncMock(return_value='{"action":"hold"}')
    publish = AsyncMock()
    ledger = SimpleNamespace(read_portfolio=lambda: ({}, [], False))
    armed = False
    tick = SingleCallTick("demo", ledger, request, publish, can_request=lambda: armed)

    await tick.run("halted batch")
    request.assert_not_awaited()
    armed = True
    await tick.run("armed batch")
    request.assert_awaited_once()
    assert "armed batch" in request.call_args.args[0]
    armed = False
    await tick.run("halted again")
    request.assert_awaited_once()
    publish.assert_not_awaited()
