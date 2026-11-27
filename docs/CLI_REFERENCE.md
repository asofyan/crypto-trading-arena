# CLI Reference

## Configuration File

The trading arena supports a JSON configuration file (`config.json` by default) for managing:
- Multiple LLM providers (OpenAI, OpenRouter)
- Multiple agents with different providers/models/strategies
- Exchange selection and trading pairs for Binance and Coinbase
- The taker fee charged on every simulated fill (`trading.fees.taker_bps`)

To get started, copy the example and fill in your API keys:
```bash
cp config.example.json config.json
```

> **Note:** `config.json` is gitignored to prevent accidental secret commits.

For the full config schema with all fields, types, and defaults, see [`config.schema.json`](../config.schema.json). IDEs that support JSON Schema (VS Code, JetBrains) will provide autocompletion and validation automatically via the `$schema` reference in the config file.

**Example config.json:**
```json
{
  "$schema": "./config.schema.json",
  "llm_providers": {
    "openai": {
      "api_key": "${OPENAI_API_KEY}",
      "base_url": "https://api.openai.com/v1",
      "default_model": "gpt-5-nano"
    },
    "openrouter": {
      "api_key": "${OPENROUTER_API_KEY}",
      "base_url": "https://openrouter.ai/api/v1",
      "default_model": "anthropic/claude-sonnet-4"
    }
  },
  "agents": [
    {
      "name": "gpt-5-nano",
      "provider": "openai",
      "model": "gpt-5-nano",
      "max_workers": 1,
      "strategy": "default"
    },
    {
      "name": "claude",
      "provider": "openrouter",
      "model": "anthropic/claude-sonnet-4",
      "max_workers": 1,
      "strategy": "default"
    }
  ],
  "trading": {
    "exchange": "coinbase",
    "binance_symbols": ["SOLUSDT", "XRPUSDT", "LINKUSDT"],
    "coinbase_products": ["BTC-USD", "SOL-USD", "FARTCOIN-USD"],
    "fees": { "taker_bps": 60 }
  }
}
```

> **Note:** The Anthropic API is not OpenAI-compatible. To use Claude models, configure them via the `openrouter` provider or another OpenAI-compatible proxy.

**Model pools (rate limits):** `model` takes either one ID or a list of IDs. A list
turns the agent's model client into a round-robin pool (`arena/model_pool.py`): each
inference goes to the next model in rotation, and a model that fails transiently
(`429`, `403`/`404` for a model id this key cannot use, `408`/`409`, any 5xx, a
connection failure) rests for the cooldown while the others keep trading. Rate limits
are charged per model, so two models roughly double one agent's headroom:

```json
{ "name": "hy4-pool", "provider": "opencode-go",
  "model": ["hy4-flash-free", "deepseek-v4.1-flash"] }
```

Every model in a pool answers the same strategy prompt with the same tools, so the
agent cannot tell which one replied. A request-shaped rejection (a `400` from a
malformed request) is *not* retried on another model — that is a bug to fix, not a
quota to dodge. Pooled clients also run with the OpenAI SDK's own retries off, so the
pool pays for failover instead of sleeping on `Retry-After`.

On the command line, repeat `--model-id` to build a pool; an explicit `--model-id`
overrides the config's `model` and prints a note (same rule as `--strategy`).
See [ADR-0009](adr/0009-round-robin-model-pool.md).

**Market-message profiles:** the connector publishes the market message twice — once
complete on `agent_router.input`, once lean on `agent_router.input.lean`. `lean` keeps
the three intraday candle tiers (15m/5m/1m, tiling the last three hours) plus a one-line
per-product trend summary (4h EMA20 distance, 4h change, last daily change, monthly-range
position). Measured: 20,633 → 2,399 tokens per tick, an 88% cut. Set it per agent:

```json
{ "name": "scalper-perp", "payload": "lean", "strategy": "scalper-perp" }
```

`full` remains the default, so swing-trend agents are unaffected. A minutes-horizon
strategy reads the intraday tiers; the daily/4h tiers are 89% of the candle rows and
exist for swing horizons.

**API Key Formats:**
- Environment variable: `"${OPENAI_API_KEY}"` - Reads from env var at runtime
- Embedded key: `"sk-..."` - Key embedded directly in config (less secure)

**Extra headers:** some providers reject requests that arrive without a routing header
rather than an OpenAI one. `extra_headers` sends fixed headers with every inference call:

```json
"opencode-go": {
  "api_key": "${OPENCODE_GO_API_KEY}",
  "base_url": "https://opencode.ai/inference/go/openai/v1",
  "extra_headers": { "x-opencode-session": "crypto-trading-arena" }
}
```

opencode-go returns `400 MissingSessionID` without `x-opencode-session`; any stable
value works. `${ENV_VAR}` references are resolved inside `extra_headers` too.

---

## deploy/agent.py

Deploy an agent with an embedded model client and trading strategy. Can use explicit CLI args or load from config.

| Flag | Required | Default | Description |
|------|----------|---------|-------------|
| `--name` | Yes* | — | Agent name (consumer group + identity) |
| `--model-id` | Yes* | — | Model ID (e.g. `gpt-5-nano`, `deepseek-chat`). Repeatable: each flag adds a model to the pool the agent round-robins across |
| `--model-cooldown-seconds` | No | `30` | How long a server error (5xx/transport) sits a model out; a `429` rests it for `--model-rate-limit-cooldown-seconds` |
| `--payload` | No | `full` | Market-message profile: `full` (every candle tier) or `lean` (intraday tables + trend line, ~88% fewer tokens). Defaults to the config's `payload` |
| `--strategy` | Yes | — | Trading strategy: `default`, `momentum`, `brainrot`, or `scalper` |
| `--bootstrap-servers` | Yes | — | Kafka broker address |
| `--base-url` | No | OpenAI | Base URL for OpenAI-compatible providers |
| `--api-key` | No | `$OPENAI_API_KEY` | API key for the provider |
| `--reasoning-effort` | No | `None` | For reasoning models (e.g. `"low"`) |
| `--extra-header` | No | — | Extra HTTP header `KEY=VALUE` sent with inference requests; repeatable, merged over the provider's `extra_headers` |
| `--from-config` | No | — | Load agent config by name from config file |
| `--config-path` | No | `config.json` | Path to config file |

\* Required unless using `--from-config`

**Examples:**
```bash
# Explicit configuration
uv run python -m deploy.agent \
    --name momentum --model-id gpt-5-nano \
    --strategy momentum --bootstrap-servers localhost:9092 \
    --api-key $OPENAI_API_KEY

# Load from config file
uv run python -m deploy.agent \
    --from-config gpt-5-nano --strategy default \
    --bootstrap-servers localhost:9092

# Using OpenRouter (for Claude and other non-OpenAI models)
uv run python -m deploy.agent \
    --name claude-agent --model-id anthropic/claude-sonnet-4 \
    --base-url https://openrouter.ai/api/v1 \
    --api-key $OPENROUTER_API_KEY \
    --strategy default --bootstrap-servers localhost:9092

# One agent, several models (rate-limit headroom)
uv run python -m deploy.agent \
    --name momentum-pool --model-id hy4-flash-free --model-id deepseek-v4.1-flash \
    --base-url https://opencode.ai/inference/go/openai/v1 \
    --api-key $OPENCODE_GO_API_KEY \
    --strategy momentum --bootstrap-servers localhost:9092
```

---

## exchanges/binance.py

Stream real-time market data from Binance to Kafka.

| Flag | Required | Default | Description |
|------|----------|---------|-------------|
| `--bootstrap-servers` | No | `localhost:9092` | Kafka broker address |
| `--config` | No | `config.json` | Path to config file for symbols |
| `--symbols` | No | From config | Binance symbols to subscribe (overrides config) |
| `--min-interval` | No | `60` | Minimum seconds between publishes |
| `--log-level` | No | `INFO` | Logging level: `DEBUG`, `INFO`, `WARNING`, `ERROR` |

**Examples:**
```bash
# Use symbols from config file
uv run python -m exchanges.binance --bootstrap-servers localhost:9092

# Override with specific symbols
uv run python -m exchanges.binance \
    --bootstrap-servers localhost:9092 \
    --symbols SOLUSDT XRPUSDT LINKUSDT
```

---

## exchanges/binance_perp.py

Stream Binance **USDⓈ-M perpetual futures** (`fstream` + `fapi`) to Kafka. Same flags as
`exchanges.binance.py` except the defaults below, because it is the same connector bound to a
different venue.

| Flag | Required | Default | Description |
|------|----------|---------|-------------|
| `--bootstrap-servers` | No | `localhost:9092` | Kafka broker address |
| `--config` | No | `config.json` | Path to config file for symbols |
| `--symbols` | No | From `trading.binance_perp_symbols` | Venue symbols to subscribe (overrides config) |
| `--min-interval` | No | `60` | Minimum seconds between publishes |
| `--log-level` | No | `INFO` | Logging level: `DEBUG`, `INFO`, `WARNING`, `ERROR` |

Products are published as `<SYMBOL>-PERP` (`SOLUSDT-PERP`), so a perp never collides with the
spot product of the same name. Bid/ask come from `/fapi/v1/ticker/bookTicker` because the futures
24h ticker carries no touch. The taker fee advertised to agents is `trading.fees.perp_taker_bps`.

**Examples:**
```bash
uv run python -m exchanges.binance_perp --bootstrap-servers localhost:9092

# Refresh the contract rules the paper engine snaps quantities to
uv run python -m arena.instruments --perp
```

---

## exchanges/coinbase.py

Stream real-time market data from Coinbase to Kafka.

| Flag | Required | Default | Description |
|------|----------|---------|-------------|
| `--bootstrap-servers` | No | `localhost:9092` | Kafka broker address |
| `--config` | No | `config.json` | Path to config file for products |
| `--products` | No | From config | Coinbase products to subscribe (overrides config) |
| `--min-interval` | No | `60` | Minimum seconds between publishes |
| `--log-level` | No | `INFO` | Logging level: `DEBUG`, `INFO`, `WARNING`, `ERROR` |

**Examples:**
```bash
# Use products from config file
uv run python -m exchanges.coinbase --bootstrap-servers localhost:9092

# Override with specific products
uv run python -m exchanges.coinbase \
    --bootstrap-servers localhost:9092 \
    --products BTC-USD ETH-USD SOL-USD
```

---

## deploy/tools_and_dashboard.py

Deploy trading tools, the price-feed subscriber, and the live dashboard.

| Flag | Required | Default | Description |
|------|----------|---------|-------------|
| `--bootstrap-servers` | Yes | — | Kafka broker address |
| `--config` | No | `config.json` | Path to config file |
| `--snapshot-interval` | No | `600` | Seconds between portfolio snapshots (`0` disables CSV recording) |
| `--data-dir` | No | `./data` | Output directory for CSV data files |
| `--db` | No | `data/arena.db` | SQLite wallet ledger path |
| `--no-ledger` | No | off | Disable wallet persistence (volatile wallets) |
| `--headless` | No | off | Run without the Rich Live dashboard (logs only) |

At startup the wallet ledger (`--db`) is replayed into the shared `AccountStore`, so a
restart resumes cash, positions, and counters instead of resetting every agent to
`INITIAL_CASH`. The entrypoint prints the hydrated agents and their cash (or
`Fresh ledger …` on first run). Every executed fill is written to the ledger in one
transaction; rejected trades never touch it. `--no-ledger` restores the old volatile
behaviour and prints a warning. `--headless` is what `manage_bot.py start backend` uses:
with no `Live` attached, `view.rerender()` is a no-op, so price callbacks stay cheap.

**Durability:** every executed fill commits in a single transaction — the account row,
its positions, and the fill land together — with `journal_mode=WAL` and
`synchronous=NORMAL`. A *process* crash is safe: the WAL is replayed on the next open.
An OS/machine crash can still lose the last committed fill(s) still sitting in the WAL;
that trade is deliberate, since fsync-per-fill would put disk latency on the trading
hot path. If a ledger write fails mid-session, the fill still counts in memory and the
engine keeps running — `AccountStore.ledger_degraded` flips to `true` so the divergence
between memory and disk is visible instead of breaking the agent mid-trade. You can see
it in three places: a red `LEDGER DEGRADED` badge in the dashboard header,
`WARNING: persistence degraded at <ts> (<agent>)` from `manage_bot.py status`, and the
`wallet.degraded` field of `status --json`. The marker records when the divergence
*started* — the first failed fill; later failures do not move it, because every fill
from that instant on is missing — and is also written durably to the ledger's `meta`
table so it outlives the process. The next *successful* startup clears it: not when the
ledger is attached (a bootstrap that dies before serving must keep the warning), but
when the worker's `after_startup` hook runs with the broker up.

**Caveat — the marker cannot outlive an unwritable database.** The marker is written
through the same connection as the fills, so a failure that leaves the database itself
unwritable (read-only mount, `ENOSPC`, corruption) blocks the marker too. The durable
`meta` row then never lands: the red dashboard badge and the engine log are the only
signals, and `manage_bot.py status` from another process will not warn.

The taker fee is configured by `trading.fees.taker_bps` in `config.json` — the
single source of truth read by both this tools node and the price-feed connector,
so the fee charged on fills always matches the fee the connector advertises to
agents. The fee applies to both buys and sells (buys pay notional + fee, sells
receive notional − fee, and buy fees capitalize into cost basis).

> **Changing the fee:** both processes read `taker_bps` once at startup, so after
> editing `config.json` you must restart **both** the exchange connector and this
> tools-and-dashboard process. Restarting only one leaves the charged fee and the
> fee advertised to agents out of sync.

Realistic values:

| Exchange | Suggested `taker_bps` |
|---|---|
| Coinbase Advanced Trade base tier | `60` (default) |
| Binance global (VIP 0) | `10` |
| Binance.US | `60` |
| Kraken Pro | `40` |
| Disable fees | `0` |

**Examples:**
```bash
# Uses trading.fees.taker_bps from config.json (default 60 bps)
uv run python -m deploy.tools_and_dashboard --bootstrap-servers localhost:9092

# Point at an alternate config (e.g. one with taker_bps: 10 for Binance VIP 0)
uv run python -m deploy.tools_and_dashboard \
    --bootstrap-servers localhost:9092 --config config.binance.json
```

---

## manage_bot.py

Operator wrapper around `deploy/tools_and_dashboard`. It never touches Kafka topics or
consumer groups itself — it only spawns, signals, and inspects the engine process.

| Subcommand | Purpose |
|------------|---------|
| `start [backend\|tui]` | `backend` (default) spawns a detached worker logging to `.run/bot.log`; `tui` runs the same worker in the foreground with the Rich dashboard (Ctrl-C to stop) |
| `stop` | SIGINT (graceful: recorder + ledger flush) → SIGTERM → SIGKILL only with `--force` |
| `status` | Process state (pid, mode, uptime, log) plus per-agent wallet rows read from the ledger |

**Flags:**

| Subcommand | Flag | Default | Description |
|------------|------|---------|-------------|
| `start` | `[backend\|tui]` | `backend` | `backend`: detached, logs to `.run/bot.log`; `tui`: foreground dashboard |
| `start` | `--fresh` | off | Archive the existing wallet DB first, so every agent restarts at `$100`. No-op when `--no-ledger` is also set (see below) |
| `start` | `--bootstrap-servers` | `localhost:9092` | Kafka bootstrap servers |
| `start` | `--config` | `config.json` | Path to config file |
| `start` | `--data-dir` | `./data` | Directory for CSV snapshots/trades |
| `start` | `--db` | `data/arena.db` | SQLite wallet ledger path |
| `start` | `--no-ledger` | off | Run without wallet persistence (volatile wallets) |
| `stop` | `--force` | off | SIGKILL if SIGINT and SIGTERM do not stop it |
| `status` | `--db` | the pid file's recorded `--db`, else `data/arena.db` | Ledger to read wallet rows from |
| `status` | `--json` | off | Emit machine-readable JSON |

**Pid file & log:** the pid file `.run/bot.pid` holds
`{pid, mode, started_at, cmd, log, no_ledger}` (written atomically). `start` refuses to
run while that pid is still alive. `start backend` appends to `.run/bot.log` (each run
prefixed with `=== start <iso-ts> ===`); `start tui` writes to the terminal. `stop`
signals only a pid whose `ps` command line mentions `deploy.tools_and_dashboard` —
anything else is treated as a stale pid file and removed without a signal. A pid file
that cannot be parsed is never deleted blindly: `stop` salvages the pid from the raw
text and removes the file only when that pid is provably gone; otherwise it fails
closed with `error: unreadable pid file <path>; remove it manually if no engine is
running.` — because a chmod/truncation/read race must not orphan a live engine.

**Single-writer lock:** the engine holds an advisory lock on the `<db>.lock` sidecar
(e.g. `data/arena.db.lock`) for as long as it owns the ledger — exactly one writer per
database. When another engine already holds it:

- `start` probes the lock *before* spawning and exits `1` with
  `error: another engine already holds <db> (single-writer lock is taken)`;
- `start --fresh` refuses to archive (prints `Nothing was moved; stop the running engine
  first.`) and exits `1`;
- the engine itself exits `1` with an actionable message (stop the running engine, pass
  `--db <other-path>`, or pass `--no-ledger`);
- `status` is unaffected: it opens the ledger read-only (a WAL reader, no lock), so it
  never fences a running engine out of its own database.

**`--fresh` archives, never deletes:** the DB plus its `-wal`, `-shm`, and `.lock`
sidecars are moved into `<db dir>/archive/` as
`<name>-<YYYYmmdd-HHMMSS-ffffff>.db` — for the default `data/arena.db` that is
`data/archive/arena-20261124-160546-589452.db` — so history stays inspectable while
every agent starts over at `$100`. If an engine still holds the single-writer lock,
`--fresh` refuses and moves nothing. Combined with `--no-ledger`, `--fresh` is a no-op:
a volatile run never opens the ledger, so archiving would destroy history the run does
not replace — `start` prints `note: --fresh ignored because --no-ledger is set` and
continues. When the database itself is already gone, `--fresh`
discards only the orphan `-wal`/`-shm` litter — the `.lock` file is never deleted, not
even when its flock looks free: a writer may have just created it and be one syscall away
from opening the database, and unlinking the inode it already holds would hand the next
engine a fresh lock file, i.e. two writers on one ledger. A stale lock file is harmless,
so the sweep only fails when one is actually held.

**Durability:** the ledger commits each fill in one transaction under
`synchronous=NORMAL` — a process crash is safe, but an OS crash can lose the last fill
still in the WAL. See the note in
[`deploy/tools_and_dashboard.py](#deploytools_and_dashboardpy) above.

**Exit codes:**

| Code | Command | Condition |
|------|---------|-----------|
| `0` | `start` | Backend alive after the 4 s startup probe; or TUI child exited 0 on its own |
| `1` | `start` | Bot already running (pid alive); `--fresh` refused because the ledger is in use; single-writer lock held elsewhere; the pid file could not be written (the just-launched child is stopped first); backend exited within the startup probe (last 20 log lines printed) |
| *child's code* | `start tui` | TUI child exited on its own (its return code is passed through) |
| `130` | `start tui` | Ctrl-C: SIGINT → graceful stop completed, pid file removed |
| `2` | `start tui` | TUI survived SIGINT → SIGTERM; pid file kept so `stop --force` can still reach it |
| `0` | `stop` | Process stopped by the SIGINT → SIGTERM ladder (SIGKILL with `--force`) |
| `1` | `stop` | Nothing to stop: no pid file, stale pid, a pid whose command line could not be verified, the process vanished before the first signal landed, or its command line no longer looks like the bot when re-verified before an escalation signal — pid file cleaned up without signalling. Also an unreadable/malformed pid file whose owner cannot be proven dead (file left in place) |
| `2` | `stop` | Survived SIGINT → SIGTERM; re-run with `--force` for SIGKILL (pid file left in place) |
| `0` | `status` | Always — reads the pid file and the ledger read-only |

**Examples:**
```bash
uv run python manage_bot.py start                 # detached backend (logs in .run/bot.log)
uv run python manage_bot.py start tui             # foreground dashboard; Ctrl-C stops
uv run python manage_bot.py start backend --fresh # archive wallet → new session at $100
uv run python manage_bot.py start --no-ledger     # volatile wallets, nothing persisted
uv run python manage_bot.py status                # process state + wallet table
uv run python manage_bot.py status --json
uv run python manage_bot.py stop                  # SIGINT → SIGTERM
uv run python manage_bot.py stop --force          # + SIGKILL if it will not go
```

---

## deploy/portfolio_monitor.py

Read-only monitor for an arena that is **already running**. The live wallets live in the
memory of the `tools_and_dashboard` process (mirrored to the SQLite ledger), so a second
copy of that entrypoint cannot observe them — it would start its own $100 wallet and,
because calfkit names consumer groups after the node, join the same groups and split the
stream into two half-arenas.

This entrypoint takes the indirect route: it replays the newest `trades_*.csv` through the
same `AccountStore` code that produced it, then marks the rebuilt positions to market from
`market_data.prices` under its own consumer group (`portfolio-monitor`). Killing it never
affects trading and never resets a wallet; it also never publishes anything.

| Flag | Required | Default | Description |
|------|----------|---------|-------------|
| `--bootstrap-servers` | No | `localhost:9092` | Kafka broker address |
| `--data-dir` | No | `./data` | Directory with the arena's `trades_*.csv` |
| `--config` | No | `config.json` | Config used to resolve `trading.fees.taker_bps` for replay |
| `--refresh` | No | `2.0` | Seconds between trades-CSV polls |
| `--once` | No | off | Replay and print one snapshot, then exit (no broker needed) |

**Examples:**
```bash
# Live read-only dashboard in a second terminal
uv run python -m deploy.portfolio_monitor --bootstrap-servers localhost:9092

# One-shot snapshot, broker not required
uv run python -m deploy.portfolio_monitor --once
```

If the running arena restarts, it writes a new `trades_*.csv`; the monitor notices the newer
file and resets to that session's state. Replayed cash is cross-checked against each row's
`cash_after` — a mismatch means `trading.fees.taker_bps` here differs from the running
process, and is logged as a warning.
