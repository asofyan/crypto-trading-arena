# Design: `scalper-perp` — Binance perpetual futures, paper, 1:1 leverage

> Created: 2026-11-26 08:10
> Status: **locked once lock-design review passes**

## Objective

Add a second trading venue shape to the arena: **Binance USDⓈ-M perpetual futures**, simulated
(paper), where an agent may hold a **long or a short** position with **1:1 leverage** (no notional
amplification) and pays the venue's **real taker fee**. The first consumer is a scalping prompt
named `scalper-perp`.

No real futures orders are placed in this change. Live execution stays Binance **Spot** only, as
already documented in `docs/plans/261125-1120-live-binance-spot/` ("margin/futures/futures-style
shorts" was explicitly out of scope for live v1).

## Decisions

### D1 — Product identity: `<SYMBOL>-PERP`

A perpetual contract is addressed as `SOLUSDT-PERP`; the venue symbol is `SOLUSDT`. Spot products
keep their bare ids (`SOLUSDT`).

Why: the arena already has one shared `PriceBook`, one ledger and one dashboard keyed by product id.
A perp `SOLUSDT` and a spot `SOLUSDT` are different instruments with different prices, fees and
accounting, so they must not collide. The suffix is the single, documented convention:

```python
# arena/instruments.py
PERP_SUFFIX = "-PERP"
def perp_product_id(symbol: str) -> str: ...
def is_perp_product(product_id: str) -> bool: ...
def venue_symbol(product_id: str) -> str: ...
```

`is_perp_product` is the **only** thing the accounting layer consults. No extra wiring in
`AccountStore`, `PaperExecutor` or `deploy/portfolio_monitor.py`.

### D2 — Net position, signed quantity

`AgentAccount.positions[product_id]` becomes **signed**: `+q` long, `-q` short, `0`/absent flat.
`cost_basis` is signed too, so `avg_cost_per_unit() = cost_basis / qty` keeps returning the entry
price for both sides (`-50 / -1 = 50`).

Tools keep the spot vocabulary: `buy` / `sell`. On a perp product they mean *direction of the net
position change*, not open/close:

| state | `buy q` | `sell q` |
|---|---|---|
| flat | open long `+q` | open short `-q` |
| long `+p` | add long | reduce; if `q > p`, close and flip short |
| short `-p` | reduce; if `q > p`, close and flip long | add short |

This is exactly how a futures position behaves and it requires no new tool for the LLM.

### D3 — Cash and equity

Cash moves by `-delta*price - fee` where `delta = +qty` on buy and `-qty` on sell. Opening a short
therefore **credits** the notional (as a real futures wallet does), and equity is:

```
equity = cash + Σ (position_qty × mark_price)
```

Short check: cash 100, sell 1 @ 50, fee 0.05 → cash 149.95, position -1. Mark 40 → equity
`149.95 - 40 = 109.95` = profit 10 − fee 0.05. Closing at 40: cash `149.95 - 40 - 0.04 = 109.91`,
position 0, equity 109.91. Realized = `(50-40)*1 - 0.05 - 0.04 = 9.91`. ✅

Note the difference from spot: for perp products **fees are expensed immediately** (not capitalized
into the cost basis), because the exchange charges them against the wallet balance, not the entry
price. `realized_pnl` therefore accumulates `closed_pnl - fee` for every perp fill.

### D4 — Leverage 1:1 = gross notional ≤ equity

Margin at 1× is 100% of notional. A fill may only proceed if, after it, the account's **gross
notional** (Σ `|qty| × mark` over every position, spot or perp) is at most its equity:

```
gross_after <= equity_after + EPS
```

Only **increasing** exposure is gated (`gross_after > gross_before`); closing or reducing is never
blocked, so an agent can always get out. A breach returns a `TradeResult(False, ...)` that states
the equity, current gross and the maximum notional it may hold — the LLM can act on that.

There is no liquidation engine and no funding payment in this change: equity may go negative if a
short runs against the position hard, and new exposure is then refused (`equity_after <= 0`).

Residual constant: `_MARGIN_EPSILON = 1e-6` USD (float dust only, never a real allowance).

### D5 — Fees: `taker_bps` (spot) and `perp_taker_bps` (perp)

`FeeModel` gains `perp_taker_bps`, default **5** (Binance USDⓈ-M VIP 0 taker = 0.05%; spot VIP 0 is
10 bps). `buy_cost` / `sell_proceeds` / `disclosure_prompt` take `perp: bool = False`.

Config:

```json
"fees": { "taker_bps": 10, "perp_taker_bps": 5 }
```

The perp connector discloses the perp rate in its prompt, the spot connector the spot rate. The
paper executor picks the rate per product via `is_perp_product`, so `deploy/portfolio_monitor.py`
replays fills at the same rate without extra configuration.

### D6 — Venue rules come from `fapi`, keyed by product id

`arena/instruments.py` learns a `product_suffix` and a venue `path`:

- futures `exchangeInfo`: `GET https://fapi.binance.com/fapi/v1/exchangeInfo`
- `MIN_NOTIONAL` on futures carries `{"filterType":"MIN_NOTIONAL","notional":"5"}` — no
  `minNotional`, no `applyToMarket`. The parser must read `notional` as a fallback, otherwise every
  futures symbol would silently get a 0 minimum and paper would accept orders the venue rejects.
- cache keys are product ids (`SOLUSDT-PERP`); `refresh_cache(..., product_suffix="-PERP")`.

Real futures minimums (verified 2026-11-26): SOLUSDT 5 USDT / step 0.01, XRPUSDT 5 / 0.1,
LINKUSDT 20 / 0.01, BTCUSDT 50 / 0.001.

### D7 — Market data from the futures venue

`exchanges/binance.py` becomes venue-parameterised (spot + perp), and
`exchanges/binance_perp.py` is a thin entrypoint:

- WS `wss://fstream.binance.com/stream?streams=solusdt@ticker` — same `24hrTicker` event shape
  (includes `b/B/a/A`), so the ticker parser is shared.
- REST `https://fapi.binance.com/fapi/v1/klines` — same array shape as `/api/v3/klines`.
- REST 24h ticker on futures has **no** `bidPrice`/`askPrice`; bid/ask come from
  `/fapi/v1/ticker/bookTicker` (all-symbols, weight 2) and stats from `/fapi/v1/ticker/24hr`
  (all-symbols, weight 40). Two calls per poll cycle, at most once per 60 s.
- the Kafka message publishes `product_id = "SOLUSDT-PERP"`.

### D8 — Live trading stays spot-only

`trading.mode == "live"` still refuses anything that is not `exchange == "binance"`, and
`LiveBinanceExecutor` only ever looks up spot rules. Perp products are paper-only in this change;
the engine prints that fact at startup rather than pretending otherwise.

## Accounting algorithm (perp branch of `AccountStore._apply_fill_locked`)

```
delta        = +qty (buy) | -qty (sell)
pos_old      = positions.get(pid, 0)                 # signed
entry_old    = cost_basis.get(pid, 0) / pos_old      # 0 when flat

# 1. leverage gate (only when exposure grows)
positions_after = pos_old + delta
cash_after      = cash - delta*price - fee
equity_after    = cash_after + Σ positions_after[i] * mark[i]
gross_before    = Σ |positions[i]| * mark[i]
gross_after     = Σ |positions_after[i]| * mark[i]
if gross_after > gross_before + EPS and gross_after > equity_after + EPS: reject
if gross_after > gross_before + EPS and equity_after <= 0:                reject

# 2. realisation
closing        = pos_old != 0 and sign(pos_old) != sign(delta)
closed_qty     = min(|delta|, |pos_old|) if closing else 0
closed_signed  = -sign(pos_old) * closed_qty
closed_pnl     = closed_qty * (price - entry_old) * sign(pos_old)

# 3. cost basis: old lot retained at old entry, remainder booked at this fill
remainder      = delta - closed_signed
cost_basis_new = (pos_old + closed_signed) * entry_old + remainder * price
pos_new        = pos_old + delta

cash          += -delta*price - fee
realized_pnl  += closed_pnl - fee
total_fees_paid += fee
trade_count   += 1
```

Properties this must keep (each one is a test):

1. flat → long: `pos=q, basis=q*px`, cash `-q*px-fee`.
2. flat → short: `pos=-q, basis=-q*px`, cash `+q*px-fee`.
3. long → flat: basis 0, realised `(exit-entry)*q - fees`.
4. short → flat: basis 0, realised `(entry-exit)*q - fees`.
5. long → bigger long: weighted average entry (incl. both fills), basis = `pos*avg`.
6. short → bigger short: same, signed.
7. long → short of the same size (flip): realised on the closed qty, new basis `-q*exit`.
8. partial reduce leaves `avg_cost_per_unit` unchanged.
9. `portfolio_value` after any sequence equals `cash + Σ qty*mark` (no basis leakage).
10. gate rejects a 2× notional open with 1× equity; gate allows a close that breaches nothing.
11. `_QTY_EPSILON` treats float dust as flat (no orphan `-1e-17` position).

## Files

| File | Change |
|---|---|
| `arena/instruments.py` | `PERP_SUFFIX` helpers, `product_suffix`, futures `exchangeInfo`/`notional` parsing |
| `arena/fees.py` | `perp_taker_bps`, `perp=` on `buy_cost`/`sell_proceeds`/`disclosure_prompt` |
| `arena/account_store.py` | signed positions, `is_perp`, perp fill branch, 1:1 gate |
| `arena/execution.py` | `PaperExecutor` charges the perp rate for perp products |
| `arena/models.py` | docstring: signed positions contract |
| `arena/strategies.py` | `scalper-perp` prompt |
| `arena/tools.py` | `execute_trade` docstring: perp long/short semantics |
| `config.py` | `binance_perp_symbols`, `perp_instrument_cache`, `fees.perp_taker_bps` |
| `config.schema.json`, `config.example.json`, `config.json` | same |
| `deploy/tools_and_dashboard.py` | merge spot+perp rules, print perp products |
| `deploy/portfolio_monitor.py` | same rules merge so replays use venue steps |
| `exchanges/binance.py` | venue parameterisation (WS/REST URLs, paths, suffix) |
| `exchanges/binance_perp.py` | futures entrypoint |
| `data/instruments-binance-perp.json` | generated from `fapi` |
| `tests/test_perp_accounting.py` | the 11 properties above |
| `docs/adr/0008-perp-paper-accounting.md` | the decision record |

## Out of scope

- Real futures order placement, funding rates, liquidation engine, cross/isolated margin modes.
- Multiple leverage settings: 1:1 only (`gross ≤ equity`), intentionally not configurable yet.
- Resting/TP-SL orders (the agent exits with market fills, like every other strategy here).
