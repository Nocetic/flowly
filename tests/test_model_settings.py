"""Fast and thinking off, set per agent (model-settings.md).

Before: an agent could not ask the ChatGPT subscription for its faster tier
(the catalogue's ``service_tiers`` were never read), and Flowly credit and
OpenRouter could switch thinking off only for background calls. Now
``agents.defaults.serviceTier`` reaches the Codex request when the model offers
that tier, the catalogue says which tiers each model offers, and ``none`` is a
level on OpenRouter and Flowly rows that switches thinking off.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from openai import BadRequestError

import flowly.integrations.model_catalog as catalog
import flowly.profile as profiles
from flowly.channels import feature_rpc
from flowly.config.loader import convert_keys, convert_to_camel
from flowly.config.schema import Config
from flowly.integrations.active_provider import ActiveProvider
from flowly.profile_host import ProfileHost
from flowly.profile_host_contract import ProfileHostError
from flowly.providers import openrouter_provider
from flowly.providers.base import LLMResponse
from flowly.providers.codex_responses_provider import CodexResponsesProvider
from flowly.providers.factory import build_provider
from flowly.providers.openrouter_provider import OpenRouterProvider
from flowly.providers.service_tier import ServiceTier, normalize_service_tier, parse_service_tiers

FAST = ServiceTier(id="priority", name="Fast", description="1.5x speed, increased usage")
ULTRA = ServiceTier(id="ultrafast", name="Ultrafast")


@pytest.fixture(autouse=True)
def _fresh_state(monkeypatch):
    monkeypatch.setattr(openrouter_provider, "_REASONING_MANDATORY_MODELS", set())
    monkeypatch.setattr(openrouter_provider, "_EFFORT_REFUSED_MODELS", set(), raising=False)
    monkeypatch.setattr(catalog, "_CACHE", {})
    monkeypatch.setattr(catalog, "_FLOWLY_POLICY", None)


# ── the catalogue ─────────────────────────────────────────────────────────


def test_tiers_are_read_once_each_and_anything_else_is_dropped():
    assert normalize_service_tier(" Priority ") == "priority"
    assert normalize_service_tier("../etc") == "" and normalize_service_tier(3) == ""
    assert parse_service_tiers([
        {"id": "priority", "name": "Fast", "description": "1.5x speed, increased usage"},
        {"id": "PRIORITY", "name": "again"},
        {"id": "ultrafast"},
        {"id": "no spaces"}, {"name": "no id"}, "priority", None,
    ]) == [FAST, ServiceTier(id="ultrafast", name="ultrafast")]  # no name: its id
    assert parse_service_tiers(None) == [] and parse_service_tiers({"id": "x"}) == []


def test_codex_rows_keep_the_tiers_each_model_offers():
    rows = catalog._codex_models_from_payload({"models": [
        {"slug": "gpt-5.6-sol", "visibility": "list", "priority": 1,
         "service_tiers": [{"id": "priority", "name": "Fast", "description": "1.5x speed, increased usage"},
                           {"id": "ultrafast", "name": "Ultrafast"}]},
        {"slug": "gpt-5.6-luna", "visibility": "list", "priority": 2, "service_tiers": []},
        {"slug": "old", "visibility": "list", "priority": 3},
    ]})
    assert [(row.id, [tier.id for tier in row.service_tiers]) for row in rows] == [
        ("gpt-5.6-sol", ["priority", "ultrafast"]), ("gpt-5.6-luna", []), ("old", []),
    ]


def test_flowly_and_openrouter_rows_that_think_can_switch_thinking_off():
    thinking = catalog._flowly_catalog_row({"id": "deepseek/r", "supported_parameters": ["reasoning"]})
    assert thinking.efforts[0] == "none" and thinking.default_effort == "medium"
    assert catalog._flowly_catalog_row({"id": "x/plain", "supported_parameters": ["tools"]}).efforts == []


# ── requests ──────────────────────────────────────────────────────────────


def _codex(tier: str = "") -> CodexResponsesProvider:
    provider = CodexResponsesProvider(api_key="tok", account_id="acct-1", provider_name="openai_codex")
    provider.service_tier = tier
    return provider


def _codex_tier(provider: CodexResponsesProvider, model: str, tier: str) -> str | None:
    payload, _ = provider._build_payload([{"role": "user", "content": "yo"}], None, model, "auto",
                                         stream=True, service_tier=tier)
    return payload.get("service_tier")


def test_codex_turns_carry_the_tier_only_where_the_model_offers_it(monkeypatch):
    monkeypatch.setitem(catalog._CACHE, "openai_codex", [
        catalog.Model(id="gpt-5.6-sol", name="Sol", service_tiers=[FAST, ULTRA]),
        catalog.Model(id="gpt-5.6-luna", name="Luna"),
    ])
    provider = _codex()
    assert _codex_tier(provider, "gpt-5.6-sol", "priority") == "priority"
    assert _codex_tier(provider, "gpt-5.6-sol", "ultrafast") == "ultrafast"
    assert _codex_tier(provider, "gpt-5.6-luna", "priority") is None  # set for another model
    assert _codex_tier(provider, "gpt-5.6-sol", "") is None
    # No catalogue for the model yet: sent, and dropped if the backend refuses it.
    assert _codex_tier(provider, "gpt-unlisted", "priority") == "priority"


def test_a_refused_tier_is_dropped_for_that_model_only(monkeypatch):
    provider = _codex()
    payload, _ = provider._build_payload([{"role": "user", "content": "yo"}], None, "gpt-5.6-sol", "auto",
                                         stream=True, service_tier="priority")
    detail = '{"error": {"message": "Unsupported parameter: service_tier", "param": "service_tier"}}'
    assert provider._recover_rejected_payload(detail, model="gpt-5.6-sol", payload=payload) is True
    assert _codex_tier(provider, "gpt-5.6-sol", "priority") is None
    assert _codex_tier(provider, "gpt-5.6-terra", "priority") == "priority"
    # A refusal of something else is not taken for one of the tier.
    other = {"model": "gpt-5.6-terra", "service_tier": "priority"}
    assert provider._recover_rejected_payload("model not found", model="gpt-5.6-terra", payload=other) is False


@pytest.mark.asyncio
async def test_a_turn_whose_tier_is_refused_is_asked_again_at_standard_speed(monkeypatch):
    provider = _codex("priority")
    sent: list[dict[str, Any]] = []

    class _Response:
        def __init__(self, status: int, body: bytes = b""):
            self.status_code = status
            self._body = body

        async def aread(self) -> bytes:
            return self._body

    class _Stream:
        def __init__(self, payload: dict[str, Any]):
            sent.append(payload)
            self.payload = payload

        async def __aenter__(self):
            if "service_tier" in self.payload:
                return _Response(400, b'{"error": {"message": "Unsupported value: service_tier"}}')
            return _Response(200)

        async def __aexit__(self, *_):
            return False

    class _Client:
        def __init__(self, *_, **__):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

        def stream(self, _method, _url, json, headers):
            return _Stream(json)

    async def aggregate(_response):
        return LLMResponse(content="ok")

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    monkeypatch.setattr(provider, "_aggregate_stream", aggregate)
    result = await provider.chat(messages=[{"role": "user", "content": "hi"}], model="gpt-5.6-sol")
    assert result.content == "ok"
    assert [p.get("service_tier") for p in sent] == ["priority", None]


@pytest.mark.asyncio
async def test_codex_background_calls_never_use_the_tier(monkeypatch):
    provider = _codex("priority")
    seen: list[str] = []

    def build(*_args, **kwargs):
        seen.append(kwargs.get("service_tier", ""))
        raise RuntimeError("stop here")

    monkeypatch.setattr(provider, "_build_payload", build)
    await provider.chat(messages=[{"role": "user", "content": "x"}], reasoning=False)
    await provider.chat(messages=[{"role": "user", "content": "x"}])
    assert seen[0] == "" and seen[-1] == "priority"


def test_every_built_provider_carries_the_configured_tier():
    config = Config()
    config.agents.defaults.service_tier = "Priority"
    active = ActiveProvider(key="openrouter", api_key="k", api_base=None, source="test")
    assert build_provider(active, default_model="a/m", config=config).service_tier == "priority"
    config.agents.defaults.service_tier = "no spaces"
    assert build_provider(active, default_model="a/m", config=config).service_tier == ""


def test_the_setting_round_trips_in_camel_case():
    config = Config()
    config.agents.defaults.service_tier = "priority"
    on_disk = convert_to_camel(config.model_dump())
    assert on_disk["agents"]["defaults"]["serviceTier"] == "priority"
    assert Config.model_validate(convert_keys(on_disk)).agents.defaults.service_tier == "priority"


def _openrouter(name: str, effort: str, mandatory: bool = False):
    provider = OpenRouterProvider(api_key="test-key", provider_name=name)
    provider.reasoning_effort = effort
    sent: list[dict[str, Any]] = []

    async def create(**kwargs: Any) -> object:
        sent.append(json.loads(json.dumps(kwargs, default=str)))
        if mandatory and kwargs.get("extra_body", {}).get("reasoning") == {"enabled": False}:
            raise BadRequestError(
                "Error code: 400 - Reasoning is mandatory for this endpoint and cannot be disabled",
                response=httpx.Response(400, request=httpx.Request("POST", "https://x")), body=None,
            )
        return object()

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    provider._client_for_key = lambda _key: SimpleNamespace(with_options=lambda **_: client)  # type: ignore[method-assign]
    provider._parse_response = lambda _response: LLMResponse(content="ok")  # type: ignore[method-assign]
    return provider, sent


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["openrouter", "flowly"])
async def test_thinking_off_reaches_openrouter_and_flowly_as_the_switch(name, monkeypatch):
    monkeypatch.setattr(catalog, "_FLOWLY_POLICY", catalog.FlowlyModelPolicy(plan="free", default_model="m"))
    provider, sent = _openrouter(name, "none")
    await provider.chat(messages=[{"role": "user", "content": "hi"}], model="a/m")
    assert sent[0]["extra_body"] == {"reasoning": {"enabled": False}}


@pytest.mark.asyncio
async def test_a_model_that_always_thinks_is_asked_again_without_the_switch():
    provider, sent = _openrouter("openrouter", "none", mandatory=True)
    result = await provider.chat(messages=[{"role": "user", "content": "hi"}], model="a/m")
    assert result.content == "ok"
    assert sent[0]["extra_body"] == {"reasoning": {"enabled": False}} and "extra_body" not in sent[1]


# ── model.set / model.list / provider.active ─────────────────────────────


@pytest.fixture
def main_agent(monkeypatch):
    cfg = Config()
    cfg.agents.defaults.model = "gpt-5.6-sol"
    saved: list[Config] = []
    monkeypatch.setattr("flowly.config.loader.load_config", lambda: cfg)
    monkeypatch.setattr("flowly.config.loader.save_config", saved.append)
    monkeypatch.setattr(
        "flowly.integrations.active_provider.resolve_active_provider",
        lambda _cfg: ActiveProvider(key="openai_codex", api_key="k", api_base=None, source="test"),
    )

    async def reloaded():
        return {"ok": True}

    monkeypatch.setattr(feature_rpc, "_provider_reload_cb", reloaded)
    monkeypatch.setitem(catalog._CACHE, "openai_codex", [
        catalog.Model(id="gpt-5.6-sol", name="Sol", efforts=["low", "medium"], service_tiers=[FAST, ULTRA]),
        catalog.Model(id="gpt-5.6-terra", name="Terra", efforts=["low", "medium"], service_tiers=[FAST]),
        catalog.Model(id="gpt-5.6-luna", name="Luna", efforts=["low", "medium"]),
    ])
    return cfg, saved


@pytest.mark.asyncio
async def test_model_set_saves_a_tier_the_model_offers(main_agent):
    cfg, saved = main_agent
    result = await feature_rpc.model_set({"serviceTier": "Priority"})
    assert (result["serviceTier"], result["serviceTierReset"], result["model"]) == ("priority", False, "gpt-5.6-sol")
    assert cfg.agents.defaults.service_tier == "priority" and saved == [cfg]
    assert feature_rpc.provider_active()["serviceTier"] == "priority"
    back = await feature_rpc.model_set({"serviceTier": ""})
    assert back["serviceTier"] == "" and cfg.agents.defaults.service_tier == ""


@pytest.mark.asyncio
@pytest.mark.parametrize(("params", "code"), [
    ({"serviceTier": 3}, "INVALID"),
    ({"serviceTier": "no spaces"}, "INVALID"),
    ({"serviceTier": "flex"}, "SERVICE_TIER_UNSUPPORTED"),
    ({"model": "gpt-5.6-luna", "serviceTier": "priority"}, "SERVICE_TIER_UNSUPPORTED"),
    ({"model": "gpt-unlisted", "serviceTier": "priority"}, "SERVICE_TIER_UNSUPPORTED"),
])
async def test_model_set_refuses_a_tier_the_model_does_not_offer(main_agent, params, code):
    cfg, saved = main_agent
    with pytest.raises(feature_rpc.FeatureRpcError) as refused:
        await feature_rpc.model_set(params)
    assert refused.value.code == code
    assert saved == [] and cfg.agents.defaults.model == "gpt-5.6-sol"


@pytest.mark.asyncio
async def test_a_new_model_keeps_the_tier_it_offers_and_drops_one_it_lacks(main_agent):
    cfg, _saved = main_agent
    await feature_rpc.model_set({"serviceTier": "priority"})
    kept = await feature_rpc.model_set({"model": "gpt-5.6-terra"})
    assert (kept["serviceTier"], kept["serviceTierReset"]) == ("priority", False)
    dropped = await feature_rpc.model_set({"model": "gpt-5.6-luna"})
    assert (dropped["serviceTier"], dropped["serviceTierReset"]) == ("", True)
    assert cfg.agents.defaults.service_tier == ""


@pytest.mark.asyncio
async def test_an_effort_change_leaves_the_tier_alone(main_agent):
    cfg, _saved = main_agent
    await feature_rpc.model_set({"serviceTier": "ultrafast"})
    result = await feature_rpc.model_set({"effort": "low"})
    assert result["serviceTier"] == "ultrafast" and cfg.agents.defaults.service_tier == "ultrafast"


@pytest.mark.asyncio
async def test_model_list_says_which_tiers_each_model_offers(main_agent):
    rows = {row["id"]: row for row in (await feature_rpc.model_list({}))["models"]}
    assert rows["gpt-5.6-sol"]["serviceTiers"] == [
        {"id": "priority", "name": "Fast", "description": "1.5x speed, increased usage"},
        {"id": "ultrafast", "name": "Ultrafast", "description": ""},
    ]
    assert rows["gpt-5.6-luna"]["serviceTiers"] == []


# ── named agents ──────────────────────────────────────────────────────────


@pytest.fixture
def profile_roots(tmp_path, monkeypatch):
    default = tmp_path / ".flowly"
    monkeypatch.setattr(profiles, "_DEFAULT_HOME", default)
    monkeypatch.setattr(profiles, "_PROFILES_ROOT", default / "profiles")
    monkeypatch.setenv("FLOWLY_HOME", str(default))
    default.mkdir(parents=True)
    (default / "workspace").mkdir()
    (default / "config.json").write_text(json.dumps({"agents": {"defaults": {"model": "gpt-5.6-sol"}}}))
    monkeypatch.setattr(
        "flowly.integrations.active_provider.resolve_active_provider",
        lambda _cfg: ActiveProvider(key="openai_codex", api_key="k", api_base=None, source="test"),
    )
    monkeypatch.setitem(catalog._CACHE, "openai_codex", [
        catalog.Model(id="gpt-5.6-sol", name="Sol", service_tiers=[FAST]),
        catalog.Model(id="gpt-5.6-luna", name="Luna"),
    ])
    return default


def test_a_named_agents_tier_is_its_own(profile_roots):
    created = profiles.create_profile("writer", local_runtime=True)
    settings = profiles.update_profile_settings("writer", service_tier="Priority")
    assert settings["serviceTier"] == "priority"
    assert json.loads((created / "config.json").read_text())["agents"]["defaults"]["serviceTier"] == "priority"
    assert "serviceTier" not in json.loads((profile_roots / "config.json").read_text())["agents"]["defaults"]
    with pytest.raises(ValueError, match="Invalid service tier"):
        profiles.update_profile_settings("writer", service_tier="no spaces")


@pytest.mark.asyncio
async def test_profiles_configure_checks_the_tier_against_the_agents_model(profile_roots):
    profiles.create_profile("writer", local_runtime=True)
    host = ProfileHost()

    result = await host.configure({"name": "writer", "serviceTier": "priority"})  # follows sol
    assert result["settings"]["serviceTier"] == "priority"

    with pytest.raises(ProfileHostError) as refused:
        await host.configure({"name": "writer", "model": "gpt-5.6-luna", "serviceTier": "priority"})
    assert refused.value.code == "SERVICE_TIER_UNSUPPORTED"

    moved = await host.configure({"name": "writer", "model": "gpt-5.6-luna"})
    assert (moved["settings"]["model"], moved["settings"]["serviceTier"]) == ("gpt-5.6-luna", "")

    with pytest.raises(ProfileHostError) as invalid:
        await host.configure({"name": "writer", "serviceTier": "no spaces"})
    assert invalid.value.code == "INVALID_PARAMS"
    with pytest.raises(ProfileHostError) as main:
        await host.configure({"name": "default", "serviceTier": "priority"})
    assert main.value.code == "DEFAULT_PROFILE_DIRECT"
