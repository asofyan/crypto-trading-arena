# Phase 05: Binance Spot Testnet End-to-End

## Objective

Prove the whole loop against a real exchange — real signing, real order lifecycle, real rejects,
real restart behaviour — with zero money at risk. This is the gate before any live USDT.

## Preconditions

- Phases 1–4 merged.
- Testnet keys from `https://testnet.binance.vision` in `.env` as
  `BINANCE_TESTNET_API_KEY` / `BINANCE_TESTNET_API_SECRET`; testnet account funded with its own
  faucet USDT.
- `live.accounts.main.testnet = true` (base URL `https://testnet.binance.vision`) — the **same**
  code path, only the host differs.

## Tasks

1. **Opt-in marker.** Register `binance_testnet` in `pyproject.toml` `markers` alongside `llm` and
   `broker`, skipped unless `--run-binance-testnet` (mirror `tests/conftest.py:84`). CI stays
   hermetic.
2. **Testnet smoke test** (`tests/test_binance_testnet.py`): `sync_time` → `balances` →
   `exchange_info(SOLUSDT)` → market buy `quoteOrderQty=6` (above the 5 USDT min notional) →
   assert `FILLED`, `executedQty > 0`, one commission entry → market sell the quantised net base →
   assert the balance returns to within a step size. Prints the real fee in bps so the operator can
   sanity-check `trading.fees.taker_bps`.
3. **Reject paths on a real venue.** Same test file, asserts the client surfaces actionable errors
   for: notional below 5 USDT (`-1013`), quantity below step size, unknown symbol, and a duplicate
   `clientOrderId` (venue-side rejection proves the id is really stable).
4. **Full-engine soak.** Run the real stack against testnet for ≥ 2 hours with **one** agent:
   connector (`uv run python -m exchanges.binance`), engine (`mode=live`, `dry_run=false`), agent.
   Watch for: rejected-order ratio, `UNKNOWN` order count (must be 0), reconcile drift (must be 0),
   orders/minute vs the token bucket, and the dashboard equity vs testnet's own balance.
5. **Restart drills on the real venue** — the operator's core requirement, executed manually:   - `manage_bot.py stop` mid-session (clean) ⇒ restart ⇒ `ARMED`, positions identical;
   - `kill -9` immediately after a trade appears in the log ⇒ restart ⇒ exactly one fill for that
     intent, no duplicate, reconcile clean;
   - `kill -9` *before* the first tick arrives ⇒ restart ⇒ `ARMED` only after a price is seen,
     trades denied with "no live price" until then (existing behaviour, re-verified);
   - disconnect the network mid-order ⇒ journal `UNKNOWN` ⇒ restart resolves against the venue.
   Record timings in `research/testnet-drill-log.md`.
6. **Bounds check.** Confirm the caps actually bite on the venue, in both shapes: with
   `max_order_pct_of_equity = 5` on a 200 USDT testnet balance, a 15 USDT buy must be denied by us,
   not by Binance; then top up the testnet balance by 100 USDT and confirm the same 15 USDT order is
   now allowed (~7.5% of 300) after `live.balance_refresh_seconds` — proving the caps scale with the
   balance instead of being frozen at a dollar amount.

## Verification

- Commands:
  - `uv run pytest -q -m "not llm and not broker and not binance_testnet"` (CI path, unchanged)
  - `uv run pytest -q -m binance_testnet --run-binance-testnet`
  - manual soak + drills as above
- Expected:
  - Smoke test green; fee printed matches the account's real tier.
  - Soak: 0 `UNKNOWN` orders, 0 drift, ≥ 100 orders exercised, no 429/418.
  - Every drill recovers to `ARMED` with the ledger matching the venue.

## Exit Criteria

- [ ] Testnet smoke + reject tests green and opt-in.
- [ ] 2-hour soak with a real agent, no drift, no unknown orders, no rate-limit incident.
- [ ] All four restart drills pass on the real venue, logged.
- [ ] CI remains network-free and green.
