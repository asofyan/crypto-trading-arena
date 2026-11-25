# Phase 01: Execution Seam

## Objective

Split "decide what filled" from "book the fill", so paper and live share one accounting path.
Behaviour, messages, tests and ledger rows stay **byte-identical** for paper.

## Preconditions

- `uv sync` done; `uv run pytest -q -m "not llm and not broker"` green on `main`.
- Read `arena/account_store.py:125-283` and `arena/tools.py:32-51` before touching anything.

## Tasks

1. **Test first.** In `tests/test_account_store.py`, add a test that calls the new
   `AccountStore.apply_fill(agent_id, action, product_id, quantity, price, fee, latency=None, ts=None)`
   directly and asserts the account state matches what `execute_trade` produces for the same inputs
   (cash, positions, cost_basis, avg_entry_ts, realized_pnl, trade_count, fees).
2. **Extract bookkeeping.** Move the mutate-and-record half of
   `AccountStore._execute_trade_locked` (`arena/account_store.py:180-270`) into `apply_fill`.
   Keep every rejection rule where it applies to both paths (`quantity > 0`, rounds to zero,
   insufficient holdings). Keep guards that only make sense pre-fill (cash, price availability) in
   the executors.
3. **Paper executor.** New `arena/execution.py`:
   - `FillRequest(agent_id, symbol, quantity, action)`
   - `FillOutcome(ok: bool, message: str, quantity: float | None, price: float | None, fee: float | None)`
   - `class Executor(Protocol)` with `execute(req: FillRequest) -> FillOutcome`
   - `PaperExecutor(store, price_book, fee_model)` reproducing today's logic exactly:
     price from `best_ask`/`best_bid`, fee from `FeeModel`, cash/holdings pre-checks, then
     `store.apply_fill(...)`.
4. **Wire the tool. [done differently]** Rather than have the tool know about executors, the seam
   sits one level lower: `AccountStore.execute_trade` builds a `FillRequest` and delegates to
   `self._executor`, which defaults to `PaperExecutor(self)`. `arena/tools.py` and
   `deploy/tools_and_dashboard.py` stay **byte-identical** — a live run later only calls
   `store.set_executor(...)`. `PaperExecutor` reads `store.price_book` and `store.fee_model` per
   call, so `set_fee_model` after construction keeps working. `_write_lock` became an `RLock` so an
   executor can hold it across "validate, then apply_fill".
5. **Delete the old path. [done]** `_execute_trade_locked` is gone; `_apply_fill_locked` holds the
   bookkeeping, `PaperExecutor` holds the pricing. `execute_trade` survives as the public entry
   point (all existing tests call it), delegating to the executor — one path, not two.
6. **Lint + full suite.**

## Verification

- Commands:
  - `uv run pytest -q -m "not llm and not broker"`
  - `uv run ruff check .`
- Expected:
  - Every existing `tests/test_account_store.py`, `tests/test_tools.py`, `tests/test_ledger.py`
    assertion passes unchanged (identifiers may move, semantics must not).
  - `python manage_bot.py start tui` runs a paper session with the same dashboard, same fills.

## Exit Criteria

- [ ] One and only one place computes paper fill price/fee (`PaperExecutor`).
- [ ] One and only one place mutates account balances (`AccountStore.apply_fill`).
- [ ] No behaviour change: identical tool messages, identical `fills` rows, identical P&L.
- [ ] Suite + ruff green; no new dependency.
