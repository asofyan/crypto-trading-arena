"""Unit tests for arena.tools — calculator, hold-time formatting, portfolio render.

calculator is an @agent_tool (a ToolNodeDef); its underlying callable is reached via
`calculator._tool.function`, which only touches ctx.agent_name / ctx.tool_call_id for
logging, so a lightweight stub context is sufficient (no LLM, no broker).
"""

from __future__ import annotations

import time
import types
from datetime import datetime, timezone

import pytest

import arena.tools as tools
from arena.models import INITIAL_CASH
from arena.tools import _format_hold_time, _get_portfolio, calculator

_STUB_CTX = types.SimpleNamespace(agent_name="tester", tool_call_id="tc-1")


def _calc(expression: str) -> str:
    return calculator._tool.function(_STUB_CTX, expression)


# ── calculator: valid math ───────────────────────────────────────


@pytest.mark.parametrize(
    "expression,expected",
    [
        ("2 + 3 * 4", "14"),  # PEMDAS
        ("2 ** 10", "1024"),
        ("max(10, 20)", "20"),
        ("min(10, 20)", "10"),
        ("sqrt(16)", "4"),
        ("abs(-5)", "5"),
        ("floor(3.7)", "3"),
    ],
)
def test_calculator_evaluates_valid_expressions(expression, expected):
    assert _calc(expression) == expected


def test_calculator_ceil_is_not_recognized():
    """The docstring advertises ceil(), but sympy's function is `ceiling` — `ceil`
    is silently treated as an undefined symbolic function (finding B3 / doc gap)."""
    assert _calc("ceil(3.2)") == "ceil(3.2)"  # not evaluated to 4
    assert _calc("ceiling(3.2)") == "4"  # the name sympy actually understands


def test_calculator_float_result():
    assert "5000" in _calc("50000 * 0.1")


def test_calculator_invalid_expression_returns_message():
    assert _calc("1 +").startswith("Invalid expression")
    assert _calc("import os").startswith("Invalid expression")
    assert _calc("").startswith("Invalid expression")


# ── calculator: documented sharp edges (characterization, see B3) ─


def test_calculator_division_by_zero_returns_zoo():
    """sympy yields complex-infinity 'zoo' rather than an error — documented quirk."""
    assert _calc("1/0") == "zoo"


def test_calculator_accepts_undefined_symbols_silently():
    """`x + 1` is not rejected; sympy treats x as a free symbol."""
    assert "x" in _calc("x + 1")


@pytest.mark.xfail(
    reason="B3: container/non-scalar input raises AttributeError instead of an error message",
    raises=AttributeError,
    strict=True,
)
def test_calculator_container_input_is_handled():
    # Desired: a friendly 'Invalid expression' message, not an unhandled crash.
    assert _calc("[1, 2, 3]").startswith("Invalid expression")


# ── _format_hold_time ────────────────────────────────────────────


@pytest.fixture
def frozen_clock(monkeypatch):
    """Freeze arena.tools' clock at epoch 1_000_000 for deterministic hold times."""

    class _Clock:
        @classmethod
        def now(cls):
            return datetime.fromtimestamp(1_000_000, tz=timezone.utc)

    monkeypatch.setattr(tools, "datetime", _Clock)
    return 1_000_000


def test_format_hold_time_none_is_na():
    assert _format_hold_time(None) == "N/A"


@pytest.mark.parametrize(
    "ago_seconds,expected",
    [
        (30, "30s"),
        (600, "10m"),  # 600s = 10 min
        (7_200, "2.0h"),  # 7200s = 2 h
        (172_800, "2.0d"),  # 172800s = 2 d
    ],
)
def test_format_hold_time_buckets(frozen_clock, ago_seconds, expected):
    assert _format_hold_time(frozen_clock - ago_seconds) == expected


# ── _get_portfolio render ────────────────────────────────────────


@pytest.fixture
def seeded_singleton():
    """Seed the arena.tools module singletons used by _get_portfolio, then reset."""
    tools.price_book.update(
        {"product_id": "BTC-USD", "price": "50000", "best_bid": "49990", "best_ask": "50010"}
    )
    tools.price_book.update(
        {"product_id": "SOL-USD", "price": "100.00", "best_bid": "99.90", "best_ask": "100.10"}
    )
    tools.store._accounts.clear()
    tools.store._trade_log.clear()
    # The read stamps outlive an account (they are keyed by agent id), so a leftover stamp
    # would silently disable the stale-read gate for every later test in this file.
    tools.store._portfolio_read_seq.clear()
    yield tools.store
    tools.store._accounts.clear()
    tools.store._trade_log.clear()
    tools.store._portfolio_read_seq.clear()


def test_get_portfolio_empty(seeded_singleton):
    out = _get_portfolio("agent")
    assert f"Cash: ${INITIAL_CASH:,.2f}" in out
    assert "Positions: none" in out
    assert f"Total portfolio value: ${INITIAL_CASH:,.2f}" in out


def test_get_portfolio_with_priced_position(seeded_singleton):
    acct = seeded_singleton.get_or_create("agent")
    acct.positions["SOL-USD"] = 0.5
    acct.cost_basis["SOL-USD"] = 48.0

    out = _get_portfolio("agent")
    assert "SOL-USD" in out
    assert "$100.00" in out  # current price column (last price)
    # Total value = cash (100) + 0.5 * 100 mark.
    assert "Total portfolio value: $150.00" in out


def test_get_portfolio_position_without_price_shows_na(seeded_singleton):
    acct = seeded_singleton.get_or_create("agent")
    acct.positions["DOGE-USD"] = 100.0  # no quote in the price book
    acct.cost_basis["DOGE-USD"] = 50.0

    out = _get_portfolio("agent")
    assert "DOGE-USD" in out
    assert "N/A" in out  # current price / mkt value / pnl rendered as N/A


# ── execute_trade: latency stopwatch via ctx.deps ────────────────
#
# The connector stamps deps={"invoked_at": <time>} on the agent invocation; the
# tool turns it into a per-fill latency. calfkit 0.12 exposes ToolContext.deps as
# the producer-supplied Mapping directly (`ctx.deps["k"]`), replacing 0.2's
# `ctx.deps.provided_deps`.


def _exec_ctx(deps: dict) -> types.SimpleNamespace:
    return types.SimpleNamespace(agent_name="agent", tool_call_id="tc-1", deps=deps)


def test_execute_trade_records_latency_from_invoked_at(seeded_singleton, monkeypatch):
    monkeypatch.setattr(tools.view, "rerender", lambda: None)
    ctx = _exec_ctx({"invoked_at": time.time() - 0.5})

    msg = tools.execute_trade._tool.function(ctx, "SOL-USD", 0.5, "buy")

    assert msg.startswith("Bought")
    latency = seeded_singleton.trade_log[-1].latency
    assert latency is not None and latency >= 0.5


def test_execute_trade_latency_none_when_dep_absent(seeded_singleton, monkeypatch):
    """No invoked_at dep (a manual/test invocation) → latency is None, not a crash."""
    monkeypatch.setattr(tools.view, "rerender", lambda: None)
    ctx = _exec_ctx({})

    tools.execute_trade._tool.function(ctx, "SOL-USD", 0.5, "buy")

    assert seeded_singleton.trade_log[-1].latency is None


# ── the agent may never size an exit ────────────────────────────
#
# 2026-11-27 09:27: a model that had read SNDK 0.62 before a fill dropped it to 0.32
# asked to sell 0.62 — a request that would have opened a 0.61 short if the churn guard
# had not refused it first. The number was stale by one fill, and a quantity is the one
# thing that cannot survive being stale on an exit. Exits now name the product only.

PERP = "SOLUSDT-PERP"


def _mark_perp(price: float = 50.0) -> None:
    tools.price_book.update(
        {
            "product_id": PERP,
            "price": str(price),
            "best_bid": str(price),
            "best_ask": str(price),
        }
    )


def _position(store, agent_id: str = "agent") -> float:
    return store.get_or_create(agent_id).positions.get(PERP, 0.0)


def test_close_position_tool_flattens_the_live_size(seeded_singleton, monkeypatch):
    """The model names the product; the size is read when the fill is booked."""
    monkeypatch.setattr(tools.view, "rerender", lambda: None)
    _mark_perp()
    ctx = _exec_ctx({})
    tools.execute_trade._tool.function(ctx, PERP, 1.0, "buy")

    message = tools.close_position._tool.function(ctx, PERP)

    assert _position(seeded_singleton) == pytest.approx(0.0)
    assert "closed" in message.lower()
    assert seeded_singleton.trade_log[-1].quantity == pytest.approx(1.0)


def test_close_position_tool_refuses_when_flat(seeded_singleton, monkeypatch):
    """Nothing to close is a refusal, not an order that opens the other side."""
    monkeypatch.setattr(tools.view, "rerender", lambda: None)
    _mark_perp()
    before = len(seeded_singleton.trade_log)

    message = tools.close_position._tool.function(_exec_ctx({}), PERP)

    assert "Nothing to close" in message
    assert len(seeded_singleton.trade_log) == before


def test_execute_trade_refuses_an_agent_order_that_would_flip(seeded_singleton, monkeypatch):
    """Crossing zero is a new position, and the agent must ask for it deliberately."""
    monkeypatch.setattr(tools.view, "rerender", lambda: None)
    _mark_perp()
    ctx = _exec_ctx({})
    tools.execute_trade._tool.function(ctx, PERP, 1.0, "buy")
    tools.get_portfolio._tool.function(ctx)  # the size must come from a book it has seen

    message = tools.execute_trade._tool.function(ctx, PERP, 2.0, "sell")

    assert _position(seeded_singleton) == pytest.approx(1.0)
    assert "close_position" in message
    assert "short" in message.lower()


def test_a_stale_size_cannot_open_a_short(seeded_singleton, monkeypatch):
    """Replay of the incident: a size read before the close must not open a short after it.

    This is the case a position-shaped gate cannot see — the book is *flat*, so "sell the
    0.6 I read" and "open a 0.6 short" are the same order. Only the read count separates
    them, which is why the assertion is that the fill does not happen at all.
    """
    monkeypatch.setattr(tools.view, "rerender", lambda: None)
    _mark_perp()
    ctx = _exec_ctx({})
    tools.execute_trade._tool.function(ctx, PERP, 0.6, "buy")
    tools.get_portfolio._tool.function(ctx)  # the read the model is carrying
    tools.close_position._tool.function(ctx, PERP)  # a fill it has not seen
    assert _position(seeded_singleton) == pytest.approx(0.0)

    refusal = tools.execute_trade._tool.function(ctx, PERP, 0.6, "sell")

    assert _position(seeded_singleton) == pytest.approx(0.0)
    assert "REJECTED" in refusal
    assert "get_portfolio" in refusal

    # A deliberate short is still possible — after reading the book it is actually holding.
    tools.get_portfolio._tool.function(ctx)
    tools.execute_trade._tool.function(ctx, PERP, 0.6, "sell")
    assert _position(seeded_singleton) == pytest.approx(-0.6)


def test_a_malformed_size_gets_the_executors_message(seeded_singleton, monkeypatch):
    """A NaN or a negative size is not a stale read; answering it with one loops the model."""
    monkeypatch.setattr(tools.view, "rerender", lambda: None)
    _mark_perp()
    ctx = _exec_ctx({})
    tools.execute_trade._tool.function(ctx, PERP, 0.6, "buy")

    for bad in (float("nan"), float("inf"), -1.0, 0.0):
        message = tools.execute_trade._tool.function(ctx, PERP, bad, "sell")
        assert "REJECTED (not filled): 1 fill" not in message
        assert "Quantity" in message or "positive" in message.lower(), (bad, message)


def test_close_position_offers_the_model_no_quantity_to_get_wrong():
    """The whole fix rests on this: an exit the model cannot size."""
    schema = tools.close_position.tool_schema
    assert set(schema.parameters_json_schema["properties"]) == {"product_id"}
    assert schema.parameters_json_schema["required"] == ["product_id"]


def test_both_deployables_register_the_same_tools():
    """The agent is told about a tool the worker must serve; a mismatch is an unknown-tool
    error the model cannot recover from, and the restart order makes it easy to hit."""
    from deploy.agent import AGENT_TOOLS
    from deploy.tools_and_dashboard import WORKER_TOOLS

    agent_names = {tool.tool_schema.name for tool in AGENT_TOOLS}
    worker_names = {tool.tool_schema.name for tool in WORKER_TOOLS}
    assert agent_names == worker_names
    assert "close_position" in agent_names
