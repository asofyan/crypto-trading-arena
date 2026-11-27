# crypto trading arena — working guide

Canonical guide for every coding assistant (Claude Code, Codex, pi, …) and for a human
picking this up cold. Read this before touching the code. The last section is the state of
the live paper session and the open backlog.

---

## 1. Development rules

### Dependencies
- Managed with **`uv`**. Do not hand-edit `pyproject.toml` — use `uv add <pkg>` so `uv.lock`
  stays canonical.
- Run everything through `uv run` (tests, modules, scripts).

### Architecture
Event-driven agents that consume data streams through **Kafka**. Each agent is safe to
deploy on its own host and decides on realtime data. One Python process per role:
connector (venue → topics), tool server (tools + dashboard + wallet), agent (LLM turns).

### Conventions
- Commits/PRs landing on `main` use conventional-commit prefixes: `feat:`, `fix:`,
  `chore:`, `docs:`, `refactor:`, `test:`, `perf:`, `style:`. PR titles the same.
- Ruff clean for new/changed files (`uv run ruff check arena/ deploy/ tests/`). The repo is
  **not** `ruff format` clean — do not reformat whole files.
- Comments and docstrings explain *why*, not *what*.

### Sub-agents
- Large planned work: split and parallelise across sub-agents.
- Spawn them with the opus model and xhigh thinking effort.
- Spawn generously for review, investigation, and intel gathering — wide and deep fan-out.

### Test driven development
- Follow `/test-driven-development`: failing test first, watch it fail for the right reason,
  then implement.
- Use `/pytest-coverage` to check completeness.

### Deep implementation reviews
- Use `/pr-review-toolkit:review-pr` for functional bugs, anti-patterns, test coverage,
  documentation correctness.
- Use `/simplify` for design/implementation simplification.
- Multiple review rounds are not done until findings converge to **no critical or must-fix
  issues**. (This session used three adversarial rounds; round 2 and 3 found real bugs that
  the author had missed, including a gate that a test did not actually exercise.)

### calfkit agents SDK
- The project dogfoods calfkit (`https://github.com/calf-ai/calfkit-sdk`).
- If calfkit genuinely lacks a seam and you would have to reach into its internals or build a
  hacky workaround, file an issue there describing what you were building and why the API
  surface was insufficient.
- Verifiable bugs also get an issue, with reproduction.

### Design discussions
- `/grill-with-docs` when planning/designing.
- `/brainstorming` in addition, for open-ended feature requests without a clear solution.

### Documentation
- Use `/diataxis-docs-writer` to write documentation.

### PR final steps
- Before merge, decide whether any decision qualifies for an ADR. Format and criteria:
  `.agents/skills/grill-with-docs/ADR-FORMAT.md`. ADRs live in `docs/adr/NNNN-slug.md`.

---

## 2. Running the stack

Three detached processes. `manage_bot.py` does not see processes started with `nohup`
(there is no pidfile), so `manage_bot.py status` prints **"Bot: not running"** even when the
agent is happily trading. Check with `ps`, not with `status`.

```bash
# 1. venue → kafka (leave running; it is the input source)
nohup uv run python -m exchanges.binance_perp --min-interval 60 \
  >> .run/perp-connector.log 2>&1 &

# 2. tool server: tools + dashboard + wallet owner + ledger (start this FIRST)
nohup uv run python -m deploy.tools_and_dashboard \
  --bootstrap-servers localhost:9092 --snapshot-interval 60 --headless \
  >> .run/bot.log 2>&1 &

# 3. the agent (only after the tool server is serving its topics)
nohup uv run python -m deploy.agent --from-config scalper-perp \
  --bootstrap-servers localhost:9092 >> .run/agent-scalper-perp.log 2>&1 &
```

- **Restart order matters.** The agent is told about the tool set at startup and calls it
  over Kafka. A tools process older than the agent answers `unknown tool`, which the model
  cannot recover from. Restart tools first, then the agent.
- `manage_bot.py start|stop` is the managed path when it *owns* the processes. `--fresh`
  archives the wallet and resets every agent to $100 — never use it casually.
- Kafka is a Docker container: `local-broker` (`confluentinc/cp-kafka:8.0.0`), `localhost:9092`.
  All admin uses `docker exec local-broker kafka-…`.
- `data/LIVE_HALT` is set (`operator halt`) → live order placement is blocked. Paper trading
  is unaffected. Live is not in use (`trading.mode` = paper; perps are paper-only).

### The trap that cost this session an hour: orphaned tool returns

`<agent>.private.return` carries tool results back to the agent. If the agent is killed
mid-turn, those uncommitted returns stay on the topic. On restart each orphan **re-enters as
a fresh turn**, that turn calls a tool, the tool publishes another return — a self-sustaining
loop that starves market messages (`agent_router.input.lean` lag grows, zero ticks logged,
while `private.return` keeps churning). Seen after the first restart: agent consumed nothing
from 11:10:32 onward with 37 pending returns.

Fix, verified:

```bash
kill -INT <agent pid>            # graceful; wait for "FastStream app shut down gracefully"
docker exec local-broker kafka-consumer-groups --bootstrap-server localhost:9092 \
  --group scalper-perp --reset-offsets --to-latest \
  --topic scalper-perp.private.return --execute
docker exec local-broker kafka-consumer-groups --bootstrap-server localhost:9092 \
  --group scalper-perp --reset-offsets --to-latest \
  --topic agent_router.input.lean --execute
# then start the agent again
```

Skipping messages is correct here: market messages are stale by minutes, and replayed tool
returns are not idempotent work.

---

## 3. Reading the system's state

| What | Where |
|---|---|
| Agent turns, LLM cooldowns/429s | `.run/agent-scalper-perp.log` |
| Tool calls, gate refusals, fills, wallet | `.run/bot.log` |
| Venue REST polling, published batches | `.run/perp-connector.log` |
| Wallet truth (survives restarts) | `data/arena.db` → `accounts`, `positions`, `fills` |
| Per-tick equity snapshots | `data/snapshots_<session>.csv` (new file each tool-server start) |
| Per-tick prices | `data/ticks_<session>.csv` |

```bash
uv run python manage_bot.py status            # wallet + configured agents (blind to nohup procs)
sqlite3 -header -column data/arena.db \
  "select agent_id, round(cash,2) cash, trade_count, round(realized_pnl,2) realized from accounts;"
sqlite3 -header -column data/arena.db "select product_id, quantity from positions;"
tail -1 data/snapshots_*.csv | tail -1     # cash, positions, portfolio_value
docker exec local-broker kafka-consumer-groups --bootstrap-server localhost:9092 \
  --describe --group scalper-perp           # LAG is the health number
```

**Healthy looks like:** `agent_router.input.lean` LAG 0–1 and a Received→Processed pair
roughly every 50 s; tick latency median ~2 s with the current pool (it was 23 s median,
191 s worst, when gemma was in the pool); no `is unavailable` lines; gate refusals only when
the agent actually asks for something disallowed.

**Diagnosing a stall:** `ps -o pid,stat,%cpu -p <pid>`; if the log has no new
`agent_router.input.lean` line but `private.return` keeps advancing, it is the orphan loop
above, not the model.

---

## 4. Money-path invariants — do not "fix" these

ADR: `docs/adr/0011-the-agent-names-an-exit-the-server-sizes-it.md` (supersedes the flipping
half of ADR-0008 for the agent path).

An LLM's position size is a number it read on an **earlier turn**. calfkit's state is an
append-only `message_history`, so an old portfolio table stays in context and competes with
the fresh one. On 2026-11-27 the model asked to sell `0.62` while holding `0.01`; that order
would have opened a `0.61` short. Only the churn guard stopped it, by luck.

Four rules, all enforced server-side, each with tests:

1. **Exits carry no size.** `close_position(product_id)` — `arena/tools.py`, backed by
   `AccountStore.close_position`, which re-reads the book inside the booking lock. A model
   asked for the product, never the quantity.
2. **`execute_trade` never crosses zero for an agent** (`allow_flip=False`,
   `arena/account_store.py::_flip_rejection`). Overshoot is refused with the live size; an
   overshoot whose opening leg is under the venue minimum is booked as the close it mostly
   was (`clamp_flip_to_a_close`). Reversal = two orders.
3. **A perp order that adds exposure needs a book the agent has seen since the last fill**
   (`_stale_read_rejection`; the stamp is `trade_count`, written by `get_portfolio` via
   `record_portfolio_read`, only after the render succeeds). This is the one that closes the
   flat-book case: "sell the size I read" and "open that size short" are the same order when
   the position went to zero in between.
4. **The prompt tells the truth about memory.** Never reintroduce
   *"you do not remember earlier turns"* — it is false, and believing it is what removed the
   agent's reason to distrust an old number. `tests/test_strategies.py` asserts the claim is
   gone from every strategy.

Gate order in `_preadmission_rejection`: **stale read → flip → churn → exit floor.**
- Churn (`arena/trade_policy.py`): 6 fills/hour, 60 s per-product re-entry, 0.25 %/hour fee
  burn. Never refuses a reduce.
- Exit floor: +100 bps target, −50 bps stop, 3600 s hold escape. Closing inside the band is
  refused, so a reversal inside the band is impossible — the prompt says so.

---

## 5. Model pool

`config.json` → `agents[scalper-perp].model` (round-robin, 900 s rest on HTTP 429, 30 s on
anything else — `arena/model_pool.py`):

```
nvidia/nemotron-3-ultra-550b-a55b:free
baidu/ernie-5.5-flash-preview:free
nvidia/nemotron-3.5-lightning:free
openai/gpt-oss-20b
```

Ruled out by direct probe (3 requests each, real tool schema): `google/gemma-4-31b-it:free`
0/3 — 429 from Google AI Studio's shared pool every time; `inclusionai/ling-3.2-flash:free`
0/3 — 429; `particle-ai/particle-1:free` 2/3; `liquid/lfm-3-1.7b:free` 1/3 (returns
`finish=length` with no tool call); `nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free`
2/3 (one call arrived with `args={}`). A model that cannot emit a correct tool call is
useless here no matter how good its prose is — the agent's only action is a tool call.

Observed latency: `gpt-oss-20b` ~0.8 s, `nemotron-3.5-lightning` ~1.0 s,
`ernie-5.5-flash-preview` ~1.5–2.0 s, `nemotron-3-ultra` ~1.5–3.9 s,
`recursive/flint-v1.0:free` 4–10 s (works, slow).

Re-probe before changing the pool — a model that answered yesterday can be throttled today:

```bash
curl -s https://openrouter.ai/api/v1/models \
  | python3 -c "import json,sys; d=json.load(sys.stdin)['data']; \
print('\n'.join(f\"{m['id']:55s} tools={'tools' in (m.get('supported_parameters') or [])}\" \
for m in d if m['id'].endswith(':free')))"
# then POST /chat/completions with a one-tool schema and assert the tool_calls array
```

**`config.json` is gitignored** (`.gitignore:224`) — pool changes are local only.
`config.example.json` is the tracked template and holds placeholder models.

---

## 6. Config facts

- Perp products: `ZECUSDT-PERP`, `1000PEPEUSDT-PERP`, `SNDKUSDT-PERP`; spot:
  `SOLUSDT`, `XRPUSDT`, `LINKUSDT`; venue rules cached in
  `data/instruments-binance-perp.json` (a symbol absent there falls back to the legacy
  1-decimal paper rule, which silently rejects `0.62`-style sizes — mind it in tests).
- Fees: spot taker 10 bps, perp taker 5 bps. Round trip on a perp = 10 bps, which is why the
  prompt's cost floor is not negotiable.
- `trading.churn` is absent from `config.json`, so the defaults above apply.
- Agent payload is `lean` (`agent_router.input.lean`): intraday candles + a trend line,
  ~2.4k tokens instead of ~20k. ADR-0010.

---

## 7. Backlog (not done)

Ordered by how much it hurts:

1. **Orphan tool-return loop has no code-level defence.** Today the cure is the manual
   offset reset in §2. Real fix: dedupe/reject a return whose turn already ended, or bound
   the tool-call rounds per turn, or make a restart script reset the two topics.
2. **`message_history` is unbounded** — every turn re-sends the whole session, which is both
   the stale-context root cause and the latency slope. calfkit exposes no trim seam:
   <https://github.com/calf-ai/calfkit-sdk/issues/378>.
3. **Gate → booking window.** Gates decide under `_write_lock`; booking happens in the next
   acquisition. Harmless at `max_workers: 1` on paper; carry the guard into `FillRequest` and
   re-check in `apply_fill` before anyone raises concurrency.
4. **Exit band is duplicated.** The prompt hardcodes +100/−50/60 min while `ExitFloor` is
   config-driven. Template the band into the prompt from config.
5. **`close_position` refusals are invisible in logs.** "Nothing to close" and exit-floor
   refusals never hit `bot.log` at INFO, which makes ops debugging blind. Log them like the
   other gates.
6. **`momentum`, `brainrot`, `scalper` prompts never tell the agent to call `get_portfolio`.**
   Harmless today (they are spot-only, and the read gate is perp-only), but the moment one of
   them gets `markets: ["perp"]` it cannot add exposure at all.
7. **`manage_bot.py status`/`stop` cannot see nohup-started processes** — either always start
   through `manage_bot.py`, or fall back to discovering the processes by command line.
8. **The read-only monitor (`deploy/portfolio_monitor.py`) is not in any health check.** It
   reads the ledger and never publishes, so a broken monitor is silent; it was only proved by
   `--once` in a test. Consider a smoke run in CI (`--once` against a fixture ledger).

---

## 8. State at handoff — 2026-11-27 ~11:25 WIB

Paper only. `LIVE_HALT` set; no real orders.

- Processes: tools server `66766`, agent `79839`, connector `63544`.
- Agent is on the **new** code and the **new** pool (banner confirms all four tools incl.
  `close_position`, and ernie in place of gemma). After the offset reset it resumed trading:
  50 fills, cash $43.09, SNDK +0.25 and ZEC +0.05, portfolio ≈ $101.10 at 11:20.
- Verification of the post-reset restart was cut short (by request). First thing next
  session: confirm `agent_router.input.lean` LAG is 0–1 with a tick every ~50 s, and that
  `close_position` appears in `bot.log` when the agent exits a position.
- Suite: **812 passed, 10 skipped, 3 xfailed**; `ruff check arena/ deploy/ tests/` clean.
- Landed on `main` (pushed to `fork`):
  - `dd62237 fix: never let an agent size an exit from a stale read` (+ ADR-0011)
  - `566ee4d docs: consolidate the working guide into AGENTS.md`
  - `a9cb99c refactor: read the monitor's wallets from the ledger, not a CSV replay`
- The working tree is clean. `.harness/` (gitignored) holds the plan/report for the fix;
  `.harness.prev-guard-experiment/` was deleted.
- The monitor now reads `data/arena.db` via `Ledger.read_portfolio`; CSV replay is behind
  `--no-ledger`. `Ledger`'s lock is an `RLock` for that reason — do not "simplify" it back.
