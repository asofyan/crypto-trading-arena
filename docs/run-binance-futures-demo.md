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

4. Check the startup reconciliation and confirm the tool server reports `Binance Futures DEMO`. The tool server must report `ARMED` to accept trading intents. If a halt flag exists, review its reason and use `manage_bot.py resume --db data/live-futures-demo.db` only with the Demo engine stopped and its halt flag isolated.

   ```bash
   uv run python manage_bot.py status --config data/config.futures-demo.json \
     --db data/live-futures-demo.db

   uv run python -m deploy.agent --from-config scalper-perp \
     --config-path data/config.futures-demo.json --bootstrap-servers "$FUTURES_DEMO_BROKER"
   ```

5. Confirm that `dry_run: true` produces only `DRY_RUN` order intents. To test Demo fills, stop the agent and tool server, verify `testnet: true` in the mapped account, set `dry_run: false`, then restart the tool server before the agent. Compare order IDs, fills, fees, remaining position, and wallet balance with Binance Demo. Test both a long and a short through entry and exit, and restart with the same ledger to verify reconciliation.

The connector polls the venue's order-book ticker. Keep the poll interval below `max_price_age_seconds` (default 5 seconds). The venue fills a market order at available prices. `max_price_slippage_bps` reserves margin headroom. [Binance market order API](https://developers.binance.com/docs/derivatives/usds-margined-futures/trade/rest-api/New-Order)

The tool server initializes and reconciles live futures automatically. Use `status` with the Demo ledger path to read its persisted arming state. If you start the tool server directly, the process row in `status` can say it is stopped while the ledger still reports its persisted arming state. Keep the same ledger across restarts so orders with uncertain outcomes can be resolved by client order ID. Use a separate config, account, broker, and ledger for a production deployment.
