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
    "binance_symbols": ["BTCUSDT", "SOLUSDT", "FARTCOINUSDT"],
    "coinbase_products": ["BTC-USD", "SOL-USD", "FARTCOIN-USD"],
    "fees": { "taker_bps": 60 }
  }
}
```

> **Note:** The Anthropic API is not OpenAI-compatible. To use Claude models, configure them via the `openrouter` provider or another OpenAI-compatible proxy.

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
| `--model-id` | Yes* | — | Model ID (e.g. `gpt-5-nano`, `deepseek-chat`) |
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
    --symbols BTCUSDT ETHUSDT SOLUSDT
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

## deploy/portfolio_monitor.py

Read-only monitor for an arena that is **already running**. The wallets live in the memory
of the `tools_and_dashboard` process, so a second copy of that entrypoint cannot observe
them — it would start its own $100k wallet and, because calfkit names consumer groups after
the node, join the same groups and split the stream into two half-arenas.

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
