# How to run a Binance Futures Demo session

Use a Binance Futures Demo wallet with test USDT. Configure **One-way Mode**, **Single-Asset Mode**, **Cross Margin** and **1× leverage** for each enabled symbol. Turn off BNB fee payment and start with a flat wallet and no open orders. Use a dedicated wallet for this agent.

1. Copy your config to `data/config.futures-demo.json`. Keep your model provider and a `scalper-perp` agent configured with `"markets": ["perp"]`. Merge the following settings into the config. Set `BINANCE_FUTURES_DEMO_KEY` and `BINANCE_FUTURES_DEMO_SECRET` in the environment or an ignored `.env` file.

   ```json
   {
     "trading": {
       "exchange": "binance",
       "mode": "live",
       "binance_perp_symbols": ["ZECUSDT"],
       "live": {
         "market": "perp",
         "dry_run": true,
         "balance_refresh_seconds": 60,
         "accounts": {
           "demo": {
             "api_key": "${BINANCE_FUTURES_DEMO_KEY}",
             "api_secret": "${BINANCE_FUTURES_DEMO_SECRET}",
             "testnet": true
           }
         },
         "agent_accounts": {"scalper-perp": "demo"},
         "allow_symbols": ["ZECUSDT"]
       }
     }
   }
   ```

2. Use a dedicated Kafka broker for the Demo stack. The connector, tool server, and agent share topic names with paper and spot. Set `FUTURES_DEMO_BROKER` to that broker's bootstrap address. Run the stack from an isolated checkout if another local stack uses the same halt flag or output directory.

3. Start the connector, then the tool server in separate terminals. The connector selects its market-data environment from the mapped account. The tool server creates a dedicated ledger and allocates the Demo USDT wallet less `reserve_usdt` on the first clean startup.

   ```bash
   uv run python -m exchanges.binance_perp \
     --config data/config.futures-demo.json --bootstrap-servers "$FUTURES_DEMO_BROKER" \
     --board-interval 3

   uv run python -m deploy.tools_and_dashboard \
     --config data/config.futures-demo.json --bootstrap-servers "$FUTURES_DEMO_BROKER" \
     --db data/live-futures-demo.db --headless
   ```

4. Check the startup reconciliation and confirm the tool server reports `Binance Futures DEMO`. The tool server must report `ARMED` to accept trading intents. With the example configuration, it rechecks the wallet every 60 seconds. After a failed reconciliation it waits 5 seconds between attempts and automatically re-arms a reconciliation halt after a clean check. If a halt flag exists, review its reason and use `manage_bot.py resume --db data/live-futures-demo.db` only with the Demo engine stopped and its halt flag isolated.

   ```bash
   uv run python manage_bot.py status --config data/config.futures-demo.json \
     --db data/live-futures-demo.db

   uv run python -m deploy.agent --from-config scalper-perp \
     --config-path data/config.futures-demo.json --bootstrap-servers "$FUTURES_DEMO_BROKER" \
     --portfolio-db data/live-futures-demo.db
   ```

   Use the same SQLite file as the tool server's `--db`. With `--portfolio-db`, the
   agent reads the portfolio locally and generates at most one decision per market
   tick. If a free model returns a retryable error, the agent immediately tries
   `openai/gpt-oss-20b` once when it is in the configured pool.
   When enabling this mode on an existing deployment, restart the tool server first
   so it can receive decisions.

5. Confirm that `dry_run: true` produces only `DRY_RUN` order intents. To test Demo fills, stop the agent and tool server, verify `testnet: true` in the mapped account, set `dry_run: false`, then restart the tool server before the agent. Compare order IDs, fills, fees, remaining position, and wallet balance with Binance Demo. Test both a long and a short through entry and exit, and restart with the same ledger to verify reconciliation.

The connector polls the venue's order-book ticker. Keep the poll interval below `max_price_age_seconds` (default 5 seconds). The venue fills a market order at available prices. `max_price_slippage_bps` reserves margin headroom. [Binance market order API](https://developers.binance.com/docs/derivatives/usds-margined-futures/trade/rest-api/New-Order)

The tool server initializes and reconciles live futures automatically. Use `status` with the Demo ledger path to read its persisted arming state. If you start the tool server directly, the process row in `status` can say it is stopped while the ledger still reports its persisted arming state. Keep the same ledger across restarts so orders with uncertain outcomes can be resolved by client order ID. Use a separate config, account, broker, and ledger for a production deployment.

## Local recipe that worked (2026-11-27)

`testnet.binancefuture.com` now 301-redirects to `demo.binance.com`, so "testnet" here means
**Binance Futures Demo Trading**: create the HMAC key under *Demo Trading → Account → API
Management* (`https://demo.binance.com/en/my/settings/api-management`), not under the main
account. A key from the production account will not authenticate against
`demo-fapi.binance.com`.

The demo wallet needs all four venue settings before the tool server will arm, and
`arena/futures_session.py::_check_settings` reads them from the venue, not from the UI:

```bash
# one-way, single-asset, no BNB fee burn, 1× cross per symbol
curl -s -H "X-MBX-APIKEY: $BINANCE_FUTURES_DEMO_KEY" \
  "https://demo-fapi.binance.com/fapi/v1/symbolConfig?symbol=ZECUSDT&timestamp=..." # signed
```

`POST /fapi/v1/leverage?symbol=ZECUSDT&leverage=1` when the demo wallet defaults to 20×.

Two isolation seams matter more than the doc's "isolated checkout":

- **Halt flag is cwd-relative** (`arena/risk.py::DEFAULT_HALT_PATH` = `data/LIVE_HALT`). A
  second stack started from the repo root inherits the paper stack's halt and never arms. Run
  the demo stack from its own cwd (`.run/demo-cwd/data/LIVE_HALT` stays absent) and symlink
  `.env` in, because `load_config` reads dotenv from the *cwd*:

  ```bash
  mkdir -p .run/demo-cwd/data && ln -sfn "$PWD/.env" .run/demo-cwd/.env
  cd .run/demo-cwd
  PYTHONPATH=/repo uv run --project /repo python -m deploy.tools_and_dashboard \
    --config /repo/data/config.futures-demo.json --bootstrap-servers localhost:9094 \
    --db /repo/data/live-futures-demo.db --data-dir /repo/data --headless
  ```

  `manage_bot.py status` must run from the same cwd, or it reports the paper stack's flag.
- **A second broker**, so topic names (`agent_router.input.lean`, `<agent>.private.return`)
  never meet the paper stack's consumers:

  ```bash
  docker run -d --name local-broker-demo --network docker_default \
    -p 9094:9092 -p 29096:29092 -e CLUSTER_ID=<fresh> -e KAFKA_NODE_ID=2 -e KAFKA_BROKER_ID=2 \
    -e KAFKA_PROCESS_ROLES=broker,controller -e KAFKA_CONTROLLER_QUORUM_VOTERS=2@localhost:9093 \
    -e KAFKA_LISTENERS=HOST://:9092,DOCKER://:29092,CONTROLLER://:9093 \
    -e KAFKA_ADVERTISED_LISTENERS=HOST://localhost:9094,DOCKER://host.docker.internal:29096 \
    -e KAFKA_LISTENER_SECURITY_PROTOCOL_MAP=CONTROLLER:PLAINTEXT,HOST:PLAINTEXT,DOCKER:PLAINTEXT \
    -e KAFKA_CONTROLLER_LISTENER_NAMES=CONTROLLER -e KAFKA_INTER_BROKER_LISTENER_NAME=HOST \
    -e KAFKA_OFFSETS_TOPIC_REPLICATION_FACTOR=1 confluentinc/cp-kafka:8.0.0
  ```

Two things the logs say that are easy to misread:

- The tool-server banner still prints `every agent starts at $100` on a fresh ledger. The
  futures ledger is seeded with `venue wallet − reserve_usdt` instead; check `accounts.cash`
  in the demo DB (§3) rather than the banner.
- `manage_bot.py status` reports `Bot: not running` for a `nohup`-started tool server (§7.7
  of AGENTS.md) and reads the halt flag from its *own* cwd. The ledger's `live_state=ARMED`
  is the truth the engine obeys.
