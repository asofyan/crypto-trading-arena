"""One model request against a local portfolio snapshot per market tick."""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from arena.models import AgentAccount

if TYPE_CHECKING:
    from arena.ledger import Ledger

log = logging.getLogger(__name__)
DECISION_TOPIC = "arena.trade_decision.v1"

DECISION_INSTRUCTIONS = """
Use the portfolio snapshot supplied with this tick. These execution instructions
override the strategy's tool-use and output instructions. Return exactly one JSON
object, without markdown. The permitted forms are:
{"action":"hold"}
{"action":"close_position","product_id":"PRODUCT"}
{"action":"buy","product_id":"PRODUCT","quantity":1.0}
{"action":"sell","product_id":"PRODUCT","quantity":1.0}
Use close_position for a full exit. The server sizes the exit and applies risk
and execution rules. A reversal takes separate ticks. Each position's signed
quantity, average cost and age in seconds are in the portfolio snapshot. Use the
market data in the tick to value the positions.
"""


class TradeDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["hold", "close_position", "buy", "sell"]
    product_id: str | None = Field(default=None, pattern=r"^[A-Z0-9][A-Z0-9_-]*$", max_length=80)
    quantity: float | None = Field(default=None, gt=0, allow_inf_nan=False, strict=True)

    @model_validator(mode="after")
    def check_fields(self) -> TradeDecision:
        if (self.product_id is not None) != (self.action != "hold"):
            raise ValueError("Only orders carry a product")
        if (self.quantity is not None) != (self.action in ("buy", "sell")):
            raise ValueError("Only buy and sell carry a quantity")
        return self


class SnapshotDecision(BaseModel):
    agent_id: str = Field(min_length=1)
    trade_count: int = Field(ge=0, strict=True)
    decision: TradeDecision


@dataclass
class SingleCallTick:
    agent_id: str
    ledger: Ledger
    request: Callable[[str], Awaitable[str]]
    publish: Callable[[TradeDecision, int], Awaitable[None]]

    can_request: Callable[[], bool] | None = None

    async def run(self, market: str) -> str:
        if self.can_request is not None and not self.can_request():
            log.info("Skipping model request for %s while trading is disarmed", self.agent_id)
            return "Skipped tick while trading is disarmed."
        accounts, _, degraded = self.ledger.read_portfolio()
        if degraded:
            return "Skipped tick while ledger is degraded."
        account = accounts.get(self.agent_id, AgentAccount())
        now = datetime.now().timestamp()
        snapshot = {
            "cash": account.cash,
            "positions": {
                product: {
                    "quantity": quantity,
                    "average_cost": account.avg_cost_per_unit(product),
                    "age_seconds": max(0, now - account.avg_entry_ts[product])
                    if product in account.avg_entry_ts else None,
                }
                for product, quantity in account.positions.items()
            },
        }
        output = await self.request(f"{market}\n\nPortfolio snapshot\n{json.dumps(snapshot)}")
        try:
            decision = TradeDecision.model_validate_json(output)
        except ValidationError:
            log.warning("Invalid one-call decision for %s", self.agent_id)
            return "Invalid decision."
        if decision.action != "hold":
            await self.publish(decision, account.trade_count)
        return decision.action
