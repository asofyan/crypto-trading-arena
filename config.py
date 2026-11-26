"""Configuration management for the Trading Arena.

Supports loading from config.json with fallback to defaults.
API keys can be embedded directly or referenced via env vars using ${VAR_NAME} syntax.
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from dotenv import find_dotenv, load_dotenv

from arena.fees import DEFAULT_PERP_TAKER_BPS, DEFAULT_TAKER_BPS, MAX_TAKER_BPS
from arena.strategies import STRATEGIES

DEFAULT_CONFIG_PATH = Path("config.json")

# Default coin pairs
DEFAULT_BINANCE_SYMBOLS = ["SOLUSDT", "XRPUSDT", "LINKUSDT"]
DEFAULT_COINBASE_PRODUCTS = ["BTC-USD", "FARTCOIN-USD", "SOL-USD"]

# Provider default endpoints
# NOTE: The "anthropic" provider is listed for documentation purposes, but the
# Anthropic API is NOT OpenAI-compatible. To use Claude models, configure them
# via "openrouter" or another OpenAI-compatible proxy instead.
PROVIDER_DEFAULTS = {
    "openai": {
        "base_url": "https://api.openai.com/v1",
        "default_model": "gpt-5-nano",
    },
    "openrouter": {
        "base_url": "https://openrouter.ai/api/v1",
        "default_model": "anthropic/claude-sonnet-4",
    },
}


class LLMProviderConfig(BaseModel):
    """Configuration for an LLM provider (must be OpenAI-compatible)."""

    api_key: str = Field("", description="API key or ${ENV_VAR} reference.")
    base_url: str = Field("", description="Base URL for the provider's API.")
    default_model: str = Field("", description="Default model ID when not overridden by an agent.")
    extra_headers: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Extra HTTP headers sent with every inference request. Some providers "
            "reject requests without a routing header, e.g. opencode-go requires "
            "`x-opencode-session`."
        ),
    )


class AgentConfig(BaseModel):
    """An agent deployment combining model configuration with trading strategy."""

    name: str = Field(description="Unique agent name (used as consumer group + identity).")
    provider: str = Field(description="Key into llm_providers (e.g. 'openai', 'openrouter').")
    model: str = Field(description="Model ID to use (e.g. 'gpt-5-nano', 'anthropic/claude-sonnet-4').")
    max_workers: int = Field(1, description="Concurrent inference workers.", ge=1)
    reasoning_effort: Literal["low", "medium", "high"] | None = Field(
        None, description="Reasoning effort level for supported models."
    )
    strategy: str = Field("default", description="Trading strategy (selects system prompt).")
    markets: list[Literal["spot", "perp"]] = Field(
        default_factory=lambda: ["spot", "perp"],
        description=(
            "Markets this agent's strategy is written for. Products outside this list "
            "are refused by the executor, so a spot prompt cannot accidentally open a "
            "short and a perp prompt cannot buy inventory."
        ),
    )

    @field_validator("strategy")
    @classmethod
    def _known_strategy(cls, value: str) -> str:
        """Reject a typo at load time; otherwise it only surfaces at agent launch."""
        if value not in STRATEGIES:
            raise ValueError(
                f"unknown strategy {value!r}; available: {', '.join(sorted(STRATEGIES))}"
            )
        return value


class FeeConfig(BaseModel):
    """Trading fee configuration. All simulated fills cross the spread, so only
    the taker rate is applied. Expressed in basis points (1 bps = 0.01%).

    Default 60 bps matches Coinbase Advanced Trade's base-tier taker rate. Set to
    10 for Binance global (VIP 0), 40 for Kraken Pro, or 0 to disable.
    """

    taker_bps: int = Field(
        DEFAULT_TAKER_BPS,
        description="Taker fee in basis points (60 bps = 0.60%). 0 disables fees.",
        ge=0,
        le=MAX_TAKER_BPS,
    )
    perp_taker_bps: int = Field(
        DEFAULT_PERP_TAKER_BPS,
        description=(
            "Taker fee in basis points for perpetual-futures products (5 bps = 0.05%, "
            "Binance USDⓈ-M VIP 0). Applied to *-PERP fills instead of taker_bps."
        ),
        ge=0,
        le=MAX_TAKER_BPS,
    )


class PaperConfig(BaseModel):
    """How paper fills are priced.

    ``venue`` snaps every quantity to the cached ``exchangeInfo`` rules (step size,
    minimum notional), so a paper run rehearses exactly what the venue will accept and
    a rejection in paper is a rejection live. ``simple`` keeps the legacy one-decimal
    rule for deployments with no venue data (Coinbase products, offline runs).
    """

    instrument_rules: Literal["venue", "simple"] = Field(
        "venue",
        description="venue = enforce cached exchangeInfo rules; simple = legacy 1-decimal rule.",
    )


class LiveAccountConfig(BaseModel):
    """One Binance Spot account's credentials. Never commit the resolved values."""

    model_config = ConfigDict(extra="forbid")

    api_key: str = Field("", description="API key or ${ENV_VAR} reference.")
    api_secret: str = Field("", description="API secret or ${ENV_VAR} reference.")
    testnet: bool = Field(
        False, description="Use testnet.binance.vision instead of the live venue."
    )


class LiveConfig(BaseModel):
    """Real-money trading: credentials, which agents may trade, and the guardrails.

    Defaults are deliberately the paranoid ones: ``dry_run`` on, a 25% per-order cap,
    a 10% daily-loss budget and a 2-orders-per-minute ceiling. ``extra="forbid"`` so a
    misspelled limit fails at startup instead of silently not applying.
    """

    # extra="forbid" catches a misspelled limit; the `_comment` alias keeps the
    # example config's explanatory key (a repo-wide convention) legal.
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    comment: str | None = Field(
        None, alias="_comment", description="Operator note. Ignored by the engine."
    )
    accounts: dict[str, LiveAccountConfig] = Field(
        default_factory=dict, description="Named venue accounts, referenced by agent_accounts."
    )
    agent_accounts: dict[str, str] = Field(
        default_factory=dict,
        description="agent name → account name. An agent absent here cannot trade live.",
    )
    dry_run: bool = Field(
        True,
        description="Journal and validate every order without contacting the venue.",
    )
    reserve_usdt: float = Field(
        0.0, ge=0, description="Quote balance never allocated to agents (buffer for fees)."
    )
    balance_refresh_seconds: float = Field(
        30.0, gt=0, description="How often the real balance is re-read for equity sync."
    )
    sync_tolerance_usdt: float = Field(
        1.0, ge=0, description="Surplus smaller than this is treated as dust, not a deposit."
    )
    deposit_policy: Literal["credit_sole_agent", "proportional", "unallocated"] = Field(
        "credit_sole_agent",
        description="Where an externally funded surplus is credited.",
    )
    allow_symbols: list[str] = Field(
        default_factory=list, description="Symbols this bot may trade live. Empty denies all."
    )
    max_order_pct_of_equity: float = Field(
        25.0, gt=0, le=100, description="Per-order ceiling as a share of equity."
    )
    max_position_pct_of_equity: float | None = Field(
        50.0, description="Per-symbol position ceiling as a share of equity; null disables."
    )
    max_order_notional_usdt: float | None = Field(
        None, description="Optional absolute per-order ceiling (fat-finger backstop)."
    )
    max_daily_loss_pct: float = Field(
        10.0, gt=0, le=100, description="Halt when session P&L falls this far below equity."
    )
    max_orders_per_minute: int = Field(
        2, ge=1, description="Trading intent ceiling; also protects the venue rate limit."
    )
    max_price_age_seconds: float = Field(
        5.0, gt=0, description="Refuse to trade on a quote older than this."
    )
    max_price_slippage_bps: int = Field(
        50,
        ge=0,
        le=1000,
        description="Cash headroom reserved for the venue filling above the quoted touch.",
    )
    recv_window_ms: int = Field(
        5000, ge=1000, le=60000, description="Binance recvWindow for signed requests."
    )

    @model_validator(mode="after")
    def _check_limits_and_wiring(self) -> LiveConfig:
        if (
            self.max_position_pct_of_equity is not None
            and self.max_order_pct_of_equity > self.max_position_pct_of_equity
        ):
            raise ValueError(
                "max_order_pct_of_equity must not exceed max_position_pct_of_equity "
                "(a cap that a single order can breach is not a cap)"
            )
        for agent, account in self.agent_accounts.items():
            if account not in self.accounts:
                raise ValueError(
                    f"agent_accounts[{agent!r}] names account {account!r}, which is not "
                    f"defined in live.accounts ({', '.join(sorted(self.accounts)) or 'none'})"
                )
        return self


class TradingConfig(BaseModel):
    """Trading-related configuration."""

    mode: Literal["paper", "live"] = Field(
        "paper",
        description=(
            "paper = simulated fills against the price book; live = real orders on the "
            "venue (see trading.live). Defaults to paper: approving live trading is a "
            "deliberate config change, never an accident."
        ),
    )
    exchange: Literal["coinbase", "binance"] = Field(
        "coinbase", description="Which exchange connector to use for market data."
    )
    binance_symbols: list[str] = Field(
        default_factory=lambda: DEFAULT_BINANCE_SYMBOLS.copy(),
        description="Binance trading pair symbols to subscribe to.",
    )
    binance_perp_symbols: list[str] = Field(
        default_factory=list,
        description=(
            "Binance USDⓈ-M perpetual contracts to trade, as venue symbols (SOLUSDT). "
            "The arena addresses each as <SYMBOL>-PERP so it cannot collide with the "
            "spot product of the same name. Empty disables perps entirely."
        ),
    )
    perp_instrument_cache: str = Field(
        "data/instruments-binance-perp.json",
        description=(
            "Cached fapi exchangeInfo rules for the perp products. Refresh with "
            "`uv run python -m arena.instruments --perp`."
        ),
    )
    coinbase_products: list[str] = Field(
        default_factory=lambda: DEFAULT_COINBASE_PRODUCTS.copy(),
        description="Coinbase product IDs to subscribe to.",
    )
    instrument_cache: str = Field(
        "data/instruments-binance.json",
        description=(
            "Cached exchangeInfo rules that quantities are snapped to. Refresh with "
            "`uv run python -m arena.instruments --refresh`."
        ),
    )
    paper: PaperConfig = Field(
        default_factory=PaperConfig, description="Paper-mode fill rules."
    )
    live: LiveConfig = Field(
        default_factory=LiveConfig, description="Real-money trading configuration."
    )
    fees: FeeConfig = Field(
        default_factory=FeeConfig,
        description="Trading fees applied to every simulated fill.",
    )


class ArenaConfig(BaseModel):
    """Root configuration for the Trading Arena."""

    llm_providers: dict[str, LLMProviderConfig] = Field(
        default_factory=dict,
        description="Named LLM provider configurations. Keys are referenced by agents[].provider.",
    )
    agents: list[AgentConfig] = Field(
        default_factory=list,
        description="Agent deployments. Each agent targets a provider, model, and strategy.",
    )
    trading: TradingConfig = Field(
        default_factory=TradingConfig,
        description="Exchange connector and trading pair configuration.",
    )

    @model_validator(mode="after")
    def _check_live_mode(self) -> ArenaConfig:
        live = self.trading.live
        if self.trading.mode != "live":
            return self
        if self.trading.binance_perp_symbols:
            # A perp short credits its notional to the wallet, so it cannot be
            # reconciled against a spot balance sheet, and this executor only speaks
            # /api/v3. Refusing at load time beats a mysterious halt on the first order.
            raise ValueError(
                "perpetual futures are paper-only: trading.mode is 'live' and "
                "trading.binance_perp_symbols is not empty. Set trading.mode to 'paper' "
                "or empty binance_perp_symbols."
            )
        if not live.accounts:
            raise ValueError("trading.mode is 'live' but trading.live.accounts is empty")
        if not live.agent_accounts:
            raise ValueError(
                "trading.mode is 'live' but trading.live.agent_accounts is empty — no "
                "agent would be allowed to trade"
            )
        for name, account in live.accounts.items():
            missing = [
                field
                for field, value in (("api_key", account.api_key), ("api_secret", account.api_secret))
                if not value
            ]
            if missing:
                raise ValueError(
                    f"trading.live.accounts[{name!r}] is missing {', '.join(missing)} "
                    "(use ${ENV_VAR} references, never literal secrets)"
                )
        if not live.allow_symbols:
            raise ValueError(
                "trading.mode is 'live' but trading.live.allow_symbols is empty — every "
                "order would be denied"
            )
        configured = sorted(set(live.agent_accounts.values()))
        if len(configured) > 1:
            raise ValueError(
                f"live trading with more than one account ({', '.join(configured)}) is not "
                "supported yet; map the agents you want to trade to a single account"
            )
        known = {agent.name for agent in self.agents}
        unknown = sorted(set(live.agent_accounts) - known)
        if unknown:
            raise ValueError(
                f"live.agent_accounts names agent(s) not in agents[]: {', '.join(unknown)}"
            )
        return self

    def get_provider_config(self, provider_name: str) -> LLMProviderConfig | None:
        """Get configuration for a specific provider."""
        return self.llm_providers.get(provider_name)

    def get_agent_config(self, name: str) -> AgentConfig | None:
        """Get configuration for a specific agent."""
        for agent in self.agents:
            if agent.name == name:
                return agent
        return None


def resolve_env_vars(value: Any, path: str = "root") -> Any:
    """Recursively resolve environment variables in a value.

    Args:
        value: The value to resolve env vars in.
        path: Current path for error reporting (e.g., "llm_providers.openai.api_key").

    Returns:
        The value with env vars resolved.

    Raises:
        ValueError: If a referenced environment variable is not set.
    """
    if isinstance(value, str):
        match = re.match(r'^\$\{([^}]+)\}$', value)
        if match:
            env_var = match.group(1)
            env_value = os.getenv(env_var)
            if env_value is None:
                raise ValueError(
                    f"Environment variable '{env_var}' is not set (referenced at {path})"
                )
            return env_value
        return value
    elif isinstance(value, dict):
        return {k: resolve_env_vars(v, f"{path}.{k}") for k, v in value.items()}
    elif isinstance(value, list):
        return [resolve_env_vars(item, f"{path}[{i}]") for i, item in enumerate(value)]
    return value


def load_config(config_path: Path | str | None = None) -> ArenaConfig:
    """Load configuration from a file.

    Args:
        config_path: Path to config file. Defaults to config.json in project root.

    Returns:
        ArenaConfig with loaded values or defaults if file doesn't exist.
    """
    if config_path is None:
        config_path = DEFAULT_CONFIG_PATH
    else:
        config_path = Path(config_path)

    # Every entrypoint reads config.json, but only deploy/*.py used to call
    # load_dotenv — so an api_key written as ${SOME_VAR} crashed the exchange
    # connectors at startup. Loading here keeps resolution identical everywhere.
    # usecwd mirrors how DEFAULT_CONFIG_PATH ("config.json") is resolved: both are
    # relative to the launch directory. A real process env var still wins.
    load_dotenv(find_dotenv(usecwd=True))

    if not config_path.exists():
        # Return default configuration
        return ArenaConfig(
            trading=TradingConfig(
                binance_symbols=DEFAULT_BINANCE_SYMBOLS.copy(),
                coinbase_products=DEFAULT_COINBASE_PRODUCTS.copy(),
            )
        )

    with open(config_path, "r") as f:
        data = json.load(f)

    # Resolve environment variables before validation
    data = resolve_env_vars(data)

    return ArenaConfig.model_validate(data)


def load_config_strict(config_path: Path | str | None = None) -> ArenaConfig:
    """Load configuration, failing loud on an invalid file.

    `load_config` already returns defaults when the file is absent, so any
    exception here means the file exists but is broken (bad JSON, an unset
    `${ENV_VAR}` reference, or an out-of-range value). A wrong fee rate would
    invalidate the whole run and silently desync the fee charged on fills from
    the fee advertised to agents, so log at ERROR and re-raise rather than
    guessing a default. Used by the deploy entrypoints that resolve the fee.
    """
    try:
        return load_config(config_path)
    except Exception:
        logging.getLogger(__name__).error(
            "Failed to load config from %s; refusing to start with an unknown fee "
            "rate. Fix the config, or remove it to use defaults.", config_path,
        )
        raise


def get_default_symbols(exchange: str = "binance") -> list[str]:
    """Get default symbols for an exchange.

    Args:
        exchange: Either 'binance' or 'coinbase'.

    Returns:
        List of default symbols/products for the exchange.
    """
    config = load_config()
    if exchange.lower() == "binance":
        return config.trading.binance_symbols
    elif exchange.lower() == "coinbase":
        return config.trading.coinbase_products
    else:
        raise ValueError(f"Unknown exchange: {exchange}")
