# Execution seam: the venue owns quantities, the ledger owns attribution

**Status:** accepted

## Context & decision

Paper fills were priced *and* booked in one method (`AccountStore._execute_trade_locked`):
it read `best_ask`/`best_bid` off the price book, invented the fee from `FeeModel`, and
mutated the account in the same breath. Adding a real venue to that method would have
produced two bookkeeping implementations that could disagree about the same fill — which
is exactly what makes paper P&L untrustworthy next to live P&L.

We split the concern in two:

- **`Executor`** (`arena/execution.py`) answers *what* filled and at what price.
  `PaperExecutor` fills at the touch and charges the configured taker fee;
  `LiveBinanceExecutor` sends a market order and books the venue's own
  `executedQty` / `cummulativeQuoteQty` / `commission`.
- **`AccountStore.apply_fill`** answers *how it is booked*: cash, cost basis, realised
  P&L, fees, trade log, ledger. One implementation, both modes.

`AccountStore.execute_trade` stays the single entry point and delegates to whatever
executor is installed, so agents, tools and the dashboard never learn which mode is
running.

**Authority split.** The exchange is the only truth for quantities and fees; the ledger
is the only truth for *which agent* did it. They are reconciled at startup rather than
kept in sync optimistically, because a ledger that quietly "fixes" itself cannot be
audited.

## Consequences

- Paper mode rehearses the real constraints (step size, min notional) via
  `trading.paper.instrument_rules = "venue"`, so a paper fill that would be rejected live
  is rejected in paper instead.
- A live fill that the account cannot book must never happen: the executor pre-checks
  cash (with a slippage buffer) and holdings *before* contacting the venue.
- Ledger writes for live fills go through `settle_fill`, which is idempotent per
  `client_order_id`; the paper path keeps `save_fill`. Both persist the same account
  snapshot through `_persist_account_state`.
- Cross-mode fee comparison is now meaningful: live rows carry the venue's commission
  and `fee_asset`, paper rows carry the configured rate.
