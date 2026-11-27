# The agent names an exit; the server sizes it

**Status:** accepted

## Context

On 2026-11-27 at 09:27 the `scalper-perp` agent asked to sell 0.62 SNDK while it held
**0.01**. The 0.62 was the position it had read from `get_portfolio` at 09:08–09:26; a fill
at 09:26:34 had already cut it to 0.32, and a fill at 09:37:12 took it to 0.01. Sell 0.62
against a long of 0.01 does not close anything — it opens a **0.61 short**, ~$48 of a
position no turn had decided on. Nothing in the order path objected to the *shape* of it;
only the churn guard refused it, and then only because the hour's fill budget happened to
be spent. At 09:56:15 the same intent slipped past into the executor, which clamped it to
a close only because the opening leg ($3.16) fell under the venue's $5 minimum. A 0.61
leg would not have been clamped.

The model was not missing information. The fill confirmation it received at 09:26:34 says
`Position reduced: net 0.32` in plain text. What it had was **several** position snapshots
in one conversation, and it reached for the wrong one. Two properties of the harness made
that reachable: calfkit's state is an append-only `message_history` (`calfkit/models/state.py`),
so every table the agent ever read stays in context; and the strategy prompt *told* the
model it had no memory ("you do not remember earlier turns"), which is false and removes
any reason to doubt an old number.

## Decision

**1. Exits carry no quantity.** `close_position(product_id)` is the exit a model can
never get wrong — a reduce it sizes itself still works, but a full exit goes through this. It calls
`AccountStore.close_position`, which re-reads the book inside the write lock the fill is
booked under — the same method, and the same argument, the hold watchdog already used:
*"the quantity on the intent is a diagnostic, and trusting it would turn a close into an
opening order whenever an agent got there first."* Nothing in an exit can go stale,
because nothing in an exit is a number the model supplied.

**2. `execute_trade` never crosses zero for an agent.** The tool calls the store with
`allow_flip=False`, and `_preadmission_rejection` refuses an order whose booked size would
take the position through zero, naming the live size and both remedies (`close_position`,
then a fresh opening order). The store keeps flipping for callers that read the book
themselves — the watchdog, replay, live reconciliation — so the ban is agent policy, not a
weakening of the book. It is asked *before churn*: a churn refusal on the same
order says "you trade too much" and leaves the stale size in the model's hands, while the
flip refusal teaches the shape of the order. (The read-count gate below runs before both —
a size the book has moved past is the more specific diagnosis.)

**3. A perp order that adds exposure needs a book the agent has seen since the last fill.**
`get_portfolio` stamps the account's `trade_count` (`AccountStore.record_portfolio_read`), and
`execute_trade` refuses an exposure-adding perp order while the stamp is behind the book. This
is the part the shape test cannot do: "sell the 0.62 I read" and "open a 0.62 short" are the
same order when the position went to zero in between, so a position-shaped gate alone leaves
the incident's own timeline reachable — a stale size that lands on a flat book. The read count
separates them, and it also catches a *deliberate* open sized from a number that moved. Exits
are exempt: `close_position` sizes itself under the booking lock, so a stale book cannot hurt
the way out. Spot is exempt: it cannot open a position by surprise.

**4. The prompt tells the truth about memory.** The amnesia clause is replaced, in every
strategy that carried it, by what actually holds: earlier turns stay in context, they are claims
about the past, a fill may have landed since, and only the newest portfolio read is current.

## Rejected options

- **Say the position louder in the tool result.** Already done — the fill confirmation
  stated `net 0.32` and the model used 0.62 anyway. Emphasis is not a guarantee; removing
  the size from the exit is.
- **Prompt-only fix.** Cheaper, but it puts the invariant in the model's compliance. The
  same failure returns with the next model in the round-robin pool.
- **Reject only flips above the venue minimum.** That is exactly what
  `clamp_flip_to_a_close` already does, and it is why the $48 case was still reachable: it
  guards representability, not intent.
- **Bound `message_history` instead.** The correct root fix for stale-context anchoring,
  but calfkit exposes no trim/prune seam (append-only by design), so it would mean reaching
  into SDK internals. Filed as [calfkit#378](https://github.com/calf-ai/calfkit-sdk/issues/378)
  instead; it should land as a follow-up, and it also addresses the tick latency that grows
  with history length.

## Residual risk

The read-count check and the flip check both run under `_write_lock` at pre-admission, and the
booking happens in a later lock acquisition. A fill landing inside that window would still pass
(perps are paper-only and `max_workers: 1`, so the window is microseconds). Closing it properly
means carrying the guard into `FillRequest` and re-checking it in `apply_fill`, where `pos_old`
is final — worth doing before `max_workers > 1`.

The prompt now also states two things it used to hide: `execute_trade` refuses to *add* exposure
until the agent has re-read the portfolio, and a reversal inside the exit band is impossible
(the band refuses the close), so a reversal is not a way out of a stalled position.

## Consequences

- A reversal now costs two fills instead of one, and therefore two churn slots. That is
  deliberate: with a 6-fill/hour budget against a 2–4 round-trips/hour target, a reversal
  is still affordable, and one order that reverses is one order that can reverse by
  accident.
- The exit band still decides *when* an exit is allowed; only its *size* moved to the
  server. `close_position` is subject to the churn guard, the exit floor, the leverage cap
  and the venue rules exactly as `execute_trade` was.
- Both tool servers must register `close_position` (`deploy/agent.py`,
  `deploy/tools_and_dashboard.py`). A tools process older than the agent would answer
  `unknown tool`, so restart the tools server before the agent.
- The fanout state topics (`calf.fanout.<agent>.state` / `.basestate`) were created by an
  earlier calfkit with `cleanup.policy=delete`, which ktables warns can evict keys. Both
  were altered to `compact` on the local broker
  (`kafka-configs --alter --add-config cleanup.policy=compact`). Fresh brokers self-provision
  compacted; only pre-existing topics need the manual alter.
