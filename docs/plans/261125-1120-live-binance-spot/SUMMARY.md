# Implementation Plan: Live Binance Spot Trading

> Created: 2026-11-25 11:20:01
> Status: **draft — waiting for review**

## Objective

Move the arena from simulated fills to **real Binance Spot orders** without changing how agents
behave. Today `execute_trade` invents a fill from the Kafka price book; afterwards it must send a
real order and bookkeeping must record what the venue actually did.

The hard requirement from the operator: **trading spot, restart-safe.** If the engine dies at any
moment, the next start must resume from the last persisted state, with no double-buy, no phantom
position, and no lost fill.

## Scope

### In scope

- A venue-execution seam in the tool path: `paper` (today's behaviour) and `live` (Binance Spot).
- Binance signed REST client: order placement, order lookup, balances, exchange info.
- Venue-accurate instruments: step size, min qty, min notional, per-symbol allowlist.
- Durable **order journal** with deterministic client order IDs → crash-safe idempotency.
- **Startup reconciliation**: exchange balances are the truth for quantities, the ledger is the
  truth for attribution. Mismatch ⇒ engine starts but live ordering stays disarmed.
- Risk gates + kill switch (per-order, per-position, portfolio, daily loss, order rate).
- Binance **Spot testnet** end-to-end validation, plus a crash/restart drill.
- Operator surface: `manage_bot.py` live status, halt/resume, reconcile report.

### Out of scope (v1)

- Resting orders (`LIMIT GTC`), OCO/TP-SL, margin/futures/futures-style shorts.
- Binance user-data stream (`listenKey`) — not needed for market-only, see D3.
- Withdrawals, transfers between wallets, BNB fee-burn optimisation.
- Binance **sub-accounts** (one account per agent) — deferred, see D1.
- Coinbase live execution.

## What is actually missing today (evidence)

| # | Gap | Where |
|---|-----|-------|
| 1 | Fills are invented at `best_ask`/`best_bid` from the price book; the venue is never contacted | `arena/account_store.py:184`, `:226` |
| 2 | Quantity is validated as "1 decimal place" — Binance steps are SOL `0.001`, XRP `0.1`, LINK `0.01` | `arena/account_store.py:173-179` |
| 3 | No min-notional check anywhere; a `0.1 LINK` (~$2.5) buy is legal in paper and rejected live (`-1013`) | `arena/account_store.py` (absent) |
| 4 | `fills` has no client order id → a retry after a crash double-books the same fill | `arena/ledger.py:437-443` |
| 5 | The Binance connector is read-only: no keys, no signing, no order endpoints | `exchanges/binance.py` (whole file) |
| 6 | Cash is a constant; nothing ties it to a real balance | `arena/models.py:11` |
| 7 | Fees are a config constant, not the venue's actual commission (and may be paid in BNB or base asset) | `config.py:83`, `arena/fees.py:41-71` |
| 8 | Ledger timestamps are naive local time; venue gives UTC epoch ms | `arena/account_store.py:283`, `arena/ledger.py:439` |

Verified live from this machine (2026-11-25): `api.binance.com` reachable;
`SOLUSDT` step `0.001` / minNotional `5`; `XRPUSDT` step `0.1`; `LINKUSDT` step `0.01`;
rate limits `ORDERS 100/10s`, `REQUEST_WEIGHT 6000/min`; `testnet.binance.vision` reachable;
`POST /api/v3/userDataStream` now returns **410 Gone** (deprecated — another reason to skip it).

## Architecture & Approach

```
        exchange connector (unchanged, read-only market data)
                          │  Kafka market_data.prices
                          ▼
┌──────────────── deploy.tools_and_dashboard (the "engine", single ledger writer) ─────────────┐
│  PriceBook ──► execute_trade tool ──► Executor (new seam, arena/execution.py)                │
│                                          │                                                   │
│                    ┌─────────────────────┴──────────────────────┐                            │
│                    ▼                                            ▼                            │
│              PaperExecutor                             LiveBinanceExecutor                   │
│           fill @ best_bid/ask                       InstrumentRules (exchangeInfo)          │
│                    │                                RiskGates ──► BinanceSpotClient          │
│                    │                                OrderJournal (SQLite, WAL)               │
│                    └───────────────┬────────────────────────────┘                            │
│                                    ▼                                                          │
│                    AccountStore.apply_fill()  ← pure bookkeeping, idempotent by order id     │
│                                    ▼                                                          │
│                              Ledger (SQLite)                                                  │
│                                                                                               │
│  startup: Reconcile(exchange balances ↔ ledger positions) ──► ARMED | HALTED                  │
└───────────────────────────────────────────────────────────────────────────────────────────────┘
```

### Decisions

**D1 — Account topology: one shared Binance account, per-agent virtual wallets, global gate.**
Each agent keeps its own `$N` allocation in the ledger (that is what makes the arena a
leaderboard). The executor enforces `Σ exposure + reserve ≤ real free balance` before any order.
Rejected for v1: Binance sub-accounts (one key set per agent) — real isolation, but it drags in
`/sapi/v1/sub-account/*`, universal transfers, per-account fee tiers and a second class of
reconciliation bugs before the first real order is even placed. The seam is account-keyed
(`live.agent_accounts: {agent → account}`), so sub-accounts can land later without an API change.

**D2 — Order type: `MARKET` only, `quoteOrderQty` on buys, `quantity` on sells, `newOrderRespType=FULL`.**
Market orders are immediate-or-nothing, so there are no resting orders to leak into the next
session, and the response carries `fills[]` (price, qty, commission, commissionAsset). A
`LIMIT IOC` with a slippage cap is the future refinement; it introduces partial fills and
`EXPIRED`-with-zero-fill states that are not worth the complexity before the first live trade.

**D3 — Fill source: the synchronous order response, not the user-data stream.**
With MARKET-only, `POST /api/v3/order` already returns the executed quantity and commission.
Skipping `listenKey` removes a whole reconnection/ordering surface — and the endpoint is
deprecated anyway (410). If the HTTP call times out we do **not** retry blindly; the journal
entry stays `UNKNOWN` and startup reconciliation resolves it via
`GET /api/v3/order?origClientOrderId=…`. Revisit when resting orders are added.

**D4 — Two sources of truth, never mixed.**
Quantities/fees: the exchange. Attribution/history: the ledger. Startup compares them and
refuses to arm on a shortfall or on stray open orders (`spec: phase-04`).

**D5 — Booking a real fill is a distinct code path from pricing it.**
`AccountStore._execute_trade_locked` currently does validation + pricing + bookkeeping in one
method. It splits into `apply_fill(...)` (pure bookkeeping: cash, cost basis, realised P&L, fees,
recorder, ledger) and the executors that decide *what* filled and at what price. Paper and live
then share one accounting implementation — the only way P&L stays comparable between the two.

**D6 — Paper mode becomes venue-faithful (confirmed).**
`trading.paper.instrument_rules = "venue"` enforces step size and min notional in paper too,
because a paper run that permits an order the venue rejects (`0.1 LINK` < 5 USDT) trains agents on
a false market. Cache from `exchangeInfo` (`data/instruments-binance.json`), fall back to the old
1-decimal rule when offline so the unit suite stays hermetic.

**D7 — Crash safety is a journal, not a hope.**
Every intent is written **before** the POST, keyed by a deterministic `clientOrderId`
(`arena-<agent>-<journal-seq>`). State machine:
`PENDING → SUBMITTED → FILLED | PARTIALLY_FILLED | EXPIRED | REJECTED` and `PENDING|SUBMITTED → UNKNOWN`
on a network error. Fill application is idempotent per `client_order_id` (partial unique index +
insert-then-update in one transaction), so replay after a crash is a no-op.

**D8 — Fail closed on state, fail loud on money.**
Ledger write failure keeps today's fail-open (ADR 0003). Anything that makes the ledger and the
exchange disagree — unknown order, shortfall, stray open order, daily loss breach — disarms live
ordering and leaves the arena running in a clearly-labelled `HALTED` state instead of guessing.
Arming is automatic: a restart that reconciles cleanly starts trading again (that is the operator's
"stop anytime, resume from last state" requirement), but **an operator halt is sticky** — it survives
restarts until someone runs `resume`.

**D9 — Halt is a sentinel file, checked per order.**
`data/LIVE_HALT` (plus a `meta` row for history) because the engine holds the ledger's exclusive
writer lock — a second process cannot write the DB, but it can create a file. `manage_bot.py halt`
touches it; `resume` requires a clean reconcile.

**D10 — External funding is adopted automatically, but only in the safe direction.**
The operator tops up the spot wallet by hand; the bot must see it. A background refresher polls
`GET /api/v3/account` every `live.balance_refresh_seconds` (default 30) and the reconciler compares
`real_equity` with `ledger_equity`:
- **surplus** (real > ledger) that has held steady across two consecutive reconciles and exceeds
  `live.sync_tolerance_usdt` (default 1) is credited to the agent's `cash` and written to a new
  `equity_events` row (`kind=deposit`) — the agent's `get_portfolio` then shows the new balance and
  it sizes accordingly;
- **shortfall** (real < ledger) never auto-adjusts: it means the ledger promised money that is not
  there, so live ordering disarms and the operator must either restore the funds or explicitly
  accept the difference with `manage_bot.py reconcile --write-down <amount>` (which journals
  `kind=write_down`).
The asymmetry is deliberate: a bug that *invents* money must never be papered over by a sync, while
a top-up is not a state disagreement — it is an event, and events are logged.

**D11 — Guardrails are percent-of-equity, not absolute amounts.**
Sizing is the agent's decision, so the caps must not encode a position size. `max_order_pct_of_equity`
(25) and `max_position_pct_of_equity` (50 for week 1, nullable) scale with the balance; the only
absolute numbers are backstops for the venue link (`max_orders_per_minute`, `max_price_age_seconds`)
and one optional `max_order_notional_usdt` ceiling for the first live week. The daily-loss halt is a
percentage of session-start equity (`max_daily_loss_pct`, 10) so it also scales with deposits.

## Phases

- [x] **Phase 1: Execution seam** — extract `apply_fill`, introduce `Executor` + `PaperExecutor`; zero behaviour change  
  _(done: seam lives in `AccountStore.execute_trade` → `Executor`; `arena/tools.py` needed no change)_
- [x] **Phase 2: Venue-accurate paper** — instrument rules, min notional, config, risk-gate policy  
  _(done: `trading.paper.instrument_rules`, `trading.instrument_cache`, `arena/instruments.py`, `arena/risk.py`. `RiskGates` is tested but has no consumer until the live executor in phase 3; `trading.mode` deliberately deferred to phase 3 so nothing can claim `live` without a live executor. Cache CLI is `uv run python -m arena.instruments --refresh`, not a `manage_bot.py` subcommand. Side effect: findings B2/B5/B6/R5 fixed.)_
- [x] **Phase 3: Binance Spot client + order journal** — signed REST, quantizer, idempotent live booking  
  _(done: `exchanges/binance_spot.py` (HMAC signing, clock offset, venue rate limits, error mapping, `credited_qty`), ledger v2 with in-place v1→v2 migration + `orders`/`equity_events`/`fills.client_order_id`, `LiveBinanceExecutor` (journal-first, dry-run, pre-submit cash/holdings checks, kill-switch on IP ban), `trading.mode`/`trading.live` config (fail-closed). ADR 0004–0005 written. Live fills in this DB: 0 — paper resumed after migration with all history intact.)_
- [x] **Phase 4: Restart safety** — startup reconciliation, ARMED/HALTED state machine, crash drills  
  _(done: `arena/reconcile.py` (baseline-aware comparison, two-sample equity sync), `arena/live_session.py` (LiveState + startup settle-then-compare, 30s balance refresher, disarm on drift, never silently re-arm), `LiveBinanceExecutor.settle_dangling` with six crash-point tests, `manage_bot.py halt|resume|reconcile|live-init` and live lines in `status`. ADR 0006–0007 written. 495 tests green.)_
- [ ] **Phase 5: Testnet end-to-end** — real orders on `testnet.binance.vision`, soak + restart under load
- [ ] **Phase 6: Real-money rollout** — one agent, tiny limits, staged enablement

## Key Changes

| File | Change |
|------|--------|
| `arena/execution.py` (new) | `Executor` protocol, `PaperExecutor`, `LiveBinanceExecutor`, `FillRequest`/`FillOutcome` |
| `arena/instruments.py` (new) | `SymbolRules`, step/notional quantizer, `exchangeInfo` cache loader |
| `arena/risk.py` (new) | `RiskGates` (order/position/portfolio/daily-loss/order-rate), pure + unit-testable |
| `arena/reconcile.py` (new) | startup diff of exchange balances vs ledger, dangling-order resolution |
| `exchanges/binance_spot.py` (new) | signed REST client: time sync, balances, exchangeInfo, place/lookup order |
| `arena/account_store.py` | split `apply_fill` out; accept `client_order_id`/actual fee; venue quantity rules injected |
| `arena/ledger.py` | `SCHEMA_VERSION` 1→2: `orders` table, `equity_events` table, `fills.client_order_id` + partial unique index, `meta` live keys, v1→v2 migration |
| `arena/tools.py` | `execute_trade` delegates to the executor; docstring/report gains real fee + venue limits |
| `arena/models.py` | `INITIAL_CASH` seeded per agent at go-live (no longer a constant assumption) |
| `config.py`, `config.schema.json` | `trading.mode`, `trading.live.*`, `trading.paper.instrument_rules` + validation |
| `deploy/tools_and_dashboard.py` | build the executor from config, reconcile on startup, arm/disarm, halt check |
| `manage_bot.py` | `live` fields in `status`, `halt` / `resume` / `reconcile` subcommands |
| `docs/adr/0004..0007` | seam + authority split, MARKET-only/sync-fill, shared account topology, external-funding adoption policy |

## Verification Strategy

- Per phase: `uv run pytest -q -m "not llm and not broker"`, `uv run ruff check .`.
- New pure modules (`instruments`, `risk`, `reconcile`, journal state machine) tested TDD-first,
  no network, `httpx.MockTransport` for the client.
- Opt-in markers: `binance_testnet` (real orders on testnet, needs keys), `binance_live` (never CI).
- Manual drills (phase 4/5): `kill -9` the engine mid-order, restart, assert exactly one fill.

## Dependencies

No new packages. `httpx` and `sqlite3` already present; signing uses stdlib `hmac`/`hashlib`.

## Risks & Mitigations

| Risk | Mitigation |
|------|-----------|
| LLM agent spends real money on a bad decision | Per-order/position/portfolio caps + daily-loss halt; start with $10 orders |
| Crash between POST and booking ⇒ double buy | Pre-POST journal + deterministic `clientOrderId` + venue-side idempotency (`-2010` / duplicate reject) + reconcile |
| Exchange and ledger drift (manual trade, dust, fee in BNB) | Baseline snapshot at go-live; startup diff; HALTED instead of trading on stale state |
| Real fee ≠ configured fee destroys P&L attribution | Book actual `commission`/`commissionAsset` from the fill response |
| Agent loops on rejected orders (`-1013`, min notional) | Error text names the rule and the allowed range; `max_orders_per_minute` gate |
| Binance IP ban from a hot order loop | Order/weight token buckets built from `exchangeInfo.rateLimits`; 429/418 ⇒ disarm |
| Secret leakage | Keys only via `${BINANCE_API_KEY}`/`${BINANCE_API_SECRET}`; `.env` gitignored; trade-only key without withdrawal |
| Paper results no longer comparable to live | Phase 1 keeps one accounting path for both |

## Decisions confirmed (2026-11-25)

| # | Question | Decision |
|---|----------|----------|
| 1 | Capital & agent scope | **100 USDT total is acceptable loss.** One agent on day one: allocate **95 USDT**, keep **5 USDT reserve** (`live.reserve_usdt`). Sizing caps are **percent-of-equity** (`max_order_pct_of_equity = 25`, `max_position_pct_of_equity = 50`) plus a temporary absolute ceiling `max_order_notional_usdt = 25` for week 1; `max_daily_loss_pct = 10`, `max_orders_per_minute = 2`. A top-up raises every cap with the balance (D10/D11). Single global `mode`, so the other three agents are simply **not mapped** in `live.agent_accounts` and stay stopped until phase 6 step 6 (they would only receive ticks and be denied). |
| 2 | Paper switches to venue rules (D6) | **Yes, now** — phase 2 ships it, including updating the two tests that encode the 1-decimal rule. |
| 3 | Binance sub-accounts | **Later** — D1 stands (shared account, per-agent virtual wallets, global exposure gate). |
| 4 | Auto-arm vs manual arm | **Auto-arm on a clean reconcile**, no extra command on the normal path. An operator `halt` is sticky across restarts. |
| 5 | BNB fee-burn | **Toggle stays on, no BNB purchased.** With a zero BNB balance Binance bills the fee in the received asset (base on a buy, quote on a sell). The executor books whatever `commissionAsset` the fill reports, and phase 6 step 4 verifies the first live order's `fee_asset` by hand. |

## Residual questions

- **Fee tier** — `trading.fees.taker_bps` stays 10 bps (Binance Spot VIP 0) as the *estimate* used by
  the pre-trade gate and the agent's cost disclosure. Real P&L uses the venue's `commission`, so a
  tier mismatch only makes the estimate conservative. Confirm after the first live order.
- **Symbols on day one** — `allow_symbols = ["SOLUSDT"]` in phase 6 (min notional 5 USDT, step 0.001,
  so a 10 USDT order is 0.05 SOL and legal). `XRPUSDT`/`LINKUSDT` join only after step 6a.
- **Deposit policy with >1 agent** — `live.deposit_policy` defaults to `credit_sole_agent`, which is
  unambiguous while one agent trades. Decide `proportional` vs `unallocated` before the second agent
  goes live in phase 6 step 6b.
