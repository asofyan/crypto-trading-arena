# Market-only orders with a write-ahead journal, no user-data stream

**Status:** accepted

## Context & decision

Live trading has one non-negotiable requirement from the operator: the bot may stop at
any moment and resume from the last state without double-buying or losing a fill.

Three choices follow from that:

**1. `MARKET` orders only.** A market order is immediate-or-nothing, so the bot never
leaves a resting order behind. That removes the entire "what is still open from the
previous process?" class of state: there is nothing open. `LIMIT IOC` with a slippage
cap is the obvious next step, but it introduces partial fills and `EXPIRED`-with-zero
fills — complexity that is not worth paying before the first real trade.

**2. Journal before the venue.** Every intent is written to the `orders` table (state
`PENDING`) *before* the POST. After the venue answers, the row is closed (`FILLED`,
`REJECTED`, `EXPIRED`, …). If the POST times out, the row becomes `UNKNOWN` and is
**never retried blindly** — a second POST could double the position. The next startup
resolves every `PENDING`/`SUBMITTED`/`UNKNOWN` row by `client_order_id`
(`GET /api/v3/order?origClientOrderId=…`), applying the fill at most once
(`fills.client_order_id` partial unique index + insert-then-book in one transaction).

**3. No user-data stream.** With market orders, `POST /api/v3/order` already returns
`fills[]` including the commission, so a `listenKey` connection would add a
reconnect-and-ordering surface for no information. (The `POST /api/v3/userDataStream`
endpoint also answers **410 Gone** now, verified from this machine.) The commission is
normalised at the point of capture: a base-asset fee on a buy is converted at the order's
average price, and the *credited* base quantity (`executedQty − base commission`) is what
gets booked, because that is what the wallet received and what reconciliation will
compare against.

## Consequences

- Reconciliation must be able to answer "did this order exist?" with one signed GET, and
  must treat `-2013 Order does not exist` as "the POST never landed".
- Adding resting orders later means adding the user-data stream (or a periodic
  open-order sweep) and a cancel policy — this ADR is what to revisit.
- The journal doubles as the audit trail: `orders` explains every fill, including the
  rejected and the unknown ones.
- Local risk denials are **not** journaled. The journal records money-moving intents;
  a denial that never reached the venue is answered to the agent and logged, and
  flooding the table with them would hide the orders that matter.
