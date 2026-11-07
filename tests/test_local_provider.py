"""Generic local OpenAI-compatible provider (LM Studio, vLLM, llama.cpp
server, text-generation-webui, ...) — the `vllm` config slot, surfaced to
users as "Local OpenAI-compatible". Mirrors test_ollama_provider.py, but
unlike Ollama there is no default port across backends, so api_base is
required rather than falling back to a guess.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from flowly.config.schema import Config
from flowly.integrations.active_provider import _build_for, resolve_active_provider


def _isolated_config(tmp_path: Path, monkeypatch) -> Config:
    """A ``Config()`` that can't see the real ``~/.flowly`` — every test in
    this module that inspects cascade/resolution behavior needs this, or it's
    at the mercy of whatever is on the machine actually running the suite
    (real account credentials, a real vllm.api_base from prior manual testing,
    ...), which makes the test order-dependent instead of deterministic."""
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-cli-home"))
    return Config()


def test_vllm_builds_with_explicit_api_base_and_dummy_key(tmp_path, monkeypatch) -> None:
    cfg = _isolated_config(tmp_path, monkeypatch)
    cfg.providers.vllm.api_base = "http://127.0.0.1:1234/v1"
    active = _build_for(cfg, "vllm")

    assert active is not None
    assert active.key == "vllm"
    assert active.api_key == "local"
    assert active.api_base == "http://127.0.0.1:1234/v1"
    assert active.source == "Local · OpenAI-compatible"


def test_vllm_uses_real_api_key_when_provided(tmp_path, monkeypatch) -> None:
    cfg = _isolated_config(tmp_path, monkeypatch)
    cfg.providers.vllm.api_base = "http://127.0.0.1:8000/v1"
    cfg.providers.vllm.api_key = "sk-real-key"
    active = _build_for(cfg, "vllm")

    assert active is not None
    assert active.api_key == "sk-real-key"


def test_vllm_requires_explicit_api_base(tmp_path, monkeypatch) -> None:
    """Unlike Ollama, there's no single sensible default port across LM
    Studio/vLLM/llama.cpp — an empty slot must resolve to unusable, not a
    guessed URL."""
    cfg = _isolated_config(tmp_path, monkeypatch)
    assert _build_for(cfg, "vllm") is None


def test_vllm_is_not_used_by_empty_cascade(tmp_path, monkeypatch) -> None:
    cfg = _isolated_config(tmp_path, monkeypatch)
    cfg.providers.flowly.enabled = False
    cfg.providers.openai_codex.enabled = False
    cfg.providers.xai_oauth.enabled = False
    cfg.providers.zai_coding.enabled = False

    assert resolve_active_provider(cfg) is None


def test_vllm_not_selected_by_cascade_even_with_api_base_set(tmp_path, monkeypatch) -> None:
    """The key safety property: vllm is explicit-only. Merely having a saved
    api_base (e.g. from a server the user isn't running anymore) must never
    make the cascade silently pick it — only an explicit
    `providers.active = "vllm"` does."""
    cfg = _isolated_config(tmp_path, monkeypatch)
    cfg.providers.flowly.enabled = False
    cfg.providers.openai_codex.enabled = False
    cfg.providers.xai_oauth.enabled = False
    cfg.providers.zai_coding.enabled = False
    cfg.providers.vllm.api_base = "http://127.0.0.1:1234/v1"

    resolved = resolve_active_provider(cfg)
    assert resolved is None or resolved.key != "vllm"


def test_explicit_vllm_active_resolves_with_custom_base(tmp_path, monkeypatch) -> None:
    cfg = _isolated_config(tmp_path, monkeypatch)
    cfg.providers.active = "vllm"
    cfg.providers.vllm.api_base = "http://localhost:8000/v1"

    active = resolve_active_provider(cfg)

    assert active is not None
    assert active.key == "vllm"
    assert active.api_base == "http://localhost:8000/v1"
    assert active.api_key == "local"


def test_explicit_vllm_active_without_api_base_falls_through_cascade(tmp_path, monkeypatch) -> None:
    """providers.active = "vllm" alone isn't enough — api_base must be set,
    or the explicit pick falls through to the cascade (never crashes)."""
    cfg = _isolated_config(tmp_path, monkeypatch)
    cfg.providers.active = "vllm"
    cfg.providers.openai_codex.enabled = False
    cfg.providers.xai_oauth.enabled = False
    cfg.providers.zai_coding.enabled = False

    resolved = resolve_active_provider(cfg)
    assert resolved is None or resolved.key != "vllm"


def test_resolve_provider_alias() -> None:
    from flowly.integrations.active_provider import resolve_provider_alias

    # "local" (the CLI/UI name) normalizes to the on-disk "vllm" slot.
    assert resolve_provider_alias("local") == "vllm"
    assert resolve_provider_alias("LOCAL") == "vllm"
    assert resolve_provider_alias("  local  ") == "vllm"
    # Everything else passes through unchanged.
    assert resolve_provider_alias("vllm") == "vllm"
    assert resolve_provider_alias("ollama") == "ollama"
    assert resolve_provider_alias("openai") == "openai"
    assert resolve_provider_alias("off") == "off"
    assert resolve_provider_alias("") == ""


def test_set_active_provider_accepts_local_alias(tmp_path: Path, monkeypatch) -> None:
    """`/provider local` (and desktop provider_set) must land on the vllm slot,
    matching `flowly setup local` — otherwise the CLI command name and the
    provider name disagree."""
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path))
    from flowly.config.loader import get_config_path
    from flowly.integrations.active_provider import set_active_provider

    path = get_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"providers": {"vllm": {"apiBase": "http://localhost:1234/v1"}}}),
        encoding="utf-8",
    )

    set_active_provider("local")
    data = json.loads(path.read_text(encoding="utf-8"))
    # Persisted as the canonical slug, never the alias.
    assert data["providers"]["active"] == "vllm"


def test_set_active_provider_accepts_vllm_without_rewriting_model(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path))
    from flowly.config.loader import get_config_path
    from flowly.integrations.active_provider import set_active_provider

    path = get_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({
            "providers": {"vllm": {"apiBase": "http://localhost:1234/v1"}},
            "agents": {"defaults": {"model": "qwen3-8b"}},
        }),
        encoding="utf-8",
    )

    changed = set_active_provider("vllm")
    data = json.loads(path.read_text(encoding="utf-8"))

    assert changed is None
    assert data["providers"]["active"] == "vllm"
    assert data["agents"]["defaults"]["model"] == "qwen3-8b"


def test_provider_list_surfaces_vllm_as_non_keyable_reporting_via_api_base(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-cli-home"))

    from flowly.channels import feature_rpc

    result = feature_rpc.provider_list()
    by_key = {p["key"]: p for p in result["providers"]}

    assert "vllm" in by_key
    assert by_key["vllm"]["keyable"] is False
    # Nothing configured yet -> not reported as configured.
    assert by_key["vllm"]["hasKey"] is False


def test_provider_list_vllm_configured_once_api_base_saved(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-cli-home"))

    from flowly.config.loader import save_config
    from flowly.channels import feature_rpc

    cfg = Config()
    cfg.providers.vllm.api_base = "http://localhost:1234/v1"
    save_config(cfg)

    result = feature_rpc.provider_list()
    by_key = {p["key"]: p for p in result["providers"]}

    # api_base alone (no api_key) is enough to report "configured" — api_key
    # is optional for this provider.
    assert by_key["vllm"]["hasKey"] is True
    assert by_key["vllm"]["apiBase"] == "http://localhost:1234/v1"


def test_vllm_model_catalog(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path))

    from flowly.config.loader import save_config
    from flowly.integrations import model_catalog

    cfg = Config()
    cfg.providers.vllm.api_base = "http://lmstudio.test/v1"
    save_config(cfg)

    class _Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {
                "data": [
                    {"id": "qwen3-8b", "owned_by": "lmstudio"},
                    {"id": "meta-llama/Llama-3.1-8B-Instruct", "owned_by": "vllm"},
                ]
            }

    class _Client:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def get(self, url: str, headers: dict):
            assert url == "http://lmstudio.test/v1/models"
            assert headers["Authorization"] == "Bearer local"
            return _Response()

    monkeypatch.setattr(model_catalog.httpx, "AsyncClient", _Client)
    model_catalog.flush_cache()

    models = asyncio.run(model_catalog.fetch_models("vllm", force_refresh=True))

    assert sorted(m.id for m in models) == [
        "meta-llama/Llama-3.1-8B-Instruct", "qwen3-8b",
    ]
    assert all(m.tags == ["local"] for m in models)


def _stub_fetch_models(monkeypatch, models):
    from flowly.integrations import model_catalog

    async def _fake(key, *, force_refresh=False):
        return list(models)

    monkeypatch.setattr(model_catalog, "fetch_models", _fake)
    return model_catalog


def test_autofill_model_for_local_replaces_unserved_model(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path))
    from flowly.config.loader import save_config
    from flowly.integrations.model_catalog import Model, autofill_model_for_local

    cfg = Config()
    cfg.agents.defaults.model = "anthropic/claude-haiku-4.5"  # cloud leftover
    save_config(cfg)
    _stub_fetch_models(monkeypatch, [Model(id="qwen3-8b", name="qwen3-8b"),
                                     Model(id="llama3.1", name="llama3.1")])

    filled = asyncio.run(autofill_model_for_local("vllm"))
    assert filled == "qwen3-8b"  # first served model
    from flowly.config.loader import load_config
    assert load_config().agents.defaults.model == "qwen3-8b"


def test_autofill_model_keeps_served_model(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path))
    from flowly.config.loader import save_config, load_config
    from flowly.integrations.model_catalog import Model, autofill_model_for_local

    cfg = Config()
    cfg.agents.defaults.model = "qwen3-8b"
    save_config(cfg)
    _stub_fetch_models(monkeypatch, [Model(id="qwen3-8b", name="qwen3-8b")])

    filled = asyncio.run(autofill_model_for_local("vllm"))
    assert filled is None  # already served → untouched
    assert load_config().agents.defaults.model == "qwen3-8b"


def test_autofill_model_noop_for_cloud_provider(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path))
    from flowly.integrations.model_catalog import autofill_model_for_local

    # Should not even fetch for a non-local provider.
    def _boom(*a, **k):
        raise AssertionError("must not fetch for cloud providers")

    monkeypatch.setattr("flowly.integrations.model_catalog.fetch_models", _boom)
    assert asyncio.run(autofill_model_for_local("openrouter")) is None


def test_autofill_model_noop_when_endpoint_empty(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path))
    from flowly.config.loader import save_config, load_config
    from flowly.integrations.model_catalog import autofill_model_for_local

    cfg = Config()
    cfg.agents.defaults.model = "some-cloud/model"
    save_config(cfg)
    _stub_fetch_models(monkeypatch, [])  # unreachable / no models

    assert asyncio.run(autofill_model_for_local("vllm")) is None
    # Left as-is; the switch proceeds and the user can fix the model.
    assert load_config().agents.defaults.model == "some-cloud/model"


def test_vllm_model_catalog_empty_when_api_base_missing(
    monkeypatch, tmp_path: Path
) -> None:
    """No network call should even happen when nothing is configured."""
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path))
    from flowly.integrations import model_catalog

    def _boom(*args, **kwargs):
        raise AssertionError("should not attempt a network call")

    monkeypatch.setattr(model_catalog.httpx, "AsyncClient", _boom)
    model_catalog.flush_cache()

    models = asyncio.run(model_catalog.fetch_models("vllm", force_refresh=True))
    assert models == []


# ── legacy config helpers (used by MCP sampling + a couple CLI paths) ──


def test_config_legacy_helpers_support_active_vllm(tmp_path, monkeypatch) -> None:
    """The legacy get_api_key/get_active_provider_name/get_api_base helpers must
    honor an explicit `active = "vllm"` the same way they do for ollama —
    otherwise MCP sampling (which uses them, not resolve_active_provider) raises
    "no API key configured" for a vllm user."""
    cfg = _isolated_config(tmp_path, monkeypatch)
    cfg.providers.active = "vllm"
    cfg.providers.vllm.api_base = "http://localhost:1234/v1"

    assert cfg.get_api_key() == "local"                    # dummy bearer, not None
    assert cfg.get_active_provider_name() == "vllm"        # not "unknown"
    assert cfg.get_api_base() == "http://localhost:1234/v1"


def test_config_legacy_helpers_vllm_real_key_and_no_base(tmp_path, monkeypatch) -> None:
    cfg = _isolated_config(tmp_path, monkeypatch)
    cfg.providers.active = "vllm"
    cfg.providers.vllm.api_key = "sk-real"
    # No api_base set -> helper returns None (no default to borrow), rather than
    # silently handing back some other slot's base.
    assert cfg.get_api_key() == "sk-real"
    assert cfg.get_api_base() is None


def test_config_legacy_helpers_keep_vllm_as_the_last_resort_as_before(tmp_path, monkeypatch) -> None:
    """A self-hosted endpoint with an API key was the cascade's last resort
    before local providers existed, in the legacy helpers too (MCP sampling,
    a few CLI paths). It stays so: a config that relied on it keeps working.
    Ollama never joins it."""
    cfg = _isolated_config(tmp_path, monkeypatch)
    cfg.providers.active = ""                       # cascade mode
    cfg.providers.vllm.api_key = "sk-self-hosted"
    cfg.providers.vllm.api_base = "http://localhost:8000/v1"

    assert cfg.get_active_provider_name() == "vllm"
    assert cfg.get_api_key() == "sk-self-hosted"
    assert cfg.get_api_base() == "http://localhost:8000/v1"
    # Any cloud key still comes first.
    cfg.providers.openrouter.api_key = "sk-or-x"
    assert cfg.get_active_provider_name() == "openrouter"


def test_the_cascade_keeps_a_keyed_self_hosted_endpoint_last_and_never_ollama(tmp_path, monkeypatch) -> None:
    cfg = _isolated_config(tmp_path, monkeypatch)
    cfg.providers.flowly.enabled = False
    cfg.providers.openai_codex.enabled = False
    cfg.providers.xai_oauth.enabled = False
    cfg.providers.zai_coding.enabled = False
    cfg.providers.ollama.api_base = "http://127.0.0.1:11434/v1"
    assert resolve_active_provider(cfg) is None
    cfg.providers.vllm.api_key = "sk-self-hosted"
    cfg.providers.vllm.api_base = "http://localhost:8000/v1"
    active = resolve_active_provider(cfg)
    assert active is not None and active.key == "vllm" and active.api_key == "sk-self-hosted"
    assert active.api_base == "http://localhost:8000/v1"


def test_config_legacy_helpers_active_vllm_beats_leftover_cloud_key(
    tmp_path, monkeypatch
) -> None:
    """A leftover cloud key in another slot must not override an explicit
    active=vllm in the legacy get_api_base helper."""
    cfg = _isolated_config(tmp_path, monkeypatch)
    cfg.providers.active = "vllm"
    cfg.providers.vllm.api_base = "http://localhost:8000/v1"
    cfg.providers.openrouter.api_key = "sk-or-leftover"

    assert cfg.get_api_base() == "http://localhost:8000/v1"
    assert cfg.get_active_provider_name() == "vllm"


# ── is_local_endpoint + local-aware request timeout ──


def test_is_local_endpoint_classification() -> None:
    from flowly.integrations.active_provider import is_local_endpoint

    for url in (
        "http://127.0.0.1:11434/v1",
        "http://localhost:1234/v1",
        "http://192.168.1.50:8000/v1",
        "http://10.0.0.5:8000/v1",
        "http://172.16.3.4:8000/v1",
        "http://100.64.0.1:11434/v1",    # Tailscale CGNAT
        "http://100.100.100.100:8000/v1",  # still inside 100.64.0.0/10
        "http://myserver:1234/v1",       # bare LAN hostname
        "http://ollama.local/v1",
    ):
        assert is_local_endpoint(url) is True, url

    for url in (
        "https://openrouter.ai/api/v1",
        "https://api.openai.com/v1",
        "https://my-vllm.example.com/v1",
        "",
        None,
    ):
        assert is_local_endpoint(url) is False, url


def test_build_provider_bumps_timeout_for_local_endpoint() -> None:
    from flowly.integrations.active_provider import ActiveProvider
    from flowly.providers.factory import build_provider, _LOCAL_DEFAULT_TIMEOUT_SECONDS

    active = ActiveProvider(
        key="vllm", api_key="local",
        api_base="http://127.0.0.1:1234/v1", source="Local",
    )
    provider = build_provider(active, default_model="qwen3-8b")
    assert provider.request_timeout_seconds == _LOCAL_DEFAULT_TIMEOUT_SECONDS


def test_build_provider_keeps_default_timeout_for_cloud() -> None:
    from flowly.integrations.active_provider import ActiveProvider
    from flowly.providers.factory import build_provider, _LOCAL_DEFAULT_TIMEOUT_SECONDS

    active = ActiveProvider(
        key="openrouter", api_key="sk-or-x",
        api_base="https://openrouter.ai/api/v1", source="BYOK",
    )
    provider = build_provider(active, default_model="anthropic/claude-haiku-4.5")
    # Cloud must NOT get the local bump — provider falls back to its own
    # (cloud-tuned) default, which is lower.
    assert provider.request_timeout_seconds != _LOCAL_DEFAULT_TIMEOUT_SECONDS


def test_build_provider_explicit_config_timeout_wins_over_local_bump() -> None:
    from types import SimpleNamespace
    from flowly.integrations.active_provider import ActiveProvider
    from flowly.providers.factory import build_provider

    active = ActiveProvider(
        key="vllm", api_key="local",
        api_base="http://127.0.0.1:1234/v1", source="Local",
    )
    cfg = SimpleNamespace(
        agents=SimpleNamespace(defaults=SimpleNamespace(llm_timeout_seconds=45.0))
    )
    provider = build_provider(active, default_model="qwen3-8b", config=cfg)
    assert provider.request_timeout_seconds == 45.0


def test_build_provider_local_bump_fires_with_stock_config() -> None:
    """Regression: the real runtime ALWAYS passes a config, and
    llm_timeout_seconds carries a concrete stock default (not None). The stock
    default must be treated as "unset" so the local bump still applies — else
    the feature is dead in production (every real call passes config=stock)."""
    from types import SimpleNamespace
    from flowly.integrations.active_provider import ActiveProvider
    from flowly.providers.factory import (
        build_provider, _LOCAL_DEFAULT_TIMEOUT_SECONDS, _STOCK_LLM_TIMEOUT,
    )

    active = ActiveProvider(
        key="vllm", api_key="local",
        api_base="http://127.0.0.1:1234/v1", source="Local",
    )
    cfg = SimpleNamespace(
        agents=SimpleNamespace(
            defaults=SimpleNamespace(llm_timeout_seconds=int(_STOCK_LLM_TIMEOUT))
        )
    )
    provider = build_provider(active, default_model="qwen3-8b", config=cfg)
    assert provider.request_timeout_seconds == _LOCAL_DEFAULT_TIMEOUT_SECONDS


def test_build_provider_cloud_stock_config_gets_no_local_bump() -> None:
    from types import SimpleNamespace
    from flowly.integrations.active_provider import ActiveProvider
    from flowly.providers.factory import build_provider, _LOCAL_DEFAULT_TIMEOUT_SECONDS, _STOCK_LLM_TIMEOUT

    active = ActiveProvider(
        key="openrouter", api_key="sk-or-x",
        api_base="https://openrouter.ai/api/v1", source="BYOK",
    )
    cfg = SimpleNamespace(
        agents=SimpleNamespace(
            defaults=SimpleNamespace(llm_timeout_seconds=int(_STOCK_LLM_TIMEOUT))
        )
    )
    provider = build_provider(active, default_model="anthropic/claude-haiku-4.5", config=cfg)
    # Cloud on the stock default must NOT get the local bump.
    assert provider.request_timeout_seconds != _LOCAL_DEFAULT_TIMEOUT_SECONDS


def test_build_provider_cloud_stock_config_keeps_the_configured_timeout() -> None:
    """The local bump must not change any cloud provider: on the stock config
    a cloud provider gets exactly the configured value, as before local
    providers existed (the ChatGPT subscription's own default is 180 s, and a
    stock config has always pinned it to 120)."""
    from types import SimpleNamespace
    from flowly.integrations.active_provider import ActiveProvider
    from flowly.providers.factory import build_provider, _STOCK_LLM_TIMEOUT

    cfg = SimpleNamespace(
        agents=SimpleNamespace(
            defaults=SimpleNamespace(llm_timeout_seconds=int(_STOCK_LLM_TIMEOUT))
        )
    )
    for active in (
        ActiveProvider(key="openrouter", api_key="sk-or-x", api_base="https://openrouter.ai/api/v1", source="BYOK"),
        ActiveProvider(key="openai_codex", api_key="tok", api_base="https://chatgpt.com/backend-api/codex", source="Sub"),
    ):
        provider = build_provider(active, default_model="m", config=cfg)
        assert provider.request_timeout_seconds == _STOCK_LLM_TIMEOUT, active.key


def test_a_cloud_provider_behind_a_local_proxy_keeps_its_timeout() -> None:
    """A cloud provider pointed at this machine (the Flowly server in
    development, a LiteLLM proxy) is not a local model: no local bump."""
    from types import SimpleNamespace
    from flowly.integrations.active_provider import ActiveProvider
    from flowly.providers.factory import build_provider, _STOCK_LLM_TIMEOUT

    cfg = SimpleNamespace(agents=SimpleNamespace(defaults=SimpleNamespace(llm_timeout_seconds=int(_STOCK_LLM_TIMEOUT))))
    active = ActiveProvider(key="openai", api_key="sk-x", api_base="http://localhost:4000/v1", source="BYOK")
    provider = build_provider(active, default_model="gpt-5", config=cfg)
    assert provider.request_timeout_seconds == _STOCK_LLM_TIMEOUT
