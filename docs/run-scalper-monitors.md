# Run the scalper monitors

## Set up Gmail alerts

Add these values to the local `.env` file.

```dotenv
GMAIL_EMAIL=sender@gmail.com
GMAIL_APP_PASSWORD=your-google-app-password
ALERT_EMAIL_TO=recipient@example.com
```

Use an app password from a Google account with 2-Step Verification enabled. [Google App Passwords](https://support.google.com/mail/answer/185833)

## Monitor OpenRouter retirements

Start `scalper-perp` with its normal agent command. The monitor starts with the agent when its provider is OpenRouter and its pool contains `:free` models and `openai/gpt-oss-20b` as the sole paid model.

The monitor checks OpenRouter's catalog every 15 minutes and emails announced expiration dates. After two successful catalog checks without a model, it takes that slot out of rotation. The other models, including GPT-OSS, continue their normal round-robin. A candidate must pass three tool-call probes before it fills the slot. If no candidate passes, the monitor sends an alert and checks again on the next cycle.

Run one agent per working directory for this pool. The agent saves replacements and pending alerts in `data/openrouter-scalper-perp.json`. To override a replacement, edit the original model ID in the deployment config and restart the agent.

## Track market changes

Run the tool server with CSV recording enabled. Use a tool server started on this version so the tick CSV records the venue and the quote timestamp. The market monitor selects Binance USD-M mainnet quotes and fetches completed 5-minute candles from the same venue.

Preview one evaluation without sending email or writing persistent state.

```bash
uv run python -m deploy.market_monitor --dry-run
```

Run the monitor continuously using the same config and data directory as the mainnet tool server. Keep one monitor process per state file.

```bash
uv run python -m deploy.market_monitor \
  --window-hours 24 --baseline-days 14 --persistent-evaluations 3
```

The monitor evaluates once per UTC day, using data through midnight UTC (07:00 WIB). It compares the latest window with the preceding baseline. The default signals are a 50% increase in median spread, a 50% change in median candle range, or a 15 percentage point drop in the share of samples reaching the target before the stop. The monitor separates long and short samples and reads the exit band, hold limit, and taker fee from the trading config.

The baseline stays fixed for the duration of each anomaly. Three consecutive daily signals escalate it to persistent. Review the email's sample counts and the actual fills before changing a pair. The test starts hypothetical entries from recorded quotes and measures the price path, while actual strategy results depend on the entries the agent selects.

Use `--help` for threshold and coverage options. With the default 80% coverage requirement, quote-based signals need sufficient tagged history in both windows. Coverage appears in the logs while the baseline fills. For archived CSVs from a verified mainnet-only directory, use `--allow-unlabeled-quotes` with that directory and specify `--quote-timezone` if the CSV timestamps were written in a different timezone.
