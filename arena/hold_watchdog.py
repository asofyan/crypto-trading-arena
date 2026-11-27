"""The other half of the exit floor: something that actually pulls the escape hatch.

``arena.trade_policy.ExitFloor`` decides *whether* a close may go out. Past the hold
limit it answers yes, and that is all it can do — a guard that never refuses a stale
exit is not a guard that closes one. In the measured run nothing closed them: four
round trips held 800 s to 3100 s past the 3600 s limit because the only actor that
could submit the order was an LLM on a market tick, and it simply stopped asking.

So the watchdog owns the timing half. It closes the gap between "closing is allowed"
and "closing happened", and it does it through ``AccountStore.execute_trade`` rather
than around it: every gate that judges an agent's order judges the watchdog's too, so
the escape hatch cannot become a side door for a trade the floor would refuse.

The selection rules are pure (:func:`stale_positions`) — no clock, no store, no I/O —
because "which positions are due" is the part worth testing exhaustively.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Mapping

from arena.models import AgentAccount, TradeResult

if TYPE_CHECKING:  # the store imports this module, so the type is only ever a hint
    from arena.account_store import AccountStore

log = logging.getLogger(__name__)

# The same epsilon the store uses to call a position flat: a float residue is not a
# position, and a close that books a fill for 1e-12 of a lot is noise wearing a fee.
_QTY_EPSILON = 1e-9


@dataclass(frozen=True)
class StalePosition:
    """A position that has outlived its hold limit, and the order that ends it."""

    agent_id: str
    product_id: str
    quantity: float  # absolute: how many units to close
    action: str  # "sell" to close a long, "buy" to close a short
    held_seconds: float


def stale_positions(
    accounts: Mapping[str, AgentAccount],
    *,
    hold_limit_s: float | None,
    now: float,
) -> list[StalePosition]:
    """Every open position that is past ``hold_limit_s`` at ``now``.

    Fails open on everything it cannot trust, matching the floor it complements: a
    missing or non-finite limit, a position with no *when*, a NaN entry time. Refusing to
    close is the trap this module exists to remove, and guessing is how a watchdog closes
    a position that was fine. A position hydrated from the ledger keeps its recorded
    entry time (``arena.ledger`` restores it); only a legacy NULL row has no *when* and
    is left alone.
    """
    if hold_limit_s is None or not math.isfinite(hold_limit_s):
        return []

    due: list[StalePosition] = []
    for agent_id in sorted(accounts):
        account = accounts[agent_id]
        for product_id in sorted(account.positions):
            quantity = account.positions[product_id]
            if not math.isfinite(quantity) or abs(quantity) <= _QTY_EPSILON:
                continue
            entry_ts = account.avg_entry_ts.get(product_id)
            if entry_ts is None or not math.isfinite(entry_ts):
                continue
            held_seconds = now - entry_ts
            if not math.isfinite(held_seconds) or held_seconds < hold_limit_s:
                continue
            due.append(
                StalePosition(
                    agent_id=agent_id,
                    product_id=product_id,
                    quantity=abs(quantity),
                    # Closing means trading against the position: a long is sold.
                    action="sell" if quantity > 0 else "buy",
                    held_seconds=held_seconds,
                )
            )
    return due


class HoldWatchdog:
    """Runs the close an agent stopped asking for once its position went stale.

    Deliberately not a second executor: every close goes through
    ``AccountStore.execute_trade``, so the churn guard, the exit floor, the leverage cap
    and the instrument rules all still apply. A watchdog that reached around them would
    be a bypass with a friendly name — and the one gate that must not fire here never
    does, because a reduce is never refused for churn and the floor allows an exit past
    the limit it is counting.
    """

    def __init__(
        self,
        store: AccountStore,
        *,
        hold_limit_s: float | None,
        interval: float = 30.0,
        clock=time.time,
    ) -> None:
        self._store = store
        self._hold_limit_s = hold_limit_s
        self._interval = interval
        self._clock = clock
        self._task: asyncio.Task | None = None

    @property
    def interval(self) -> float:
        """Seconds between ticks, so the startup banner reads it from one place."""
        return self._interval

    def due(self) -> list[StalePosition]:
        """What is stale right now, read under the store's lock.

        Blocking: the scan takes the same lock a live close holds for the length of a
        venue round trip, so callers on the event loop want :meth:`_due` instead.
        """
        return self._store.stale_positions(self._hold_limit_s, now=self._clock())

    async def _due(self) -> list[StalePosition]:
        """``due()`` on a worker thread, for the same reason the close runs there."""
        return await asyncio.to_thread(self.due)

    async def tick(self) -> list[TradeResult]:
        """Close everything that is due, and report what each close did.

        Two things here are load-bearing rather than stylistic. The size comes from
        ``AccountStore.close_position``, which re-reads the book under the booking lock:
        the quantity on the intent is a diagnostic, and trusting it would turn a close
        into an opening order whenever an agent got there first. And the close runs in a
        worker thread, because in live mode it is a blocking venue call — doing it here
        would stall every consumer of the event loop for the round trip.

        One un-closable position must not cost the others their exit: each attempt is
        isolated, so a refusal — or a bug in one product's bookkeeping — is logged and
        the watchdog keeps watching.
        """
        results: list[TradeResult] = []
        for position in await self._due():
            try:
                result = await asyncio.to_thread(
                    self._store.close_position,
                    position.agent_id,
                    position.product_id,
                )
            except Exception:  # noqa: BLE001 - a watchdog that dies stops watching
                log.exception(
                    "hold watchdog failed to close agent=%s product=%s qty=%s",
                    position.agent_id,
                    position.product_id,
                    position.quantity,
                )
                continue
            if result.success:
                log.info(
                    "hold watchdog closed agent=%s product=%s qty=%s action=%s "
                    "held=%.0fs",
                    position.agent_id,
                    position.product_id,
                    position.quantity,
                    position.action,
                    position.held_seconds,
                )
            else:
                log.warning(
                    "hold watchdog could not close agent=%s product=%s qty=%s "
                    "held=%.0fs: %s",
                    position.agent_id,
                    position.product_id,
                    position.quantity,
                    position.held_seconds,
                    result.message,
                )
            results.append(result)
        return results

    def start(self) -> asyncio.Task:
        """Begin ticking every ``interval`` seconds on the running loop."""
        self._task = asyncio.create_task(self._loop())
        return self._task

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self._interval)
            try:
                await self.tick()
            except Exception:  # noqa: BLE001 - a tick that dies stops every later close
                log.exception("hold watchdog tick failed; still watching")

    async def close(self) -> None:
        """Stop watching; a cancelled loop must not leave a task trading in the dark."""
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        # No generic `except` here: ``_loop`` already catches every error it can survive,
        # so anything escaping it is a cancellation and nothing else.
        self._task = None
