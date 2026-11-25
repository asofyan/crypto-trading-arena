# Phase 06: Real-Money Rollout

## Objective

Put real USDT behind one agent, with limits small enough that a bug is a lesson, not a loss —
then widen deliberately. No new code in this phase unless a finding demands it.

## Preconditions

- Phase 5 complete: soak clean, all restart drills passed.
- Binance account KYC'd; API key with **Spot Trading on, withdrawals off, IP allowlisted**.
- BNB burn toggle is on but the wallet holds no BNB: place **one manual ~6 USDT market buy in the
  Binance app first** and check the fee was billed in SOL (not a rejection, not BNB). This removes
  the last unverified assumption before automation runs — two minutes of work, no code.
- `data/arena.db` archived (`manage_bot.py start --fresh` semantics) or a separate
  `--db data/live.db` for live — do not mix paper history into the live ledger's baseline.
- `docs/adr/0004`–`0007` written and merged.

## Tasks

1. **Config for day one.** `trading.mode = "live"`, `live.dry_run = false`,
   `agent_accounts = {"<one agent>": "main"}`, `allow_symbols = ["SOLUSDT"]`,
   `max_order_pct_of_equity = 25`, `max_position_pct_of_equity = 50`,
   `max_daily_loss_pct = 10`, `max_orders_per_minute = 2`, `max_price_age_seconds = 5`,
   `balance_refresh_seconds = 30`, `sync_tolerance_usdt = 1`.
   One temporary absolute ceiling for week 1: `max_order_notional_usdt = 25` — keep it, so a
   fat-fingered equity (or an unexpected deposit during debugging) cannot become a $100 order.
   Fund the spot wallet with **100 USDT total — the amount already accepted as a loss**. Nothing
   else is in that account.
2. **`live-init`** with a **95 USDT** allocation for that agent and a **5 USDT** `live.reserve_usdt`
   buffer (absorbs slippage overrun and dust-level noise so a clean run does not trip the
   reconciler). Verify the baseline rows and the seeded cash *before* arming.
3. **Dry-run pass first.** Same config, `dry_run = true`, 30 minutes, one agent, live market data:
   confirm it is denied nothing unexpectedly, the risk gate reasons read sensibly in the log, and
   the reconcile stays clean (dry-run books nothing, so any drift here is a real bug — investigate).
4. **Arm for real.** `manage_bot.py resume`; watch the first three orders in `orders`, in the
   dashboard, and against the Binance app. Confirm qty/price/fee match the venue exactly, and that
   `fills.fee_asset` is the base asset on a buy (BNB burn is on but the wallet holds no BNB, so
   Binance bills in the received asset). If it ever reports `BNB`, stop and revisit phase 3 step 8.
5. **Day-1 watch list** (30 min cadence): rejected orders, `UNKNOWN` count, ledger-vs-venue equity
   delta, daily-loss headroom, agent behaviour (does the LLM chase? does it re-enter after every
   loss?). Kill with `manage_bot.py halt` on anything unexplained — a halt costs nothing.
5b. **Top-up behaviour, verified with real money (the reason D10 exists).** After the first clean
   day, deposit **+50 USDT manually in the Binance app** and watch, without restarting:
   within `balance_refresh_seconds` × 2 the reconciler writes an `equity_events` row
   (`kind=deposit`, amount 50), `get_portfolio` shows the higher cash, and the next order the agent
   sizes is larger because the percentage caps moved with it. Then withdraw 50 USDT and confirm the
   engine **halts** with a shortfall instead of quietly adjusting — and that
   `manage_bot.py reconcile --write-down 50` is the only way out.
6. **Widen in steps**, each gated on ≥ 24h clean: (a) drop the temporary absolute ceiling, leaving
   percentage caps only; (b) add a second agent with its own allocation and pick
   `live.deposit_policy`; (c) add `XRPUSDT`/`LINKUSDT`; (d) all four agents.
7. **Post-mortem → docs.** `.run/` logs + `orders` table into a short `research/live-week-1.md`:
   slippage vs quote, real fee bps, fill latency (the existing `latency` column already measures
   invoke→fill), and whether paper and live P&L are comparable. Decide whether the next step is
   sub-accounts (D1) or the user-data stream (D3).

## Verification

- Commands:
  - `manage_bot.py status` / `status --json` ⇒ `mode live, state ARMED, halt none`
  - `sqlite3 data/live.db "select state,count(*) from orders group by 1"` ⇒ no `UNKNOWN` older than
    the last restart
  - `manage_bot.py halt` then a manual trade attempt ⇒ denied with the halt reason
- Expected: every order in the ledger has a matching order on the venue with the same
  `clientOrderId` prefix; equity delta ≈ fees only.

## Exit Criteria

- [ ] First live order reconciled to the venue by hand (qty, price, fee).
- [ ] A hand-made +50 USDT top-up raised the agent's cash and its order size with no restart, and a
      −50 USDT withdrawal halted instead of self-adjusting.
- [ ] 24h live with zero drift and no unexplained rejection.
- [ ] Halt verified against real trading.
- [ ] ADRs 0004–0007 merged; week-1 findings documented.
