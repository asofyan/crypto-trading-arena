# Phase 02: Venue-Accurate Paper Mode + Risk Gates

## Status: **done** (2026-11-25)

Delivered: `arena/instruments.py`, `arena/risk.py`, `arena/execution.py` venue path,
`trading.paper.instrument_rules` + `trading.instrument_cache` (+ schema), engine banner,
`uv run python -m arena.instruments --refresh`. 367 tests green, ruff clean.

## Objective

Make paper trading obey the real venue's instrument rules, and introduce the risk-gate policy that
live mode will reuse. Still no network call in the trading path.

## Preconditions

- Phase 1 merged (executor seam exists).

## Tasks

1. **Test first — instruments.** `tests/test_instruments.py`:
   - `SymbolRules.step_size=0.001, min_qty=0.001, min_notional=5` → `quantize(0.0015) == 0.001`
     (floor, never round up), `quantize(0.0009)` → `None` (below min qty).
   - a $2.50 notional buy is rejected with a message naming `min notional 5`.
   - unknown symbol → rejection naming the allowlist, not a crash.
2. **`arena/instruments.py`.** `SymbolRules(base_asset, quote_asset, step_size, min_qty,
   min_notional, tick_size)`, `parse_exchange_info(payload) -> dict[str, SymbolRules]`,
   `quantize_down(qty, rules)`, `check_notional(qty, price, rules)`.
   Decimal arithmetic (`decimal.Decimal`) on the step quantisation — floats lose 0.001 precision.
3. **Instrument source.** `InstrumentCache.load(path)` / `.save(path)` for
   `data/instruments-binance.json`. The engine tries: config path → cache file → built-in
   default (`"simple"`: 1 decimal, no min notional). Offline and tests hit the last one.
   Refresh with `uv run python -m arena.instruments --refresh --symbols SOLUSDT XRPUSDT LINKUSDT`
   (unauthenticated `GET /api/v3/exchangeInfo`; the `symbols` array must be JSON-encoded with
   **no spaces** or the venue answers 400 — verified against the live API).
4. **Test first — config.** `tests/test_config.py`:
   - `trading.mode` accepts `paper|live`, rejects anything else.
   - `trading.paper.instrument_rules` accepts `venue|simple` (default `venue`).
   - `trading.live` requires `accounts` + `agent_accounts` only when `mode == "live"`;
     `mode == "paper"` with no `live` block still loads.
   - percentages are sane (`0 < max_order_pct_of_equity <= max_position_pct_of_equity <= 100`, or a
     startup error); a typo that silently disables a cap is worse than refusing to start.
     `max_position_pct_of_equity` accepts `null` (gate off) but `max_order_pct_of_equity` does not.
5. **Config + schema.** Add the blocks to `config.py` (`LiveConfig`, `PaperConfig`) and mirror them
   in `config.schema.json` (there is a test asserting the committed schema matches the model —
   `tests/test_config.py:154`, do not leave it drifting).
6. **Test first — risk gates.** `tests/test_risk.py`:
   `RiskGates.check(symbol, side, qty, price, price_age_s, equity_usdt, position_usdt,
   session_start_equity) -> Allow | Deny(reason)` over a **minimal** set:
   - hard invariants: halt file, `ARMED` state, symbol allowlist, stale price
     (`price_age_s > live.max_price_age_seconds`), `max_orders_per_minute`, insufficient cash;
   - sizing caps as a share of `equity_usdt`: `max_order_pct_of_equity` (25),
     `max_position_pct_of_equity` (50, nullable), plus the optional absolute
     `max_order_notional_usdt` ceiling;
   - daily-loss halt as `max_daily_loss_pct` (10) of `session_start_equity` — a top-up raises the
     limit in step with the balance.
   Clock injected (`now: Callable`) so time-window tests are deterministic. Assert explicitly that a
   percentage gate **scales**: at equity 95 a 25 USDT order is denied, at equity 195 the same order
   is allowed. Sizing decisions stay with the agent — these are guardrails, not a position sizer.
7. **`arena/risk.py`.** Pure dataclasses, no I/O except one injected `is_halted() -> bool`.
   Deny reasons are **agent-facing strings** — the LLM must be able to fix its own next attempt
   ("max order notional 10 USDT; you asked for 24.30").
8. **Wire paper.** `PaperExecutor` quantizes with `SymbolRules` before booking and surfaces the
   same deny strings. `FeeModel` stays the paper fee source; add a one-line note in
   `arena/fees.py` that live uses the venue's actual commission instead.
   Update `arena/tools.py:execute_trade`'s docstring — it currently promises "1 decimal place"
   (`arena/tools.py:120`), which the LLM reads as a hard rule. It must instead state the symbol's
   `stepSize`/`minNotional` as reported at runtime, so the agent sizes correctly on the first try.
9. **Update the two tests that encode the old precision rule** (`tests/test_account_store.py`
   1-decimal rejection cases) to assert *venue* rules; keep one `instrument_rules="simple"` case
   proving the legacy rule still works when the cache is absent.

## Verification

- Commands:
  - `uv run pytest -q -m "not llm and not broker"`
  - `uv run ruff check .`
  - `uv run python -m deploy.tools_and_dashboard --bootstrap-servers localhost:9092 --headless`
    (log line shows `instrument rules: venue (SOLUSDT step 0.001, minNotional 5)` or the `simple`
    fallback, so the operator can see which regime is active)
- Expected: a paper buy of `0.1 LINK` is now rejected with the min-notional message; a paper buy of
  `0.036 SOL` succeeds and books `0.036` (not `0.0`).

## Exit Criteria

- [ ] Paper fills and rejections are indistinguishable from the venue's own rules.
- [ ] Risk gates are pure, unit-tested, and reachable from both executors.
- [ ] Config validates cap ordering and fails loud.
- [ ] Schema matches the model; suite + ruff green.
