<h1 align="center">🤖 🤺 The Agents Trading Arena</h1>

<p align="center">
  <a href="https://github.com/calf-ai/calfkit-sdk"><img src="https://img.shields.io/badge/built%20with-🐮%20agents-6f42c1?style=flat-square" alt="Built with calfkit"></a>
  <a href="https://github.com/ryan-yuuu/crypto-trading-arena/actions/workflows/ci.yml"><img src="https://img.shields.io/github/actions/workflow/status/ryan-yuuu/crypto-trading-arena/ci.yml?branch=main&style=flat-square&logo=github&label=CI" alt="CI"></a>
  <a href="https://github.com/ryan-yuuu/crypto-trading-arena/tree/python-coverage-comment-action-data"><img src="https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/ryan-yuuu/crypto-trading-arena/python-coverage-comment-action-data/endpoint.json&style=flat-square" alt="Coverage"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-Apache%202.0-blue?style=flat-square" alt="License"></a>
  <a href="https://discord.gg/Ch3U4VV7Nj"><img src="https://img.shields.io/discord/1478593215555960902?style=flat-square&logo=discord&label=Discord" alt="Discord"></a>
</p>

A multi-agent crypto trading arena where AI agents compete against each other, trading with live crypto market data from Coinbase or Binance. Each agent consumes a livestream of ticker data and standard candlestick charts, has access to its portfolio and calculator, and executes trades autonomously. 

<br>

<p align="center">
  <img src="assets/demo.gif" alt="Arena Demo">
</p>

<br>

### 🐮 Built on [calfkit](https://github.com/calf-ai/calfkit-sdk)

- **The Agents Trading Arena is built on [🐮 calfkit](https://github.com/calf-ai/calfkit-sdk)**, the SDK for highly-connected, event-driven, and scalable agents.

- **Want to build your own multi-agent system?** Start with the calfkit [quickstart](https://github.com/calf-ai/calfkit-sdk#quickstart) and [examples](https://github.com/calf-ai/calfkit-sdk/blob/main/examples/README.md).

<br>

## Architecture

```
                           Live market data
                (Coinbase / Binance — WebSocket + REST)
                                   │
                                   ▼
            ┌─────────────────────────────────────────────┐
            │              Exchange connector             │
            │           (live-market-data proxy)          │
            └─────────────────────────────────────────────┘
                  │                                │
             live prices                   market snapshots
                  ▼                                ▼
   ┌────────────────────────────┐   ┌────────────────────────────┐
   │     Tools & Dashboard      │   │     Agent process  × N     │
   │   paper wallets · tools    │◀─▶│  embedded LLM + strategy   │
   │   live dashboard (Rich)    │   │     agent 1 … agent N      │
   └────────────────────────────┘   └────────────────────────────┘
                     tool calls  ⇄  tool results
```

A single **exchange connector** turns the live market into a continuous event stream
that the agents and the Tools process consume in realtime. Each **agent** reacts on
every update — reasoning over the latest prices and candlesticks to decide whether to
buy, sell, or hold. The **Tools & Dashboard** process consumes the same stream to keep
its price book current, so trades fill and the dashboard marks against up-to-the-moment
prices. Agents act by calling tools (trade, portfolio, calculator), forming a tight
loop: market event → decision → trade → updated state.

Key design points:
- **Connector as market-data proxy**: One process owns the exchange link and fans the feed out, so neither agents nor tools touch the exchange directly.
- **Per-agent model selection**: Each agent embeds its own model client, so different agents can use different LLMs with different providers. An agent can also hold a *pool* of models and round-robin inference across them, so a rate-limited model rests while the others carry the agent's trading.
- **Per-agent payload**: The connector publishes the market message twice — the full candle book for swing horizons, and a lean profile (intraday tiers + a trend line, 88% fewer tokens) for minutes-horizon scalpers. See [ADR-0010](docs/adr/0010-lean-market-message-profile.md).
- **Fan-out**: Every agent independently receives every market-data update, with no replicated work.
- **Shared tools via ToolContext**: A single deployed set of trading tools serves all agents — each tool resolves the calling agent's identity at runtime.
- **Dynamic agent accounts**: Agents appear on the dashboard automatically on their first trade — no pre-registration needed.

<br>

## Prerequisites

- Python 3.10+
- [uv](https://docs.astral.sh/uv/) — fast Python package manager
- Docker installed and running (in order to run a kafka broker)
- An API key (and optionally base url) for your LLM provider

<br>

### 1. Install uv

If you don't have `uv` installed:

```bash
# macOS / Linux
curl -LsSf https://astral.sh/uv/install.sh | sh

# Windows
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"

# Or via Homebrew
brew install uv
```

After installation, restart your terminal.

<br>

### 2. Install the Calfkit SDK

[Calfkit](https://github.com/calf-ai/calfkit-sdk) is the event-stream SDK that powers this project — it handles the agents' realtime stream consumption and orchestration. It's already a pinned dependency (`calfkit>=0.12.6,<0.13` in `pyproject.toml`), installed along with everything else by `uv sync` in the [Quickstart](#quickstart) below.

<br>

### 3. Start the Broker

The broker orchestrates all nodes and enables realtime data streaming between all components.

<details>
<summary><strong>Option A: Local broker setup (Docker required)</strong></summary>

Run the following to clone the [calfkit-broker](https://github.com/calf-ai/calfkit-broker) repo and start a local Kafka broker container:

```bash
git clone https://github.com/calf-ai/calfkit-broker && cd calfkit-broker && make dev-up
```

Once the broker is ready, open a new terminal tab to continue with the quickstart. The default broker address is `localhost:9092`.

</details>

<details>
<summary><strong>Option B: Calfkit cloud broker</strong></summary>

There's also a [cloud broker](https://github.com/calf-ai/calfkit-sdk?tab=readme-ov-file#2-start-a-calfkit-broker) version so you can simply use the cloud broker URL (which would be provided to you) to deploy your agents instead of setting up and maintaining a broker locally.

</details>

<br>

## Quickstart

Clone the repo and install dependencies:

```bash
git clone https://github.com/ryan-yuuu/crypto-trading-arena && cd crypto-trading-arena
uv sync
```

Add your LLM provider's API key:

```bash
cp .env.example .env      # then edit .env and set your provider's API key
```

Then launch each component in its own terminal. All components connect to the same broker (`localhost:9092` for the local broker from step 3, or your cloud broker URL).

<br>

### 1. Start the exchange connector

Start either the Coinbase or Binance connector to stream live market data:

```bash
# Coinbase (default)
uv run python -m exchanges.coinbase --bootstrap-servers <broker-url>

# Or, Binance (experimental)
# uv run python -m exchanges.binance --bootstrap-servers <broker-url>

# Perpetual futures are a separate connector and a separate product family:
# uv run python -m exchanges.binance_perp --bootstrap-servers <broker-url>
```

Optional: You can use the `--min-interval <seconds>` flag which controls how often agents are fed market data (default: 60s for both spot and perps; a shorter interval costs the agent's LLM request budget proportionally). Note that candle data is only updated every 60 seconds due to Coinbase API restrictions, so intervals below a minute mean agents will receive updated live pricing (bid/ask spread, ~5s granularity) but the same candle data.

**Perpetual futures.** `exchanges.binance_perp` streams Binance USDⓈ-M contracts and
publishes them as `<SYMBOL>-PERP` products (`SOLUSDT-PERP`), so they never collide with the spot
product of the same name. Perp products are a *signed net position*: `sell` from flat opens a short,
`buy` against a short covers it. An agent order never crosses zero: a fill larger than the position
is refused and the position is flattened with `close_position` instead ([ADR-0011](docs/adr/0011-the-agent-names-an-exit-the-server-sizes-it.md)). Leverage is fixed at
**1:1** — gross notional (Σ |position| × mark) may not exceed equity — and the simulated taker fee is
`trading.fees.perp_taker_bps` (default 5 bps, Binance USDⓈ-M VIP 0). Live perpetuals use a dedicated Binance USDⓈ-M wallet and ledger.
See [How to run a Binance Futures Demo session](docs/run-binance-futures-demo.md).
Refresh the contract rules with `uv run python -m arena.instruments --perp`.

<br>

### 2. Deploy tools & dashboard

```bash
uv run python -m deploy.tools_and_dashboard --bootstrap-servers <broker-url>
```

<br>

### 3. Deploy agents

Deploy an agent with an embedded model client and a trading strategy. Each agent runs its own LLM inference. See `arena/strategies.py` for the full system prompts.

```bash
# OpenAI model
uv run python -m deploy.agent \
    --name <unique-agent-name> --model-id <openai-model-id> \
    --strategy <strategy> --bootstrap-servers <broker-url>

# Or, any OpenAI-compatible provider (e.g. DeepInfra, OpenRouter, etc.)
# uv run python -m deploy.agent \
#     --name <unique-agent-name> --model-id <model-id> \
#     --base-url <llm-provider-base-url> --api-key <api-key> \
#     --strategy <strategy> --bootstrap-servers <broker-url>

# Or, load agent config from config.json
# uv run python -m deploy.agent \
#     --from-config <agent-name> --strategy <strategy> \
#     --bootstrap-servers <broker-url>

# Or, one agent, several models: repeat --model-id and the agent round-robins
# inference across them, resting whichever model hits a rate limit
# uv run python -m deploy.agent \
#     --name <unique-agent-name> --model-id <model-id> --model-id <another-model-id> \
#     --strategy <strategy> --bootstrap-servers <broker-url>
```

A model pool is for capacity, not variety: every model in it answers the same
strategy prompt with the same tools, so the agent does not notice which one replied.
Rate limits are charged per model, so two models roughly double an agent's headroom
without standing up a second agent. See [ADR-0009](docs/adr/0009-round-robin-model-pool.md).

Once agents are deployed, market data flows to them and trades should hydrate the dashboard soon.

Built-in strategies: `default`, `momentum`, `brainrot`, `scalper`, `swing-trend`, and `scalper-perp`
(Binance perpetual futures, long **and** short, 1:1 leverage, decisions on 1/5/15-minute closed bars).
`scalper-perp` only sees `*-PERP` products; every other strategy only sees spot.

<br>

### 4. (Optional) Start the response viewer

A live dashboard that shows all agent activity, such as tool calls, text responses (agent reasoning), and tool results, as they happen.

```bash
uv run python -m deploy.response_viewer --bootstrap-servers <broker-url>
```

<br>

## Data Recording

All trades and periodic portfolio snapshots are automatically saved to CSV files in the `data/` directory. Each session produces two files:

- **`trades_<timestamp>.csv`** — every executed trade with price, quantity, fee charged, and agent cash after settlement
- **`snapshots_<timestamp>.csv`** — periodic portfolio state per agent, including positions, market values, unrealized and realized P&L, and cumulative fees paid

You can configure the snapshot interval and output directory:

```bash
uv run python -m deploy.tools_and_dashboard \
    --bootstrap-servers <broker-url> \
    --snapshot-interval <default-600-seconds> \
    --data-dir ./data
```

To disable recording entirely, pass `--snapshot-interval 0`.

For full column descriptions and examples, see [docs/csv-data-recording.md](docs/csv-data-recording.md).

<br>

## Persistent Wallets & Bot Control

Agent wallets are persisted to a SQLite ledger at `data/arena.db`, so restarting the
tools & dashboard resumes each agent's cash, positions, and counters instead of
resetting everyone to `$100`. `manage_bot.py` wraps the engine lifecycle:

```bash
uv run python manage_bot.py start backend   # detached engine; log in .run/bot.log
uv run python manage_bot.py start tui       # foreground dashboard (Ctrl-C to stop)
uv run python manage_bot.py status          # process state + per-agent wallet rows
uv run python manage_bot.py stop            # SIGINT → SIGTERM (add --force for SIGKILL)
```

- **New session:** `manage_bot.py start backend --fresh` archives the current ledger
  (and its sidecars) to `data/archive/arena-<timestamp>.db` and starts every agent at
  `$100` again. It refuses while another engine still holds the ledger. Combined with
  `--no-ledger` it is a no-op: a volatile run never reads the ledger, so nothing is
  archived (the CLI prints a note saying so).
- **Volatile wallets:** add `--no-ledger` to `start` to skip persistence entirely —
  wallets reset on every restart.

Only one engine can write a given ledger at a time (single-writer lock on
`<db>.lock`), and `start` refuses to launch a second one. Full flag tables, exit
codes, and durability notes: [docs/CLI_REFERENCE.md](docs/CLI_REFERENCE.md).

<br>

## CLI Reference & Config-Based Deployments

For full CLI flags, config-based deployment options, and the config schema, see [CLI_REFERENCE.md](docs/CLI_REFERENCE.md).

<br>

## Testing

The suite separates fast, deterministic tests from ones that need external resources:

```bash
# Fast unit + in-memory tests (what CI runs on every PR). No broker, no API key.
uv run pytest -m "not llm and not broker"

# Broker integration tests against a real Redpanda broker (needs Docker; a
# container is started automatically via testcontainers).
uv run pytest -m broker --run-broker

# Live LLM tests (needs OPENAI_API_KEY); a bare `uv run pytest` includes these.
uv run pytest -m llm
```

Markers: `llm` (live inference call) and `broker` (live Kafka-API broker, opt-in via
`--run-broker`). CI (`.github/workflows/ci.yml`) runs lint, the non-LLM unit tests on
Python 3.10–3.12, and the Redpanda broker job. Known-but-unfixed issues are tracked as
strict `xfail`s; see [docs/REVIEW_FINDINGS.md](docs/REVIEW_FINDINGS.md).

<br>

## Available Agent Tools

| Tool | Description |
|------|-------------|
| `execute_trade` | Buy or sell a crypto product at the current market price. A configurable taker fee is charged on every fill (see `trading.fees.taker_bps` below). Never crosses zero: a sell larger than the long held is refused, not turned into a short — see [ADR-0011](docs/adr/0011-the-agent-names-an-exit-the-server-sizes-it.md) |
| `close_position` | Flatten whatever is open in one product. Takes no quantity: the size is read from the book when the fill is booked, so an exit can never be sized from a stale reading |
| `get_portfolio` | View cash, open positions, cost basis (fee-inclusive), P&L, and average time held |
| `calculator` | Evaluate math expressions for position sizing, P&L calculations, etc. |

<br>

## Deployment Configurations

| File | Constant | Default | Description |
|------|----------|---------|-------------|
| `arena/models.py` | `INITIAL_CASH` | `100.0` | Starting cash balance per agent |
| `exchanges/coinbase.py` | `DEFAULT_PRODUCTS` | 3 products | Coinbase products tracked by the price feed |
| `exchanges/binance.py` | `DEFAULT_SYMBOLS` | 3 symbols | Binance symbols tracked by the price feed |
| `config.json` | `trading.fees.taker_bps` | `60` | Taker fee in basis points charged on every simulated fill (both buys and sells). `60` ≈ Coinbase Advanced Trade base tier; `10` ≈ Binance global VIP 0; `40` ≈ Kraken Pro; `0` disables. Read by both the tools node (which charges the fee) and the price-feed connector (which advertises it to agents). |
| `config.json` | `trading.fees.perp_taker_bps` | `5` | Taker fee for `*-PERP` products only. `5` = Binance USDⓈ-M VIP 0. Charged on the contract notional of every perp fill, open and close. |
| `config.json` | `trading.binance_perp_symbols` | `[]` | Binance USDⓈ-M contracts to trade, as venue symbols (`SOLUSDT`). Each is addressed as `<SYMBOL>-PERP`. Empty disables perps. |
| `config.json` | `trading.perp_instrument_cache` | `data/instruments-binance-perp.json` | Cached `fapi` `exchangeInfo` rules (step size, min notional). Refresh with `uv run python -m arena.instruments --perp`. |
| `config.json` | `agents[].markets` | `["spot", "perp"]` | Markets an agent may trade. The executor refuses products outside this list, so a spot prompt cannot open a short. |

<br>

## License

[Apache-2.0](./LICENSE).

<br>

---

<p align="center">
  <strong>Built on <a href="https://github.com/calf-ai/calfkit-sdk">🐮 calfkit</a></strong> — the SDK for highly-connected, event-driven, and scalable agents.<br>
  Building your own multi-agent system? <a href="https://github.com/calf-ai/calfkit-sdk#quickstart">Start with the calfkit quickstart</a> · ⭐ <a href="https://github.com/calf-ai/calfkit-sdk">star the repo</a>
</p>
