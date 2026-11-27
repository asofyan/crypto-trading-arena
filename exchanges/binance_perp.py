"""Binance USDⓈ-M perpetual futures market data -> Kafka.

A thin entrypoint, not a second connector: the streaming logic, the parsers and the
Kafka contract are shared with the spot connector, which is parameterised by
`exchanges.binance.PERP_VENUE`. Only the venue changes — futures hosts, `fapi` paths,
the `-PERP` product ids and the 15 s default publish interval.

Usage:
    uv run python -m exchanges.binance_perp
    uv run python -m exchanges.binance_perp --symbols SOLUSDT XRPUSDT LINKUSDT
    uv run python -m exchanges.binance_perp --min-interval 30
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from exchanges import AGENT_INPUT_TOPIC, AGENT_INPUT_TOPIC_LEAN, quiet_shared_inbox_reply_log
from exchanges.binance import PERP_VENUE, parse_args as binance_parse_args
from exchanges.binance import run as binance_run


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """The connector's CLI with the futures venue's prog and defaults."""
    return binance_parse_args(argv, venue=PERP_VENUE)


async def run(
    args: argparse.Namespace,
    agent_topic: str = AGENT_INPUT_TOPIC,
    lean_topic: str | None = AGENT_INPUT_TOPIC_LEAN,
) -> None:
    await binance_run(args, agent_topic=agent_topic, venue=PERP_VENUE, lean_topic=lean_topic)


def main() -> None:
    args = parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
        datefmt="%H:%M:%S",
    )
    # Fire-and-forget producer bound to the shared inbox (agent_router.output): quiet the
    # per-reply "no pending handle" notices so they don't drown genuine connector logs.
    quiet_shared_inbox_reply_log()

    asyncio.run(run(args))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
