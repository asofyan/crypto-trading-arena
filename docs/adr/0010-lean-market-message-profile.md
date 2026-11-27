# Two market-message profiles, not one prompt for every horizon

**Status:** accepted

## Context & decision

Every agent consumed the same 76 KB market message: the live touch, five candle tiers
(daily, 4h, 15m, 5m, 1m) and the fee disclosure. Measured on the live feed, that is
**~20,600 input tokens per tick** — and the agent loop re-sends it on every tool round,
so a tick with `get_portfolio` and an `execute_trade` costs ~68,000 input tokens.

Two thirds of that payload is invisible to a minutes-horizon scalper: the daily tier
(89 rows/product) and the 4h tier (239 rows/product) exist for `swing-trend`'s decision
tier — the code says so at `arena/models.py`. A scalper reads the three intraday windows
(15m/5m/1m, which tile the last three hours) plus, at most, enough context to know which
way the tide runs.

So the connectors now publish **two profiles of the same fan-out**:

- `agent_router.input` — unchanged, every tier. `swing-trend` keeps consuming it.
- `agent_router.input.lean` — the intraday tiers plus a compact trend line (4h EMA20
  distance, 4h 12-bar change, last daily change, position in the monthly range).

Each agent picks its profile with `agents[].payload` (`full` default, `lean` opt-in), and
`deploy/agent.py` subscribes it to the matching topic. Measured on the new perp pairs:
**20,633 → 2,399 tokens, an 88% cut.**

The trend summary exists because deleting the higher tiers outright loses the one thing
intraday data cannot show: a 40-day regime. It costs ~200 tokens instead of ~15,600.
Deliberately absent: a per-agent prompt rewrite inside the agent process, which would
have meant parsing candle tables back out of the prompt text, and per-strategy topics,
which would have meant a topic per prompt variant instead of one extra fan-out.

## Consequences

- A new agent pays nothing for the lean profile: the `lean` topic is only published when
  a connector is given a `lean_topic` (production sets it; tests and one-profile
  deployments do not), and an agent only subscribes to it if configured.
- The broker must auto-create `agent_router.input.lean` (or an operator must create it).
  Nothing in this repo provisions topics; the existing fan-out topic behaves the same way.
- The two profiles can drift if someone edits `build_market_message`. They are built by
  one function from one `CandleBook`, so a drift is a code change in one place, not two.
- The 4h/daily tiers still drive `CandleBook` polling for every connector, even when only
  lean agents are deployed: fetching them is ~10 REST calls every few minutes, far cheaper
  than the tokens they used to cost, and `swing-trend` may be deployed at any time.
