# Rate limits met by rotating an agent's models, not by adding agents

**Status:** accepted

## Context & decision

Agents trade on a fixed cadence (a market tick every 15–60s per agent), and an agent
that is rate-limited does not trade that tick — it just misses. The obvious fix is
another agent, but that doubles the LLM traffic, splits one trader's wallet and
strategies across two consumer groups, and makes the ledger attribute the same
strategy to two identities. So the capacity is added *inside* one agent's model client
instead: `model` in `config.json` takes a list, and `arena/model_pool.RoundRobinModel`
wraps those clients behind the same `PydanticModelClient` interface the single-model
agent already used.

The rotation rules are the ones a trader would want, not a generic retry policy:

- **Strict round-robin.** Consecutive inferences go to consecutive models, so load —
  and therefore rate-limit pressure — is spread evenly instead of all landing on one
  model until it breaks.
- **A failed model sits out.** `429`, `403`/`404` (this key cannot use this model id),
  `408`, `409`, any `5xx`, or a transport failure rests that model for a cooldown
  (default 30s, `--model-cooldown-seconds`) and the request is retried on the next model
  immediately. A typo'd model id therefore costs one tick, not every Nth tick forever.
- **A resting model is tried last, not skipped.** If every model is cooling down, the
  agent still makes one attempt per model rather than refusing to trade — one extra
  `429` is cheaper than a missed tick.
- **A permanent error is not rotated.** `400`s and other request-shaped `4xx` are the
  caller's bug; replaying a malformed request against every model would burn each
  model's quota to produce the same failure.
- **The pool owns retrying.** Pooled clients get the OpenAI SDK's own retries disabled
  (`max_retries = 0`), otherwise two SDK retries — which sleep on `Retry-After`, up to a
  minute each — happen *before* the pool can fail over, inside a tick that is 15–60s long.
  A single-model agent keeps the SDK default, since it has no pool to fail over to.

## Considered options

- **`FallbackModel` (pydantic-ai).** Already available, but it always tries models in
  preference order: the first model absorbs all traffic until it fails, which is
  exactly the model most likely to rate-limit again the moment the cooldown ends. It
  also has no resting/cooldown memory, so concurrent requests keep hitting the model
  that just failed.
- **`litellm` router / `LiteLLMProxy`.** A new dependency plus a proxy process to do
  what 60 lines do here, and it would move model routing out of the config file.

## Consequences

- A model can change *mid-conversation*: pydantic-ai replays the whole message history
  each request, and every model in the pool is given the identical strategy prompt and
  tool schemas, so a switch is invisible to the strategy. This relies on the pool being
  models of comparable capability — a pool mixing a strong and a weak model will trade
  in two different styles.
- One pool shares one provider and API key, so it dodges *per-model* quotas, not a
  per-account one. Crossing accounts means a new provider entry.
- Streaming (`request_stream`) is not implemented; `RoundRobinModel` raises the base
  class's `NotImplementedError`. Calfkit's agent loop only calls `request`, and a
  mid-stream failover would be a lie about what the caller already received.
- The rotation cursor is claimed *before* the request is awaited, so concurrent requests
  (`max_workers > 1`, not wired up today) still split across models instead of both
  starting on the same one.
