# External funding: adopt a stable surplus, require an explicit write-down

**Status:** accepted

## Context & decision

The operator funds the spot wallet by hand, mid-session. The arena sizes every order as
a percentage of equity, so a deposit that the ledger cannot see is a silently lowered
ceiling — the bot does not grow with the account.

Equity sync therefore compares `real_equity` with `ledger_equity` on every balance
refresh (default 30s, ~40 request-weight per minute of a 6000 budget) and applies one
rule per direction:

- **Surplus (real > ledger).** Adopted, but only after it has held steady across two
  consecutive samples, and only above `live.sync_tolerance_usdt` (default 1 USDT). One
  sample can be a cached or partially settled venue response, and crediting money that is
  not there is the one direction that makes the ledger lie about what it can spend. Each
  adoption is written to `equity_events` (`kind=deposit`) with the equity before and
  after, so the curve stays explainable after the fact.
- **Shortfall (real < ledger).** Never adjusted automatically. It blocks live ordering
  and the operator must either restore the funds or accept the difference with
  `manage_bot.py reconcile --write-down <amount>`, which journals `kind=write_down`.

The asymmetry is the decision: a top-up is an event and events are logged, while a
shortfall is indistinguishable from a bug that invented money, and a reconciler that
"fixes" that class of bug silently can never be trusted again.

Only the *quote* asset is adopted. A surplus of the base asset might be an external
crypto deposit or dust from a previous experiment; crediting it into an agent's position
would inflate exactly the number reconciliation checks. It is reported as a note instead.

## Consequences

- `live.deposit_policy` decides the recipient: `credit_sole_agent` (default, unambiguous
  while one agent trades), `proportional` (split by current equity), or `unallocated`
  (visible in `live session` logging, spendable by nobody until an operator allocates it).
- A withdrawal disarms trading until a human acknowledges it. This is deliberate: the
  alternative is a bot that quietly shrinks its own limits whenever the wallet shrinks,
  including when the shrink is its own accounting bug.
- The go-live baseline (`live_baseline`, written by `manage_bot.py live-init`) is what
  makes the comparison meaningful: it is the slice of the wallet the arena does *not*
  own, so pre-existing holdings are excluded rather than reported as drift forever.
