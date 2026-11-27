"""Unit tests for config.py — loading, env-var resolution, and validation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from arena.fees import DEFAULT_TAKER_BPS, MAX_TAKER_BPS
from config import (
    AgentConfig,
    ArenaConfig,
    FeeConfig,
    LLMProviderConfig,
    TradingConfig,
    get_default_symbols,
    load_config,
    load_config_strict,
    resolve_env_vars,
)


def _write(tmp_path, payload):
    p = tmp_path / "config.json"
    p.write_text(json.dumps(payload))
    return p


# ── load_config / load_config_strict semantics ───────────────────


def test_absent_file_returns_defaults(tmp_path):
    cfg = load_config(tmp_path / "missing.json")
    assert cfg.trading.exchange == "coinbase"
    assert cfg.trading.fees.taker_bps == DEFAULT_TAKER_BPS
    assert cfg.trading.binance_symbols  # populated defaults
    assert cfg.trading.coinbase_products


def test_valid_file_is_parsed(tmp_path):
    path = _write(
        tmp_path,
        {"trading": {"exchange": "binance", "fees": {"taker_bps": 10}}},
    )
    cfg = load_config(path)
    assert cfg.trading.exchange == "binance"
    assert cfg.trading.fees.taker_bps == 10


def test_invalid_json_raises(tmp_path):
    path = tmp_path / "config.json"
    path.write_text("{ not valid json")
    with pytest.raises(Exception):
        load_config(path)


def test_strict_reraises_on_invalid_fee(tmp_path):
    path = _write(tmp_path, {"trading": {"fees": {"taker_bps": MAX_TAKER_BPS + 1}}})
    with pytest.raises(ValidationError):
        load_config_strict(path)


def test_strict_returns_defaults_when_absent(tmp_path):
    cfg = load_config_strict(tmp_path / "missing.json")
    assert cfg.trading.fees.taker_bps == DEFAULT_TAKER_BPS


# ── resolve_env_vars ─────────────────────────────────────────────


def test_resolve_whole_string_var(monkeypatch):
    monkeypatch.setenv("MY_KEY", "secret-value")
    assert resolve_env_vars("${MY_KEY}") == "secret-value"


def test_resolve_missing_var_raises_with_path(monkeypatch):
    monkeypatch.delenv("ABSENT_VAR", raising=False)
    with pytest.raises(ValueError, match=r"ABSENT_VAR.*llm_providers\.openai\.api_key"):
        resolve_env_vars({"llm_providers": {"openai": {"api_key": "${ABSENT_VAR}"}}})


def test_resolve_recurses_into_dicts_and_lists(monkeypatch):
    monkeypatch.setenv("A", "1")
    monkeypatch.setenv("B", "2")
    out = resolve_env_vars({"x": ["${A}", "${B}"], "y": {"z": "${A}"}})
    assert out == {"x": ["1", "2"], "y": {"z": "1"}}


def test_resolve_passes_through_non_strings():
    assert resolve_env_vars({"n": 5, "b": True, "f": 1.5}) == {"n": 5, "b": True, "f": 1.5}


def test_resolve_does_not_substitute_embedded_refs(monkeypatch):
    """Whole-string-only by design (finding R6): embedded ${VAR} is left verbatim."""
    monkeypatch.setenv("TOKEN", "xyz")
    assert resolve_env_vars("Bearer ${TOKEN}") == "Bearer ${TOKEN}"
    assert resolve_env_vars("${TOKEN}-suffix") == "${TOKEN}-suffix"


# ── get_default_symbols ──────────────────────────────────────────


def test_get_default_symbols_per_exchange(monkeypatch):
    # get_default_symbols reads the repo config.json, which references ${OPENAI_API_KEY};
    # a dummy value keeps resolve_env_vars from raising (see finding R3).
    monkeypatch.setenv("OPENAI_API_KEY", "dummy")
    assert "SOLUSDT" in get_default_symbols("binance")
    assert "BTC-USD" in get_default_symbols("coinbase")
    assert get_default_symbols("BINANCE") == get_default_symbols("binance")  # case-insensitive


def test_get_default_symbols_unknown_exchange_raises(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "dummy")
    with pytest.raises(ValueError, match="Unknown exchange"):
        get_default_symbols("kraken")


# ── Model validation ─────────────────────────────────────────────


@pytest.mark.parametrize("bps", [-1, MAX_TAKER_BPS + 1])
def test_fee_config_rejects_out_of_range(bps):
    with pytest.raises(ValidationError):
        FeeConfig(taker_bps=bps)


@pytest.mark.parametrize("bps", [0, DEFAULT_TAKER_BPS, MAX_TAKER_BPS])
def test_fee_config_accepts_in_range(bps):
    assert FeeConfig(taker_bps=bps).taker_bps == bps


def test_agent_config_requires_positive_workers():
    with pytest.raises(ValidationError):
        AgentConfig(name="a", provider="openai", model="m", max_workers=0)


def test_agent_config_rejects_unknown_reasoning_effort():
    with pytest.raises(ValidationError):
        AgentConfig(name="a", provider="openai", model="m", reasoning_effort="extreme")


def test_arena_config_lookup_helpers():
    cfg = ArenaConfig(
        llm_providers={"openai": LLMProviderConfig(api_key="k")},
        agents=[AgentConfig(name="momentum", provider="openai", model="m")],
    )
    assert cfg.get_provider_config("openai").api_key == "k"
    assert cfg.get_provider_config("missing") is None
    assert cfg.get_agent_config("momentum").model == "m"
    assert cfg.get_agent_config("missing") is None


# ── Committed schema vs model (finding R5) ───────────────────────


def _find_key(obj, key):
    """First value whose parent dict has `key`, searched depth-first."""
    if isinstance(obj, dict):
        if key in obj and isinstance(obj[key], dict):
            return obj[key]
        for v in obj.values():
            found = _find_key(v, key)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = _find_key(v, key)
            if found is not None:
                return found
    return None


def test_committed_schema_encodes_taker_bps_upper_bound():
    schema = json.loads(Path("config.schema.json").read_text())
    taker = _find_key(schema, "taker_bps")
    assert taker is not None
    assert taker.get("maximum") == MAX_TAKER_BPS


# ── Provider extra headers (providers like opencode-go reject requests
#    without a routing header, e.g. x-opencode-session) ─────────


def test_provider_extra_headers_default_empty():
    assert LLMProviderConfig().extra_headers == {}


def test_provider_extra_headers_parsed_from_file(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCODE_GO_API_KEY", "oc_sk_test")
    path = _write(
        tmp_path,
        {
            "llm_providers": {
                "opencode-go": {
                    "api_key": "${OPENCODE_GO_API_KEY}",
                    "base_url": "https://opencode.ai/inference/go/openai/v1",
                    "extra_headers": {"x-opencode-session": "arena-test"},
                }
            }
        },
    )
    provider = load_config(path).get_provider_config("opencode-go")
    assert provider.api_key == "oc_sk_test"
    assert provider.extra_headers == {"x-opencode-session": "arena-test"}


def test_provider_extra_headers_resolve_env_vars(tmp_path, monkeypatch):
    monkeypatch.setenv("SESSION_HEADER_VALUE", "from-env")
    path = _write(
        tmp_path,
        {"llm_providers": {"p": {"extra_headers": {"x-session": "${SESSION_HEADER_VALUE}"}}}},
    )
    assert load_config(path).get_provider_config("p").extra_headers == {"x-session": "from-env"}


def test_committed_schema_documents_extra_headers():
    schema = json.loads(Path("config.schema.json").read_text())
    provider = _find_key(schema, "extra_headers")
    assert provider is not None
    assert provider.get("type") == "object"


# ── Provider extra headers: agent wiring ────────────────────────


def test_extra_header_flags_parse_into_mapping(monkeypatch):
    import deploy.agent as agent_module

    monkeypatch.setattr(
        "sys.argv",
        [
            "agent",
            "--name", "a",
            "--strategy", "default",
            "--model-id", "m",
            "--bootstrap-servers", "localhost:9092",
            "--extra-header", "x-opencode-session=arena-a",
            "--extra-header", "x-trace=1",
        ],
    )
    args = agent_module.parse_args()
    assert args.extra_headers == {"x-opencode-session": "arena-a", "x-trace": "1"}


def test_extra_header_flag_rejects_malformed_pair(monkeypatch):
    import deploy.agent as agent_module

    monkeypatch.setattr(
        "sys.argv",
        [
            "agent",
            "--name", "a",
            "--strategy", "default",
            "--model-id", "m",
            "--bootstrap-servers", "localhost:9092",
            "--extra-header", "no-equals-sign",
        ],
    )
    with pytest.raises(SystemExit):
        agent_module.parse_args()


# ── Strategy assignment: a typo dies at load, not at agent launch ─


def test_unknown_strategy_fails_at_load_time(tmp_path):
    path = _write(
        tmp_path,
        {"agents": [{"name": "a", "provider": "p", "model": "m", "strategy": "swing_trend"}]},
    )

    with pytest.raises(ValidationError, match="unknown strategy"):
        load_config(path)


def test_committed_schema_lists_every_strategy():
    from arena.strategies import STRATEGIES

    schema = json.loads(Path("config.schema.json").read_text())
    strategy = _find_key(schema, "strategy")

    assert sorted(strategy["enum"]) == sorted(STRATEGIES)


# ── .env loading ────────────────────────────────────────────────
# Every entrypoint reads config.json, but only deploy/*.py called load_dotenv.
# With an env-referenced api_key, the exchange connectors crashed at startup
# because they resolved ${VAR} against a process env that never saw .env.


def test_load_config_honors_dotenv_in_cwd(tmp_path, monkeypatch):
    monkeypatch.delenv("ARENA_DOTENV_PROBE", raising=False)
    (tmp_path / ".env").write_text("ARENA_DOTENV_PROBE=from-dotenv\n")
    (tmp_path / "config.json").write_text(
        json.dumps({"llm_providers": {"p": {"api_key": "${ARENA_DOTENV_PROBE}"}}})
    )
    monkeypatch.chdir(tmp_path)

    assert load_config().get_provider_config("p").api_key == "from-dotenv"


def test_load_config_env_var_beats_dotenv(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("ARENA_DOTENV_PROBE=from-dotenv\n")
    (tmp_path / "config.json").write_text(
        json.dumps({"llm_providers": {"p": {"api_key": "${ARENA_DOTENV_PROBE}"}}})
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ARENA_DOTENV_PROBE", "from-process")

    assert load_config().get_provider_config("p").api_key == "from-process"


# ── Paper instrument rules (venue-accurate paper fills) ─────────


def test_paper_instrument_rules_default_to_venue():
    assert TradingConfig().paper.instrument_rules == "venue"


def test_paper_instrument_rules_accept_simple():
    cfg = TradingConfig.model_validate({"paper": {"instrument_rules": "simple"}})
    assert cfg.paper.instrument_rules == "simple"


@pytest.mark.parametrize("value", ["off", "VENUE", ""])
def test_paper_instrument_rules_reject_unknown_values(value, tmp_path):
    path = _write(tmp_path, {"trading": {"paper": {"instrument_rules": value}}})
    with pytest.raises(ValidationError):
        load_config(path)


def test_instrument_cache_path_is_configurable(tmp_path):
    path = _write(tmp_path, {"trading": {"instrument_cache": "data/custom.json"}})
    assert load_config(path).trading.instrument_cache == "data/custom.json"


def test_committed_schema_documents_instrument_rules():
    schema = json.loads(Path("config.schema.json").read_text())
    assert "instrument_rules" in schema["$defs"]["PaperConfig"]["properties"]
    assert "instrument_cache" in schema["$defs"]["TradingConfig"]["properties"]


# ── Live trading configuration (real money, so fail closed) ─────


def _live_payload(**overrides) -> dict:
    """A complete, valid live-mode config that individual tests then break."""
    payload = {
        "agents": [{"name": "a1", "provider": "p", "model": "m"}],
        "llm_providers": {"p": {"api_key": "k"}},
        "trading": {
            "mode": "live",
            "live": {
                "accounts": {"main": {"api_key": "k", "api_secret": "s"}},
                "agent_accounts": {"a1": "main"},
                "allow_symbols": ["SOLUSDT"],
            },
        },
    }
    for path, value in overrides.items():
        target = payload["trading"]["live"] if path != "trading" else payload["trading"]
        if path == "trading":
            target.update(value)
        else:
            target[path] = value
    return payload


def test_trading_mode_defaults_to_paper():
    assert TradingConfig().mode == "paper"
    assert TradingConfig().live.dry_run is True


def test_a_complete_live_config_loads(tmp_path):
    cfg = load_config(_write(tmp_path, _live_payload()))
    assert cfg.trading.mode == "live"
    assert cfg.trading.live.agent_accounts == {"a1": "main"}
    assert cfg.trading.live.dry_run is True  # still opt-in to real orders


def test_live_mode_without_credentials_refuses_to_start(tmp_path):
    payload = _live_payload()
    payload["trading"]["live"]["accounts"] = {"main": {"api_key": "", "api_secret": ""}}
    with pytest.raises(ValidationError, match="api_key"):
        load_config(_write(tmp_path, payload))


def test_live_mode_without_an_agent_mapping_refuses_to_start(tmp_path):
    with pytest.raises(ValidationError, match="agent_accounts is empty"):
        load_config(_write(tmp_path, _live_payload(agent_accounts={})))


def test_live_mode_without_an_allowlist_refuses_to_start(tmp_path):
    with pytest.raises(ValidationError, match="allow_symbols is empty"):
        load_config(_write(tmp_path, _live_payload(allow_symbols=[])))


def test_agent_mapping_must_reference_a_defined_account(tmp_path):
    with pytest.raises(ValidationError, match="not defined in live.accounts"):
        load_config(_write(tmp_path, _live_payload(agent_accounts={"a1": "missing"})))


def test_live_mode_rejects_more_than_one_account(tmp_path):
    """Two venue accounts would need per-account executors; refuse until they exist."""
    payload = _live_payload()
    payload["agents"] = [
        {"name": "a1", "provider": "p", "model": "m"},
        {"name": "a2", "provider": "p", "model": "m"},
    ]
    payload["trading"]["live"]["accounts"]["second"] = {"api_key": "k2", "api_secret": "s2"}
    payload["trading"]["live"]["agent_accounts"] = {"a1": "main", "a2": "second"}

    with pytest.raises(ValidationError, match="more than one account"):
        load_config(_write(tmp_path, payload))


def test_an_unused_second_account_is_allowed(tmp_path):
    payload = _live_payload()
    payload["trading"]["live"]["accounts"]["second"] = {"api_key": "k2", "api_secret": "s2"}

    cfg = load_config(_write(tmp_path, payload))

    assert cfg.trading.live.agent_accounts == {"a1": "main"}


def test_order_pct_cannot_exceed_position_pct(tmp_path):
    with pytest.raises(ValidationError, match="must not exceed"):
        load_config(
            _write(tmp_path, _live_payload(max_order_pct_of_equity=60, max_position_pct_of_equity=50))
        )


def test_live_defaults_are_the_paranoid_ones(tmp_path):
    live = load_config(_write(tmp_path, _live_payload())).trading.live
    assert live.max_order_pct_of_equity == 25.0
    assert live.max_position_pct_of_equity == 50.0
    assert live.max_daily_loss_pct == 10.0
    assert live.max_orders_per_minute == 2
    assert live.max_price_age_seconds == 5.0
    assert live.deposit_policy == "credit_sole_agent"


def test_a_misspelled_limit_fails_instead_of_being_ignored(tmp_path):
    """extra='forbid': a typo must not silently drop a guardrail."""
    payload = _live_payload()
    payload["trading"]["live"]["max_dailly_loss_pct"] = 5  # three l's

    with pytest.raises(ValidationError) as exc:
        load_config(_write(tmp_path, payload))

    assert "max_dailly_loss_pct" in str(exc.value)


def test_a_comment_key_is_allowed_in_the_live_block(tmp_path):
    payload = _live_payload()
    payload["trading"]["live"]["_comment"] = "operator note"
    assert load_config(_write(tmp_path, payload)).trading.live.comment == "operator note"


def test_paper_mode_ignores_an_incomplete_live_block(tmp_path):
    cfg = load_config(
        _write(tmp_path, {"trading": {"mode": "paper", "live": {"dry_run": False}}})
    )
    assert cfg.trading.mode == "paper"
    assert cfg.trading.live.accounts == {}


def test_committed_schema_documents_live_mode():
    schema = json.loads(Path("config.schema.json").read_text())
    trading = schema["$defs"]["TradingConfig"]["properties"]
    assert "mode" in trading
    assert "live" in trading
    live = schema["$defs"]["LiveConfig"]["properties"]
    for key in (
        "accounts",
        "agent_accounts",
        "dry_run",
        "allow_symbols",
        "max_order_pct_of_equity",
        "max_daily_loss_pct",
        "max_price_age_seconds",
    ):
        assert key in live


# ── Model pool (one agent, several models) ───────────────────────


def test_agent_config_takes_a_model_pool():
    agent = AgentConfig(name="a", provider="openai", model=["gpt-5-nano", "gpt-5-mini"])

    assert agent.model_ids == ["gpt-5-nano", "gpt-5-mini"]


def test_agent_config_wraps_a_lone_model_into_the_pool():
    agent = AgentConfig(name="a", provider="openai", model="gpt-5-nano")

    assert agent.model_ids == ["gpt-5-nano"]


@pytest.mark.parametrize(
    "model",
    [
        "",
        [],
        ["gpt-5-nano", ""],
        ["gpt-5-nano", "gpt-5-nano"],
    ],
)
def test_agent_config_rejects_an_unusable_model_pool(model):
    with pytest.raises(ValidationError):
        AgentConfig(name="a", provider="openai", model=model)


def test_committed_schema_matches_the_generated_model_field():
    """The committed schema is hand-maintained; pin the one field we edited by hand."""
    generated = ArenaConfig.model_json_schema()["$defs"]["AgentConfig"]["properties"]["model"]
    committed = json.loads(Path("config.schema.json").read_text())

    assert committed["$defs"]["AgentConfig"]["properties"]["model"] == generated


def test_committed_schema_offers_a_model_pool():
    schema = json.loads(Path("config.schema.json").read_text())

    model = schema["$defs"]["AgentConfig"]["properties"]["model"]
    shapes = [option.get("type") for option in model.get("anyOf", [model])]

    assert shapes == ["string", "array"]


# ── Market-message profile per agent ─────────────────────────────


def test_agent_config_payload_defaults_to_the_full_market_message():
    agent = AgentConfig(name="a", provider="p", model="m")

    assert agent.payload == "full"


def test_agent_config_accepts_the_lean_payload():
    agent = AgentConfig(name="a", provider="p", model="m", payload="lean")

    assert agent.payload == "lean"


def test_agent_config_rejects_an_unknown_payload():
    with pytest.raises(ValidationError):
        AgentConfig(name="a", provider="p", model="m", payload="tiny")


def test_committed_schema_documents_the_payload_profiles():
    schema = json.loads(Path("config.schema.json").read_text())

    payload = schema["$defs"]["AgentConfig"]["properties"]["payload"]

    assert payload["enum"] == ["full", "lean"]
    assert payload["default"] == "full"
