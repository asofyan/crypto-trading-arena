"""Shared types and cross-process contracts for exchange connectors."""

import logging

from pydantic import BaseModel

PRICE_TOPIC = "market_data.prices"

# Agent-routing topics — the shared fan-out contract. Every agent subscribes to
# AGENT_INPUT_TOPIC under its own consumer group (so one publish broadcasts to all
# agents); agent turn events are observed on AGENT_OUTPUT_TOPIC. These names are an
# on-the-wire contract shared by independently-deployed processes and must stay
# byte-stable across the whole fleet.
AGENT_INPUT_TOPIC = "agent_router.input"
AGENT_OUTPUT_TOPIC = "agent_router.output"

# The lean profile of the same fan-out: intraday candle tiers plus a compact
# higher-timeframe trend line, for minutes-horizon agents. Same broadcast contract,
# ~10% of the tokens — a scalper does not need 328 rows of daily/4h history on every
# tick, and paying for them is what makes a model pool expensive.
AGENT_INPUT_TOPIC_LEAN = "agent_router.input.lean"

MARKET_PREAMBLE = (
    "Here is the latest ticker information. You should view your portfolio first before "
    "making any decisions to trade.\nprice = last traded price, best_bid = price you sell "
    "at, best_ask = price you buy at.\n"
)


def build_market_message(
    *,
    fee_disclosure: str,
    tickers_json: str,
    candle_book,
    product_ids: list[str],
    profile: str = "full",
) -> str:
    """Assemble the agent-facing market message for one profile.

    ``full`` keeps every candle tier (the swing-trend decision tier); ``lean`` keeps the
    intraday tiers plus a trend summary, which is what a scalper actually reads.
    """
    from arena.models import INTRADAY_TIMEFRAMES

    parts = [f"{MARKET_PREAMBLE}{fee_disclosure}\n\n{tickers_json}"]
    if candle_book is None or not candle_book.has_data():
        return "\n".join(parts)

    if profile == "lean":
        parts.append(
            "\n## Price History (intraday OHLCV)\n"
            "The three windows tile the last three hours at rising resolution.\n\n"
            f"{candle_book.format_prompt(product_ids, timeframes=INTRADAY_TIMEFRAMES)}"
        )
        trend = candle_book.trend_summary(product_ids)
        if trend:
            parts.append("\n" + trend)
        return "\n".join(parts)

    parts.append(
        "\n## Price History (OHLCV candlesticks)\n"
        "Below are candlesticks at several granularities — coarser for broader trend "
        "context, finer for recent price action.\n\n"
        f"{candle_book.format_prompt(product_ids)}"
    )
    return "\n".join(parts)

# calfkit routes agent replies to the invoker's inbox and logs a per-reply "no pending
# handle" notice under this (calfkit-owned) logger when a fire-and-forget / observer client —
# one that holds no run handles — binds a shared inbox like AGENT_OUTPUT_TOPIC. See ADR-0002.
SHARED_INBOX_REPLY_LOGGER = "calfkit.client.hub"


def quiet_shared_inbox_reply_log(level: int = logging.ERROR) -> None:
    """Suppress the shared-inbox "no pending handle" reply notices by raising the calfkit hub
    logger to ``level``. Connectors use ERROR (genuine fault replies stay visible); the
    full-screen viewer uses CRITICAL (it renders faults in-panel, and any stderr line would
    corrupt its display)."""
    logging.getLogger(SHARED_INBOX_REPLY_LOGGER).setLevel(level)


class TickerMessage(BaseModel):
    """Ticker message published to Kafka (common schema for all exchanges)."""

    product_id: str
    price: str
    best_bid: str
    best_bid_size: str
    best_ask: str
    best_ask_size: str
    side: str
    last_size: str
    open_24h: str
    high_24h: str
    low_24h: str
    volume_24h: str
    volume_30d: str
    trade_id: int
    sequence: int
    time: str
