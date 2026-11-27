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
from contextlib import AsyncExitStack, suppress
from functools import partial
from pathlib import Path

import httpx

from dotenv import load_dotenv

from calfkit import Agent, Client, OpenAIModelClient, Worker
from calfkit._vendor.pydantic_ai.messages import ModelRequest, SystemPromptPart, TextPart, UserPromptPart
from calfkit._vendor.pydantic_ai.models import ModelRequestParameters
from arena.ledger import Ledger
from arena.live_session import ARMED, STATE_META_KEY
from arena.risk import DEFAULT_HALT_PATH
from arena.single_call import (
    DECISION_INSTRUCTIONS,
    DECISION_TOPIC,
    SingleCallTick,
    SnapshotDecision,
)
from arena.alerts import GmailMailer
from arena.model_pool import (
    DEFAULT_COOLDOWN_SECONDS,
    DEFAULT_RATE_LIMIT_COOLDOWN_SECONDS,
    RoundRobinModel,
    normalize_model_ids,
)
from arena.openrouter_monitor import OpenRouterMonitor
from arena.tools import calculator, close_position, execute_trade, get_portfolio

from arena.strategies import STRATEGIES
from config import ArenaConfig, load_config
from exchanges import AGENT_INPUT_TOPIC, AGENT_INPUT_TOPIC_LEAN

load_dotenv()

# The tool set the agent is told about. `deploy/tools_and_dashboard.py` serves the same
# list; a mismatch is an unknown-tool error the model cannot recover from.
AGENT_TOOLS = [execute_trade, close_position, get_portfolio, calculator]


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
        "--portfolio-db",
        type=Path,
        help="Read the tool server ledger locally and generate one decision per market tick.",
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


LING_FLASH = "inclusionai/ling-3.0-flash"
OPENROUTER_URL = "https://openrouter.ai/api/v1"
OPENROUTER_PAID_FALLBACKS = ("openai/gpt-oss-20b", LING_FLASH)


def ling_flash_decision_options() -> dict:
    branches = []
    for action in ("hold", "close_position", "buy", "sell"):
        fields = {"action": {"type": "string", "enum": [action]}}
        if action != "hold":
            fields["product_id"] = {"type": "string"}
        if action in ("buy", "sell"):
            fields["quantity"] = {"type": "number"}
        branches.append({
            "type": "object",
            "properties": fields,
            "required": list(fields),
            "additionalProperties": False,
        })
    return {
        "provider": {
            "only": ["Ant Digital"],
            "allow_fallbacks": False,
            "require_parameters": True,
        },
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "trade_decision_by_action",
                "strict": True,
                "schema": {"type": "object", "anyOf": branches},
            },
        },
    }


def build_model_client(
    model_ids: list[str],
    *,
    base_url: str | None = None,
    api_key: str | None = None,
    reasoning_effort: str | None = None,
    extra_headers: dict[str, str] | None = None,
    model_cooldown_seconds: float = DEFAULT_COOLDOWN_SECONDS,
    rate_limit_cooldown_seconds: float = DEFAULT_RATE_LIMIT_COOLDOWN_SECONDS,
    single_attempt: bool = False,
) -> OpenAIModelClient | RoundRobinModel:
    """Build the agent's ``model_client``: one client, or a pool behind one.

    The pool hides itself behind the same interface, so a single-model agent keeps
    the plain client — it has nothing to rotate, so the SDK's own retries remain its
    only retry layer.
    """
    model_ids = normalize_model_ids(model_ids)
    openrouter = (base_url or "").rstrip("/") == OPENROUTER_URL
    clients = [
        OpenAIModelClient(
            model_name=model_id,
            base_url=base_url,
            api_key=api_key,
            reasoning_effort=reasoning_effort,
            extra_headers=extra_headers or None,
            **({"extra_body": ling_flash_decision_options()}
               if single_attempt and openrouter and model_id == LING_FLASH else {}),
        )
        for model_id in model_ids
    ]
    if len(clients) == 1:
        if model_cooldown_seconds != DEFAULT_COOLDOWN_SECONDS:
            print("note: --model-cooldown-seconds only applies to a model pool; ignoring")
        if single_attempt:
            clients[0].client.max_retries = 0
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
        single_attempt=single_attempt,
        paid_fallback=(
            next((model for model in OPENROUTER_PAID_FALLBACKS if model in model_ids), None)
            if single_attempt else None
        ),
    )


def monitors_openrouter(name: str, base_url: str | None, model_ids: list[str]) -> bool:
    paid = [model for model in model_ids if not model.endswith(":free")]
    return (
        name == "scalper-perp"
        and (base_url or "").rstrip("/") == OPENROUTER_URL
        and len(paid) == 1
        and paid[0] in OPENROUTER_PAID_FALLBACKS
        and len(model_ids) > 1
    )


async def cancel_monitor(task: asyncio.Task) -> None:
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


async def send_monitor_email(subject: str, body: str) -> None:
    await GmailMailer.from_env().send(subject, body)


def single_call_handler(tick: SingleCallTick):
    async def before_node(ctx):
        if ctx.delivery_kind != "call":
            return "Ignored tool continuation in one-call mode."
        message = ctx.state.uncommitted_message
        if not isinstance(message, ModelRequest):
            return "Skipped tick without a market prompt."
        market = "\n".join(
            part.content for part in message.parts
            if isinstance(part, UserPromptPart) and isinstance(part.content, str)
        )
        if not market.strip():
            return "Skipped tick without a market prompt."
        return await tick.run(market)

    return before_node


async def main() -> None:
    args = parse_args()

    config = None
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

    build_client = partial(
        build_model_client,
        base_url=args.base_url,
        api_key=api_key,
        reasoning_effort=args.reasoning_effort,
        extra_headers=args.extra_headers,
        single_attempt=args.portfolio_db is not None,
    )
    async with AsyncExitStack() as stack:
        ledger = None
        can_request = None
        if args.portfolio_db is not None:
            ledger = stack.enter_context(Ledger(args.portfolio_db, read_only=True))
            live_trading = config is not None and config.trading.mode == "live"
            if live_trading or ledger.get_meta(STATE_META_KEY) is not None:
                def trading_is_armed() -> bool:
                    return (
                        not DEFAULT_HALT_PATH.exists()
                        and ledger.get_meta(STATE_META_KEY) == ARMED
                    )

                can_request = trading_is_armed

        monitor = None
        model_ids = args.model_ids
        if monitors_openrouter(args.name, args.base_url, model_ids):
            http = await stack.enter_async_context(httpx.AsyncClient(
                base_url=args.base_url.rstrip("/") + "/",
                headers={**(args.extra_headers or {}), "Authorization": f"Bearer {api_key}"},
                timeout=15,
            ))

            def replace(old_id: str, new_id: str) -> None:
                replacement = build_client([new_id])
                replacement.client.max_retries = 0
                model_client.replace_model(old_id, replacement)

            monitor = OpenRouterMonitor(
                client=http,
                state_path=Path("data/openrouter-scalper-perp.json"),
                model_ids=model_ids,
                replace=replace,
                retire=lambda model_id: model_client.retire_model(model_id),
                send=send_monitor_email,
                reasoning_effort=args.reasoning_effort,
                can_probe=can_request,
                tools=[
                    {"type": "function", "function": {
                        "name": schema.name,
                        "description": schema.description,
                        "parameters": schema.parameters_json_schema,
                    }}
                    for schema in (tool.tool_schema for tool in AGENT_TOOLS)
                ],
            )
            model_ids = monitor.effective_models()

        model_client = build_client(
            model_ids,
            model_cooldown_seconds=args.model_cooldown_seconds,
            rate_limit_cooldown_seconds=args.model_rate_limit_cooldown_seconds,
        )
        if monitor is not None:
            for model_id in monitor.retired_models():
                model_client.retire_model(model_id)
            stack.push_async_callback(cancel_monitor, asyncio.create_task(monitor.run()))
            print("OpenRouter free-model retirement monitor enabled.")

        tools = AGENT_TOOLS
        agent_options = {}
        if ledger is not None:
            async def request_decision(market: str) -> str:
                response = await model_client.request(
                    [ModelRequest(parts=[
                        SystemPromptPart(content=f"{system_prompt}\n{DECISION_INSTRUCTIONS}"),
                        UserPromptPart(content=market),
                    ])],
                    getattr(model_client, "settings", None),
                    ModelRequestParameters(),
                )
                return "".join(part.content for part in response.parts if isinstance(part, TextPart))

            async def publish_decision(decision, trade_count: int) -> None:
                intent = SnapshotDecision(
                    agent_id=args.name, trade_count=trade_count, decision=decision
                )
                await client.broker.publish(intent, DECISION_TOPIC)

            tick = SingleCallTick(args.name, ledger, request_decision, publish_decision, can_request)
            tools = []
            agent_options["before_node"] = single_call_handler(tick)
            print(
                "One decision per tick with same-tick paid fallback. "
                f"Local portfolio ledger: {args.portfolio_db}"
            )

        agent = Agent(
            args.name,
            system_prompt=system_prompt,
            subscribe_topics=input_topic_for(args.payload),
            model_client=model_client,
            tools=tools,
            **agent_options,
        )
        worker = Worker(client, nodes=[agent], group_id=args.name)

        tool_names = ", ".join(t.tool_schema.name for t in tools)
        pool = len(model_ids) > 1
        print(f"  - Agent:    {args.name}")
        print(f"  - Strategy: {args.strategy}")
        print(f"  - Model:    {', '.join(model_ids)}{' (round robin)' if pool else ''}")
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
