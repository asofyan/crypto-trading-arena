"""Deploy a single named trading Agent for the daytrading arena.

Runs one agent per process (launch this module once per agent). Despite the
``agent_router.*`` topic names, this process does no routing itself — it hosts
one Agent node (plus its tool nodes) in a Worker.

Each agent subscribes to the shared ``agent_router.input`` topic with its
own consumer group, so every agent receives every market tick independently.
The model client is embedded directly in the Agent (no separate ChatNode). That
client may be a pool: repeat ``--model-id`` (or make ``model`` a list in
config.json) and the agent round-robins inference across those models, resting
whichever one hits a rate limit (see ``arena.model_pool``).

Example:
    uv run python -m deploy.agent \
        --name momentum --strategy momentum \
        --model-id gpt-5-nano --bootstrap-servers <broker-url>

    uv run python -m deploy.agent \
        --name brainrot-daytrader --strategy brainrot \
        --model-id deepseek-chat --base-url https://api.deepseek.com/v1 \
        --api-key $DEEPSEEK_API_KEY --bootstrap-servers <broker-url>

    uv run python -m deploy.agent \
        --from-config gpt-5-nano --strategy default \
        --bootstrap-servers <broker-url>
"""

import argparse
import asyncio
import math
import os
import sys

from dotenv import load_dotenv

from calfkit import Agent, Client, OpenAIModelClient, Worker
from arena.model_pool import (
    DEFAULT_COOLDOWN_SECONDS,
    DEFAULT_RATE_LIMIT_COOLDOWN_SECONDS,
    RoundRobinModel,
    normalize_model_ids,
)
from arena.tools import calculator, execute_trade, get_portfolio
from arena.strategies import STRATEGIES
from config import ArenaConfig, load_config
from exchanges import AGENT_INPUT_TOPIC, AGENT_INPUT_TOPIC_LEAN

load_dotenv()


def _parse_header_pair(pair: str) -> tuple[str, str]:
    """Parse a repeatable `--extra-header KEY=VALUE` argument."""
    key, sep, value = pair.partition("=")
    if not sep or not key.strip():
        raise argparse.ArgumentTypeError(f"expected KEY=VALUE, got {pair!r}")
    return key.strip(), value


def input_topic_for(payload: str) -> str:
    """The fan-out topic carrying this agent's market-message profile."""
    return AGENT_INPUT_TOPIC_LEAN if payload == "lean" else AGENT_INPUT_TOPIC


def _non_negative_seconds(text: str) -> float:
    """argparse type for a duration that cannot be negative."""
    try:
        seconds = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected seconds, got {text!r}") from None
    if not math.isfinite(seconds) or seconds < 0:
        raise argparse.ArgumentTypeError(f"expected a finite non-negative duration, got {text!r}")
    return seconds


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Deploy a named Agent for the daytrading arena.",
    )
    parser.add_argument(
        "--name",
        help="Agent name (used as consumer group + identity)",
    )
    parser.add_argument(
        "--strategy",
        default=None,
        choices=list(STRATEGIES.keys()),
        help=(
            "Trading strategy (selects system prompt). Required unless --from-config "
            "supplies one; an explicit flag overrides the config value."
        ),
    )
    parser.add_argument(
        "--payload",
        choices=("full", "lean"),
        default=None,
        help=(
            "Market-message profile: 'full' (every candle tier) or 'lean' (intraday "
            "tables + a trend line, ~88%% fewer tokens). Default: the config value, "
            "else 'full'."
        ),
    )
    parser.add_argument(
        "--model-id",
        dest="model_ids",
        action="append",
        metavar="MODEL_ID",
        help=(
            "Model ID passed to OpenAIModelClient (e.g. gpt-5-nano, deepseek-chat). "
            "Repeat the flag to give the agent a pool it round-robins across."
        ),
    )
    parser.add_argument(
        "--model-cooldown-seconds",
        type=_non_negative_seconds,
        default=DEFAULT_COOLDOWN_SECONDS,
        metavar="SECONDS",
        help=(
            "How long a model sits out after a rate limit or server error before it "
            "rejoins the rotation (default: %(default)s)"
        ),
    )
    parser.add_argument(
        "--model-rate-limit-cooldown-seconds",
        type=_non_negative_seconds,
        default=DEFAULT_RATE_LIMIT_COOLDOWN_SECONDS,
        metavar="SECONDS",
        help=(
            "How long a 429 (rate limit / exhausted quota) sits a model out; free tiers "
            "reset daily, so this must outlast a quota window (default: %(default)s)"
        ),
    )
    parser.add_argument(
        "--base-url",
        default=None,
        help="Base URL for OpenAI-compatible providers (default: OpenAI)",
    )
    parser.add_argument(
        "--api-key",
        default=None,
        help="API key for the provider (default: $OPENAI_API_KEY)",
    )
    parser.add_argument(
        "--reasoning-effort",
        default=None,
        help='Reasoning effort for reasoning models (e.g. "low")',
    )
    parser.add_argument(
        "--extra-header",
        dest="extra_header",
        action="append",
        default=[],
        type=_parse_header_pair,
        metavar="KEY=VALUE",
        help=(
            "Extra HTTP header sent with inference requests, repeatable "
            "(e.g. x-opencode-session=arena-agent)"
        ),
    )
    parser.add_argument(
        "--bootstrap-servers",
        required=True,
        help="Kafka bootstrap servers address",
    )
    parser.add_argument(
        "--from-config",
        metavar="AGENT_NAME",
        dest="from_config",
        help="Load agent configuration from config.json by name",
    )
    parser.add_argument(
        "--config-path",
        default="config.json",
        help="Path to config file (default: config.json)",
    )
    args = parser.parse_args()
    args.extra_headers = dict(args.extra_header)
    return args


def apply_agent_config(args: argparse.Namespace, config: ArenaConfig) -> bool:
    """Copy a config.json agent assignment onto ``args``.

    Returns False when the agent is absent, so the caller can list the known ones.
    config.json owns the strategy too: a launcher that forgets ``--strategy`` must
    not silently run a different strategy than the one the agent was assigned.
    """
    agent_config = config.get_agent_config(args.from_config)
    if agent_config is None:
        return False

    args.name = agent_config.name
    if args.payload is None:
        args.payload = agent_config.payload
    elif args.payload != agent_config.payload:
        print(
            f"note: --payload {args.payload} overrides config payload "
            f"{agent_config.payload!r} for {agent_config.name}"
        )
    if args.model_ids:
        # Compare normalized pools: a padded or reordered "same" pool is not an override.
        requested = normalize_model_ids(args.model_ids)
        if requested != agent_config.model_ids:
            print(
                f"note: --model-id {', '.join(requested)} overrides config model "
                f"{', '.join(agent_config.model_ids)}"
            )
        args.model_ids = requested
    else:
        args.model_ids = agent_config.model_ids
    args.reasoning_effort = agent_config.reasoning_effort

    if args.strategy is None:
        args.strategy = agent_config.strategy
    elif args.strategy != agent_config.strategy:
        print(
            f"note: --strategy {args.strategy} overrides config strategy "
            f"{agent_config.strategy!r} for {agent_config.name}"
        )

    provider_config = config.get_provider_config(agent_config.provider)
    if provider_config:
        args.api_key = provider_config.api_key or args.api_key
        args.base_url = provider_config.base_url or args.base_url
        args.extra_headers.update(provider_config.extra_headers)
    else:
        print(f"WARNING: Provider '{agent_config.provider}' not configured. Using CLI/env values.")
    return True


def build_model_client(
    model_ids: list[str],
    *,
    base_url: str | None = None,
    api_key: str | None = None,
    reasoning_effort: str | None = None,
    extra_headers: dict[str, str] | None = None,
    model_cooldown_seconds: float = DEFAULT_COOLDOWN_SECONDS,
    rate_limit_cooldown_seconds: float = DEFAULT_RATE_LIMIT_COOLDOWN_SECONDS,
) -> OpenAIModelClient | RoundRobinModel:
    """Build the agent's ``model_client``: one client, or a pool behind one.

    The pool hides itself behind the same interface, so a single-model agent keeps
    the plain client — it has nothing to rotate, so the SDK's own retries remain its
    only retry layer.
    """
    clients = [
        OpenAIModelClient(
            model_name=model_id,
            base_url=base_url,
            api_key=api_key,
            reasoning_effort=reasoning_effort,
            extra_headers=extra_headers or None,
        )
        for model_id in normalize_model_ids(model_ids)
    ]
    if len(clients) == 1:
        if model_cooldown_seconds != DEFAULT_COOLDOWN_SECONDS:
            print("note: --model-cooldown-seconds only applies to a model pool; ignoring")
        return clients[0]
    # The pool is the retry layer now: the SDK would retry twice more, sleeping on
    # Retry-After for up to a minute inside the tick we are trying to save, and spend
    # the very quota the pool exists to protect. `client.client` is the only way in
    # until calfkit forwards provider options (calf-ai/calfkit-sdk#377).
    for client in clients:
        client.client.max_retries = 0
    return RoundRobinModel(
        clients,
        cooldown_seconds=model_cooldown_seconds,
        rate_limit_cooldown_seconds=rate_limit_cooldown_seconds,
    )


async def main() -> None:
    args = parse_args()

    # Handle config-based deployment
    if args.from_config:
        config = load_config(args.config_path)
        try:
            applied = apply_agent_config(args, config)
        except ValueError as error:
            print(f"ERROR: {error}")
            sys.exit(1)
        if not applied:
            print(f"ERROR: Agent '{args.from_config}' not found in config.")
            print(f"Available agents: {[a.name for a in config.agents]}")
            sys.exit(1)

    # Validate required args
    if not args.name:
        print("ERROR: --name is required (or use --from-config)")
        sys.exit(1)
    if not args.model_ids:
        print("ERROR: --model-id is required (or use --from-config with configured model)")
        sys.exit(1)
    try:
        args.model_ids = normalize_model_ids(args.model_ids)
    except ValueError as error:
        print(f"ERROR: {error}")
        sys.exit(1)
    if not args.strategy:
        print("ERROR: --strategy is required (or use --from-config with a configured strategy)")
        sys.exit(1)

    if args.payload is None:
        args.payload = "full"

    system_prompt = STRATEGIES.get(args.strategy)
    if system_prompt is None:
        print(f"ERROR: Unknown strategy '{args.strategy}'")
        print(f"Available: {', '.join(STRATEGIES.keys())}")
        sys.exit(1)

    # Resolve API key: explicit flag > config > env var
    api_key = args.api_key or os.getenv("OPENAI_API_KEY")
    if not api_key:
        print("ERROR: No API key provided.")
        print("Pass --api-key, set in config, or set OPENAI_API_KEY.")
        sys.exit(1)

    print("=" * 50)
    print(f"Agent Deployment: {args.name}")
    print("=" * 50)

    print(f"\nConnecting to Kafka broker at {args.bootstrap_servers}...")
    client = Client.connect(args.bootstrap_servers)

    tools = [execute_trade, get_portfolio, calculator]
    agent = Agent(
        args.name,
        system_prompt=system_prompt,
        subscribe_topics=input_topic_for(args.payload),
        model_client=build_model_client(
            args.model_ids,
            base_url=args.base_url,
            api_key=api_key,
            reasoning_effort=args.reasoning_effort,
            extra_headers=args.extra_headers,
            model_cooldown_seconds=args.model_cooldown_seconds,
            rate_limit_cooldown_seconds=args.model_rate_limit_cooldown_seconds,
        ),
        tools=tools,
    )
    worker = Worker(client, nodes=[agent], group_id=args.name)

    tool_names = ", ".join(t.tool_schema.name for t in tools)
    pool = len(args.model_ids) > 1
    print(f"  - Agent:    {args.name}")
    print(f"  - Strategy: {args.strategy}")
    print(f"  - Model:    {', '.join(args.model_ids)}{' (round robin)' if pool else ''}")
    if pool:
        print(f"  - Cooldown: {args.model_cooldown_seconds:g}s")
    print(f"  - Input:    {input_topic_for(args.payload)} ({args.payload} payload)")
    print(f"  - Tools:    {tool_names}")
    if args.base_url:
        print(f"  - Base URL: {args.base_url}")
    if args.reasoning_effort:
        print(f"  - Reasoning effort: {args.reasoning_effort}")
    if args.extra_headers:
        print(f"  - Extra headers: {', '.join(args.extra_headers)}")

    print("\nAgent ready. Waiting for requests...")
    await worker.run()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nAgent stopped.")
