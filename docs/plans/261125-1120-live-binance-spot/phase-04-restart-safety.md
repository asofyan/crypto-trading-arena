# Phase 04: Restart Safety — Reconciliation & Arming

## Objective

Guarantee the operator's requirement: *stop anytime, restart, resume from the last state based on
the last price.* After this phase the engine can be killed at any instant and, on restart, either
proves it is consistent and arms itself, or refuses to trade and says exactly what disagrees.

## Preconditions

- Phase 3 merged; ledger v2 in place.

## Tasks

1. **Go-live baseline.** `manage_bot.py live-init --fund 100 --agents agent-a,agent-b` (engine
   stopped; takes the writer lock): reads real balances, records
   `meta.live_baseline = {"USDT": <free>, "SOL": 0, …}`, seeds each agent's cash to its allocation,
   and refuses if `Σ allocations + reserve > free USDT`.
   The baseline is what keeps pre-existing holdings from being attributed to an agent (and stops
   the reconciler from reporting them as drift forever).
2. **Test first — reconcile.** `tests/test_reconcile.py`, fake venue + in-memory ledger:
   - clean state ⇒ `ARMED`;
   - ledger holds `0.003 SOL`, venue has `0.000` ⇒ `HALTED` with `shortfall SOL 0.003`;
   - venue has *more* than the ledger ⇒ `ARMED` + `surplus` note (dust, external deposit — must not
     block trading, and must never be silently adopted);
   - dangling `PENDING`/`UNKNOWN` order that the venue reports `FILLED` ⇒ settled via
     `settle_order`, then the balance comparison passes;
   - dangling order the venue reports `EXPIRED` ⇒ marked, no fill booked;
   - any open order on the account ⇒ `HALTED` (`unexpected_open_orders`), because v1 places none.
3. **`arena/reconcile.py`.** `reconcile(client, ledger, rules, baseline) -> Report`:
   ```
   settle dangling orders (PENDING|SUBMITTED|UNKNOWN) by clientOrderId
   re-read balances
   for symbol in allowlist: expected = Σ agent.positions[symbol]
                            actual   = free+locked[base] − baseline[base]
                            shortfall if actual < expected − step_size
   expected_cash = Σ agent.cash ; actual_quote = free+locked[USDT] − baseline[USDT]
                            shortfall if expected_cash > actual_quote + 1e-9
   open_orders ⇒ blocking
   ```
   Tolerance is one `step_size` per symbol (venue truncation), nothing looser.
3b. **Test first — equity sync (D10).** `tests/test_equity_sync.py`:
   - a 100 USDT surplus present in **two consecutive** reconcile samples ⇒ one `equity_events` row
     (`kind=deposit`), agent `cash` += 100, `get_portfolio` shows the new balance, still `ARMED`;
   - a surplus that appears once and vanishes (transient read) ⇒ nothing adopted;
   - surplus below `live.sync_tolerance_usdt` (default 1) ⇒ ignored, no row (dust);
   - shortfall ⇒ `HALTED`, no cash adjustment, deny message names
     `manage_bot.py reconcile --write-down <amount>`;
   - `--write-down 50` journals `kind=write_down` and re-arms only once the numbers agree.
   Implementation: `equity_sync(report, ledger, policy) -> Adoption | None` in `arena/reconcile.py`,
   `Ledger.record_equity_event(...)`, and `live.deposit_policy`
   (`credit_sole_agent` default | `proportional` | `unallocated`, where unallocated is visible in
   `status` but not spendable until `manage_bot.py allocate`).
4. **Arming state machine.** `meta.live_state ∈ {RECONCILING, ARMED, HALTED}` plus
   `meta.live_reconcile` (JSON report). The executor denies any order unless `ARMED` and the halt
   file is absent, with the deny reason naming the state and the first blocking diff.
5. **Startup sequence** (`deploy/tools_and_dashboard.py`), in this order, before serving tool nodes:
   ledger open → hydrate → mode block → (live only) `sync_time` → `settle dangling` →
   `reconcile` → set state → `worker.after_startup` re-checks state (the broker may not be up when
   the ledger was attached — mirror the existing `clear_degraded_marker_when_serving` pattern at
   `deploy/tools_and_dashboard.py:128-143`).
   **Auto-arm:** a clean reconcile with no halt file sets `ARMED` with no operator command — that is
   the "stop anytime, restart, continue" requirement. **Sticky halt:** if `data/LIVE_HALT` exists the
   state is `HALTED` even when the reconcile is clean, and startup never deletes that file; only
   `manage_bot.py resume` clears it.
   A failed reconcile must **not** abort startup: the dashboard, price feed and paper-style
   observation stay up, disarmed, with the diff visible.
   **Live balance refresher:** a background thread re-reads `GET /api/v3/account` every
   `live.balance_refresh_seconds` (default 30; weight 20 per call = ~40/min against a 6000/min
   budget) so a hand-made top-up is adopted mid-session, no restart. It writes the cached equity,
   never mutates the ledger — adoption stays in `equity_sync` under the store's write lock.
6. **Halt / resume.**
   - `manage_bot.py halt [--reason "…"]` ⇒ `touch data/LIVE_HALT` + note in `.run/`. Refuses silently
     doing nothing: prints the path and confirms the file exists.
   - `manage_bot.py resume` ⇒ engine must be stopped or the state must be `HALTED`; requires a clean
     `reconcile` (runs it under the writer lock), then removes the halt file and sets `ARMED`.
   - `RiskGates.is_halted()` checks the file (cheap `Path.exists`) on every order.
7. **Daily-loss halt.** When `realised + unrealised` drawdown since the session start breaches
   `live.max_daily_loss_pct` (10% of `session_start_equity`, recorded in `meta`), the executor writes
   the halt file itself with the numbers. Automatic, one-way: resuming is a human decision.
8. **Operator surface.** `manage_bot.py status` (`manage_bot.py:244-267` (`wallet_state`) / `:327-395` (`build_status`/`render_status`)): add
   `mode`, `live_state`, `account`, `venue_equity` vs `ledger_equity`, halt reason, and the first
   blocking diff. `--json` gains the same keys (do not break the existing schema — additive only).
   Dashboard header: `LIVE ARMED` / `LIVE HALTED` / `PAPER` badge next to the ledger-degraded badge.
9. **Crash drills** (`tests/test_crash_recovery.py`, fake venue, no network):
   - kill between journal `PENDING` and POST ⇒ restart settles `EXPIRED`-if-unknown, no fill;
   - kill after POST but before booking (venue filled) ⇒ restart books exactly one fill;
   - kill after booking ⇒ restart books nothing new;
   - kill between two agents' concurrent orders ⇒ both settle, no cross-agent mixing;
   - **operator halt survives a restart**: halt file present + clean reconcile ⇒ state stays
     `HALTED` and the order is denied, proving a restart cannot silently un-halt the bot.

## Verification

- Commands:
  - `uv run pytest -q -m "not llm and not broker"`
  - `uv run ruff check .`
  - manual: start the engine with a doctored ledger (`UPDATE positions SET quantity = 0.5`) and
    confirm it comes up `HALTED` with `shortfall SOL 0.5`, trades denied, dashboard showing the diff;
    then `manage_bot.py stop && manage_bot.py resume` and confirm `ARMED`.
- Expected: no path exists where live ordering is enabled while the ledger and the venue disagree.

## Exit Criteria

- [ ] Every crash point in the order lifecycle recovers with exactly one or zero fills (4 tests).
- [ ] Auto-arm on a clean start, sticky halt across restarts (both tested).
- [ ] A mid-session top-up credits the agent and lifts the percentage caps with no restart (tested).
- [ ] A shortfall never adjusts cash silently; `--write-down` is the only way to accept it.
- [ ] `HALTED` is visible in the dashboard, `status`, `status --json`, and the deny message.
- [ ] Resume requires a clean reconcile; halt is one command and one file.
- [ ] Baseline prevents pre-existing holdings from being reported as drift.
