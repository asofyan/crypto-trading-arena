# Paper perpetual futures: signed net positions, 1:1 leverage, real taker fees

**Status:** accepted — the flipping half of decision 2 is superseded for the agent path by
[ADR-0011](0011-the-agent-names-an-exit-the-server-sizes-it.md) (an agent order never crosses
zero; exits are sized by the server). The signed-net-position model itself is unchanged.

## Context & decision

The arena was spot-only: a position is inventory you bought, cash funds it, and `sell`
means "sell what you hold". A scalping strategy on perpetual futures needs the opposite
shape — the same product can be held long or short, opening a short *credits* the wallet,
and the venue charges commission on the contract notional on every fill. Four decisions
make that fit this codebase without a second accounting engine.

**1. A perp is its own product id (`SOLUSDT-PERP`).** The price book, the ledger, the
dashboard and the trade log are all keyed by product id. A perp `SOLUSDT` and a spot
`SOLUSDT` have different prices, fees and accounting, so they must not collide: a bare
`sell SOLUSDT` would otherwise be ambiguous between "sell inventory" and "open a short".
`arena.instruments` owns the suffix and the three helpers (`perp_product_id`,
`is_perp_product`, `venue_symbol`), and *nothing else* decides what a perp is. Because the
decision lives in the id, every consumer — paper executor, ledger replay, portfolio
monitor — agrees with no extra wiring.

**2. The position is a signed net quantity.** `positions[pid]` is `+q` long, `-q` short,
absent flat; `cost_basis` carries the same sign so `cost_basis / qty` is the entry price on
either side. `buy` adds `+qty` and `sell` adds `-qty`, which means one rule covers opening,
adding, reducing, closing and flipping — no separate open/close tool for the model to
misuse. Cash moves by `-delta*price - fee`, so opening a short credits its notional
exactly as the venue's wallet does, and

```
equity = cash + Σ (position × mark)
```

needs no separate margin account. Check: cash 100, sell 1 @ 50, fee 0.05 → cash 149.95,
position −1; at mark 40 equity is 109.95 = profit 10 − fee 0.05.

Unlike spot, a perp fill's fee is **expensed against the wallet**, not capitalised into the
cost basis, because that is what the exchange does — the commission never touches the
entry price. Realised P&L is therefore the closed lot's move minus every fee paid so far.

**3. Leverage 1:1 means gross notional ≤ equity.** At 1×, margin is 100% of notional, so
the only thing to enforce is that the sum of `|position| × mark` never exceeds equity. The
gate runs on every path that can *grow* exposure — perp opens, and a spot buy on an account
that also holds a perp, whose cash includes borrow-shaped proceeds — and on no path that
reduces it. Closing an underwater position is always allowed, even into a negative wallet,
because a simulator that traps an agent in a losing trade teaches the model the wrong
lesson. Missing or stale (>60 s) marks fail closed: an unpriced book cannot prove the
account is not levered. There is no liquidation engine and no funding payment; equity may
go negative, and new exposure is then refused.

**4. Perps are paper-only, and the fee is the venue's.** A short's proceeds live only in the
paper ledger, so they can never be proven against a spot balance sheet: `trading.mode:
"live"` with `binance_perp_symbols` set is rejected at config load, and a ledger that still
holds a perp position blocks arming with an `unreconcilable` drift. The paper fee is
`trading.fees.perp_taker_bps`, default **5 bps** — Binance USDⓈ-M VIP 0 taker, half the
spot rate — applied by product id so the replay in `deploy/portfolio_monitor.py` charges
the same number without configuration.

An agent is confined to the markets its strategy was written for (`AgentConfig.markets`,
default both). A spot prompt has no vocabulary for a short and a perp prompt reasons about
leverage a spot agent cannot see, so the executor — not just the system prompt — refuses
the wrong instrument.

Venue rules come from `fapi`'s `exchangeInfo`, cached by product id. Its `MIN_NOTIONAL`
filter uses the key `notional` and has no `applyToMarket` flag; reading only the spot
spelling would leave every futures minimum at 0 and let paper accept orders the venue
rejects.

## Consequences

- One accounting path serves both markets; the branch is `is_perp_product(product_id)`, so
  a new caller cannot forget to declare what it is trading.
- Adding real futures execution later means writing a `fapi` executor that books the
  venue's `executedQty`/`commission` — the accounting, the gate and the fee model are
  already in place.
- The dashboard and `get_portfolio` show a perp row with a `SHORT` label and a negative
  quantity; every money column is signed, so nothing special-cases the display.
- Funding rates, liquidation, cross/isolated margin and configurable leverage are out of
  scope. If leverage ever becomes configurable, the cap must move from a constant into the
  account, and the gate must be recomputed after every mark.
