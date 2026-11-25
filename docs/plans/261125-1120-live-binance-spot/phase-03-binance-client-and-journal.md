# Phase 03: Binance Spot Client + Order Journal

## Status: **done** (2026-11-25)

Delivered: `exchanges/binance_spot.py` + 19 tests, ledger v2 (migration + journal +
idempotent `settle_fill` + `equity_events`, 17 tests), `LiveBinanceExecutor` + 21 tests,
`trading.mode`/`trading.live` config + schema regeneration, engine wiring with a
venue-validated startup, `data/LIVE_HALT` kill switch. 438 tests green, ruff clean.

### Deviations from the plan below

- **Risk denials are not journaled.** The journal records money-moving intents; a denial
  that never reached the venue is returned to the agent and logged. Flooding `orders`
  with `REJECTED` rows would bury the ones that matter.
- **`arena/tools.py` and `PaperExecutor` are unchanged.** The seam is
  `AccountStore.execute_trade`; live mode installs a different executor.
- **Buys use `quoteOrderQty`** (the quantised quantity converted to a quote amount), so
  the pre-check and the debit are dollar-exact; the bookable quantity is the venue's
  `credited_qty` (executed minus base-asset commission).
- **Pre-submit cash/holdings checks were added** beyond the plan: a fill the ledger later
  refuses would leave the venue and the ledger permanently apart.
- **Live mode fetches `exchangeInfo` at startup** and sizes against it, rather than
  trusting the cached file (which is for offline paper mode).
- **`manage_bot.py` has no `status`/`halt` surface yet** — that is phase 4; phase 3 ships
  the halt *file* and the auto-halt on IP ban.

## Objective

Place real orders, and make booking them crash-safe and idempotent. At the end of this phase
`mode = "live"` with `live.dry_run = true` works end-to-end except for the actual POST.

## Preconditions

- Phases 1–2 merged.
- Operator: API key created with **Spot Trading enabled, withdrawals disabled, IP restricted**;
  `BINANCE_API_KEY` / `BINANCE_API_SECRET` in `.env` (gitignored). Trading wallet funded with USDT.

## Tasks

1. **Test first — client.** `tests/test_binance_spot.py` with `httpx.MockTransport` (no marker, no
   network):
   - signature is `HMAC-SHA256(secret, query)` hex, appended as `signature=`, with `timestamp` and
     `recvWindow`; `X-MBX-APIKEY` header present; the signed payload excludes the header.
   - `-1021` (timestamp) ⇒ re-sync via `/api/v3/time` once and retry once.
   - `429` ⇒ raise `RateLimited(retry_after)`, never a blind retry loop; `418` ⇒ `IpBanned` and the
     caller disarms.
   - `-2010`/`-2016` ⇒ mapped to a human string the agent can act on; `-1013` ⇒ one `exchangeInfo`
     refresh + one retry, then fail.
   - `place_market_buy(quote_qty)` sends `quoteOrderQty` + `newOrderRespType=FULL`; a response with
     `status=FILLED` and two `fills[]` entries converts to one `VenueFill` with summed qty/notional
     and per-asset commission.
2. **`exchanges/binance_spot.py`.** `BinanceSpotClient(api_key, api_secret, base_url=..., transport=None)`
   with a **synchronous** `httpx.Client` — the tool node runs in a worker thread; do not import
   `asyncio` here. Methods: `sync_time()`, `balances() -> dict[asset, Balance(free, locked)]`,
   `exchange_info(symbols) -> dict[str, SymbolRules]`, `place_market_buy|sell`,
   `get_order(symbol, client_order_id)`, `open_orders()`, `my_trades(symbol, order_id)`.
   Token buckets seeded from `exchangeInfo.rateLimits` (verified: `ORDERS 100/10s`,
   `REQUEST_WEIGHT 6000/min`) — no hardcoded numbers.
   `base_url` selects `https://api.binance.com` or `https://testnet.binance.vision`.
3. **Test first — ledger schema v2.** `tests/test_ledger.py`:
   - opening a v1 database migrates to v2 in place and a subsequent full read matches the rows
     written before migration (accounts, positions, fills untouched);
   - `apply_idempotent_fill(client_order_id=X, …)` twice books exactly once, returns `False` the
     second time;
   - a v2 database opened by v1 code still raises `LedgerSchemaError` (no silent downgrade).
4. **Ledger v2.** `SCHEMA_VERSION = 2`; add
   ```sql
   CREATE TABLE orders (
     client_order_id TEXT PRIMARY KEY, ts TEXT NOT NULL, agent_id TEXT NOT NULL,
     account TEXT NOT NULL, symbol TEXT NOT NULL, side TEXT NOT NULL,
     qty_requested REAL, quote_qty_requested REAL, state TEXT NOT NULL,
     venue_order_id TEXT, executed_qty REAL, cum_notional REAL, fee REAL, fee_asset TEXT,
     price REAL, error TEXT, updated_at TEXT NOT NULL
   );
   CREATE INDEX orders_state ON orders(state);
   CREATE TABLE equity_events (
     id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, agent_id TEXT,
     kind TEXT NOT NULL,          -- deposit | write_down | baseline | initial_allocation
     amount REAL NOT NULL,        -- quote-denominated; negative for write_down
     equity_before REAL NOT NULL, equity_after REAL NOT NULL, note TEXT
   );
   ALTER TABLE fills ADD COLUMN client_order_id TEXT;   -- NULL for paper rows
   ALTER TABLE fills ADD COLUMN fee_asset TEXT;
   CREATE UNIQUE INDEX fills_client_order_id ON fills(client_order_id)
     WHERE client_order_id IS NOT NULL;
   ```
   Migration runs inside one transaction and rewrites `meta.schema_version` last. Keep the
   "migrate it or move it aside" refusal for *unknown* versions.
5. **`Ledger.save_order` / `settle_order` / `dangling_orders`.** `settle_order` applies the fill and
   the journal transition in **one** transaction: insert the `fills` row (partial unique index makes
   the insert a no-op if it exists), and only touch `accounts`/`positions` when that insert actually
   happened. That single guard is what makes replay-after-crash safe.
6. **Test first — live executor.** `tests/test_execution_live.py` with a fake venue:
   - order goes `PENDING` (journal) *before* `place_market_*` is called — assert call order;
   - a venue `FILLED` response books exactly one fill with the venue's qty/price/fee;
   - a network exception on POST leaves `UNKNOWN`, books nothing, and the tool returns a
     "order outcome unknown, will reconcile" message (never a false success);
   - `dry_run = true` writes a `DRY_RUN` journal row, calls no venue method, books no fill;
   - `is_halted()` ⇒ deny before any journal write; a risk deny ⇒ journal `REJECTED` + no venue call.
7. **`LiveBinanceExecutor`.** Flow per order:
   1. `halted` / allowlist / symbol rules / quantize / risk gates → deny ⇒ `REJECTED`, return reason;
   2. estimate cost at `best_ask` × (1 + `max_price_slippage_bps`) and pre-check agent cash
      (a live fill that lands slightly above the estimate must not be rejected post-hoc — the money
      already moved; log `WARN` and allow the ledger to go marginally negative);
   3. journal `PENDING` with a fresh `client_order_id`;
   4. POST; on 4xx/5xx-definitive ⇒ journal `REJECTED`; on timeout/network ⇒ journal `UNKNOWN`;
   5. `FILLED` ⇒ `settle_order(...)`; `PARTIALLY_FILLED`/`EXPIRED` ⇒ record what filled, halve-check;
   6. return an agent-facing message with real price, qty, fee, cash left.
8. **Fee booking.** Convert `commissionAsset` to a quote-denominated fee: quote ⇒ as-is; base on a
   buy ⇒ `commission × price`, and book the **net** base received (`executedQty − commission`) as
   the position; BNB ⇒ mark it and keep the BNB cost basis at fill price. `AgentAccount` gains no
   new field; `fills.fee` stays quote-denominated and `fills.fee_asset` records the original.
   Confirmed operator setting: BNB burn is **on** but the account holds **zero BNB**, so v1 should
   only ever see base/quote commissions; the BNB branch is a defensive path, and a fill whose
   `fee_asset == "BNB"` raises a dashboard warning because it means someone funded BNB.
9. **Engine wiring.** `deploy/tools_and_dashboard.py`: build the executor from config; print the
   active mode block (`mode live (dry-run), account main, symbols SOLUSDT, order cap 10 USDT,
   exposure cap 40 USDT`); keys resolve via `${ENV}` from config, never argv.
   Agent scope is explicit: an agent **absent from `live.agent_accounts`** gets
   `execute_trade` ⇒ "not enabled for live trading" and `get_portfolio` ⇒ "live trading not enabled
   for you" instead of a fictional $100 wallet. Only mapped agents may touch real money; the rest
   must not even be launched in live mode, because they would still receive every tick and burn
   LLM tokens to be told no.
10. **Timestamps.** New rows use `datetime.now(timezone.utc).isoformat()`; existing naive rows keep
    rendering via `_format_fill_ts`. Note the mixed convention in `Ledger`'s docstring.

## Verification

- Commands:
  - `uv run pytest -q -m "not llm and not broker"`
  - `uv run ruff check .`
  - `uv run python -m deploy.tools_and_dashboard --bootstrap-servers localhost:9092 --headless`
    with `trading.mode = "live"`, `live.dry_run = true` ⇒ engine starts, logs the mode block, and
    agent trades produce `DRY_RUN` journal rows with no positions changing.
  - `sqlite3 data/arena.db "select state,count(*) from orders group by 1"`
- Expected:
  - A v1 `data/arena.db` opens and keeps all history.
  - No venue call in dry-run; exactly one journal row per intent.

## Exit Criteria

- [ ] Signed client covered by fixture tests; no network in the unit suite.
- [ ] Ledger v2 migrates in place, keeps all history, refuses unknown versions.
- [ ] Booking a fill twice is a no-op, proven by test.
- [ ] A POST timeout never books a fill and never reports success.
- [ ] Dry-run places no order and mutates no position.
