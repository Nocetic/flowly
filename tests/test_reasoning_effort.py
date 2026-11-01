"""Reasoning effort, set per agent (flowly/providers/effort.py).

Before: the ChatGPT subscription always ran at ``medium`` (the only way to
change it was a suffix on the model name), Flowly credit and OpenRouter could
only switch thinking off, and Anthropic got nothing. The apps had no field to
set. Now ``agents.defaults.reasoningEffort`` reaches every provider that takes
a level, the catalogue says which levels each model takes, and the free Flowly
plan's credit stops at ``medium``.
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
from flowly.providers.anthropic_provider import AnthropicProvider
from flowly.providers.base import LLMResponse
from flowly.providers.codex_responses_provider import CodexResponsesProvider
from flowly.providers.effort import (
    anthropic_efforts,
    capped,
    normalize_effort,
    ordered_efforts,
)
from flowly.providers.factory import build_provider
from flowly.providers.openrouter_provider import OpenRouterProvider


@pytest.fixture(autouse=True)
def _fresh_state(monkeypatch):
    monkeypatch.setattr(openrouter_provider, "_REASONING_MANDATORY_MODELS", set())
    monkeypatch.setattr(openrouter_provider, "_EFFORT_REFUSED_MODELS", set(), raising=False)
    monkeypatch.setattr(catalog, "_CACHE", {})
    monkeypatch.setattr(catalog, "_FLOWLY_POLICY", None)


# ── the levels ────────────────────────────────────────────────────────────


def test_levels_are_known_lower_case_and_ordered():
    assert normalize_effort(" High ") == "high"
    assert normalize_effort("turbo") == ""
    assert normalize_effort(None) == ""
    assert ordered_efforts([{"effort": "max"}, {"effort": "low"}, "ULTRA", {"effort": "warp"}, "low"]) == [
        "low", "max", "ultra",
    ]
    assert capped("xhigh", "medium") == "medium"
    assert capped("low", "medium") == "low"
    assert capped("max", None) == "max"


@pytest.mark.parametrize(
    ("model", "levels", "default"),
    [
        ("claude-opus-5-5", ["low", "medium", "high", "xhigh", "max"], "medium"),
        ("anthropic/claude-haiku-5.5", ["low", "medium", "high", "xhigh", "max"], "medium"),
        ("claude-sonnet-5-5", ["low", "medium", "high", "xhigh", "max"], "high"),
        ("claude-opus-4-7", ["low", "medium", "high", "xhigh", "max"], "high"),
        ("claude-opus-4-6", ["low", "medium", "high", "max"], "high"),
        ("claude-opus-4-5-20251101", ["low", "medium", "high"], "high"),
        ("claude-haiku-4-5", [], None),
        ("claude-sonnet-4-5", [], None),
    ],
)
def test_anthropic_levels_follow_the_model_family(model, levels, default):
    assert anthropic_efforts(model) == (levels, default)


# ── the catalogue ─────────────────────────────────────────────────────────


def test_codex_catalogue_keeps_each_models_levels_and_default():
    models = catalog._codex_models_from_payload({"models": [
        {"slug": "gpt-5.6-terra", "visibility": "list", "default_reasoning_level": "medium",
         "supported_reasoning_levels": [{"effort": e} for e in ("low", "medium", "high", "xhigh", "max", "ultra")]},
        {"slug": "gpt-5.6-luna", "visibility": "list", "default_reasoning_level": "bogus",
         "supported_reasoning_levels": [{"effort": "low"}, {"effort": "medium"}]},
        {"slug": "gpt-plain", "visibility": "list"},
    ]})
    by_id = {m.id: m for m in models}
    assert by_id["gpt-5.6-terra"].efforts == ["low", "medium", "high", "xhigh", "max", "ultra"]
    assert by_id["gpt-5.6-terra"].default_effort == "medium"
    assert by_id["gpt-5.6-luna"].default_effort == "medium"  # an unknown default falls back
    assert by_id["gpt-plain"].efforts == [] and by_id["gpt-plain"].default_effort is None


def test_flowly_rows_take_effort_when_the_model_reasons():
    thinking = catalog._flowly_catalog_row({"id": "deepseek/r", "supported_parameters": ["tools", "reasoning"]})
    plain = catalog._flowly_catalog_row({"id": "meta/plain", "supported_parameters": ["tools"]})
    old_proxy = catalog._flowly_catalog_row({"id": "x/y"})
    assert thinking.efforts == ["none", "low", "medium", "high"] and thinking.default_effort == "medium"
    assert plain.efforts == [] and old_proxy.efforts == []


@pytest.mark.asyncio
async def test_openrouter_rows_take_effort_when_the_model_reasons(monkeypatch):
    payload = {"data": [
        {"id": "a/thinks", "supported_parameters": ["tools", "reasoning"], "pricing": {}},
        {"id": "b/plain", "supported_parameters": ["tools"], "pricing": {}},
    ]}

    class _Client:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url, **_):
            return httpx.Response(200, json=payload, request=httpx.Request("GET", url))

    monkeypatch.setattr(catalog.httpx, "AsyncClient", _Client)
    by_id = {m.id: m for m in await catalog._fetch_openrouter()}
    assert by_id["a/thinks"].efforts == ["none", "low", "medium", "high"]
    assert by_id["b/plain"].efforts == []


@pytest.mark.asyncio
async def test_anthropic_catalogue_gets_the_family_levels(monkeypatch):
    from flowly.integrations import models_dev

    async def rows(_key):
        return [catalog.Model(id="claude-opus-4-7", name="Opus"), catalog.Model(id="claude-haiku-4-5", name="Haiku")]

    monkeypatch.setattr(models_dev, "fetch_provider_models", rows)
    by_id = {m.id: m for m in await catalog.fetch_models("anthropic")}
    assert by_id["claude-opus-4-7"].efforts[-1] == "max"
    assert by_id["claude-haiku-4-5"].efforts == []


def test_only_the_free_flowly_plan_is_capped(monkeypatch):
    assert catalog.flowly_effort_cap() is None  # plan not known yet
    monkeypatch.setattr(catalog, "_FLOWLY_POLICY", catalog.FlowlyModelPolicy(plan="free", default_model="m"))
    assert catalog.flowly_effort_cap() == "medium"
    monkeypatch.setattr(catalog, "_FLOWLY_POLICY", catalog.FlowlyModelPolicy(plan="pro", default_model="m"))
    assert catalog.flowly_effort_cap() is None


# ── requests ──────────────────────────────────────────────────────────────


def _openrouter(name: str, effort: str, refuse_effort: bool = False):
    provider = OpenRouterProvider(api_key="test-key", provider_name=name)
    provider.reasoning_effort = effort
    sent: list[dict[str, Any]] = []

    async def create(**kwargs: Any) -> object:
        sent.append(json.loads(json.dumps(kwargs, default=str)))
        if refuse_effort and "effort" in kwargs.get("extra_body", {}).get("reasoning", {}):
            raise BadRequestError(
                "Error code: 400 - reasoning effort is not supported for this model",
                response=httpx.Response(400, request=httpx.Request("POST", "https://x")), body=None,
            )
        return object()

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    provider._client_for_key = lambda _key: SimpleNamespace(with_options=lambda **_: client)  # type: ignore[method-assign]
    provider._parse_response = lambda _response: LLMResponse(content="ok")  # type: ignore[method-assign]
    return provider, sent


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["openrouter", "flowly"])
async def test_openrouter_and_flowly_turns_carry_the_agents_effort(name):
    provider, sent = _openrouter(name, "high")
    await provider.chat(messages=[{"role": "user", "content": "hi"}], model="a/m")
    assert sent[0]["extra_body"] == {"reasoning": {"effort": "high"}}


@pytest.mark.asyncio
async def test_background_calls_keep_switching_thinking_off():
    provider, sent = _openrouter("flowly", "high")
    await provider.chat(messages=[{"role": "user", "content": "hi"}], model="a/m", reasoning=False)
    assert sent[0]["extra_body"] == {"reasoning": {"enabled": False}}


@pytest.mark.asyncio
async def test_no_effort_sends_no_field_and_other_backends_never_get_one():
    unset, sent_unset = _openrouter("flowly", "")
    await unset.chat(messages=[{"role": "user", "content": "hi"}], model="a/m")
    other, sent_other = _openrouter("deepseek", "high")
    await other.chat(messages=[{"role": "user", "content": "hi"}], model="a/m")
    assert "extra_body" not in sent_unset[0] and "extra_body" not in sent_other[0]


@pytest.mark.asyncio
async def test_the_free_flowly_plan_runs_at_most_medium(monkeypatch):
    monkeypatch.setattr(catalog, "_FLOWLY_POLICY", catalog.FlowlyModelPolicy(plan="free", default_model="m"))
    flowly, sent_flowly = _openrouter("flowly", "high")
    await flowly.chat(messages=[{"role": "user", "content": "hi"}], model="a/m")
    own_key, sent_own = _openrouter("openrouter", "high")
    await own_key.chat(messages=[{"role": "user", "content": "hi"}], model="a/m")
    assert sent_flowly[0]["extra_body"] == {"reasoning": {"effort": "medium"}}
    assert sent_own[0]["extra_body"] == {"reasoning": {"effort": "high"}}  # own key: never capped


@pytest.mark.asyncio
async def test_a_model_that_refuses_effort_is_asked_again_without_it():
    provider, sent = _openrouter("openrouter", "high", refuse_effort=True)
    result = await provider.chat(messages=[{"role": "user", "content": "hi"}], model="a/m")
    assert result.content == "ok"
    assert sent[0]["extra_body"] == {"reasoning": {"effort": "high"}}
    assert "extra_body" not in sent[1]
    await provider.chat(messages=[{"role": "user", "content": "again"}], model="a/m")
    assert "extra_body" not in sent[2]  # remembered


@pytest.mark.asyncio
async def test_streamed_turns_carry_the_effort_too():
    provider, sent = _openrouter("openrouter", "low")
    async for _ in provider.chat_stream(messages=[{"role": "user", "content": "hi"}], model="a/m"):
        break
    assert sent[0]["extra_body"] == {"reasoning": {"effort": "low"}}


def _codex_effort(provider: CodexResponsesProvider, model: str, effort: str) -> str:
    payload, _ = provider._build_payload([{"role": "user", "content": "yo"}], None, model, "auto",
                                         stream=True, effort=effort)
    return payload["reasoning"]["effort"]


def test_codex_turns_carry_the_agents_effort_over_the_old_default():
    provider = CodexResponsesProvider(api_key="tok", account_id="acct-1")
    assert _codex_effort(provider, "gpt-5.6-terra", "") == "medium"
    assert _codex_effort(provider, "gpt-5.6-terra", "ultra") == "ultra"
    assert _codex_effort(provider, "gpt-5.6-terra:high", "") == "high"  # old model-name suffix still read
    assert _codex_effort(provider, "gpt-5.6-terra:high", "low") == "low"  # the setting wins


def test_codex_falls_back_to_the_default_for_a_level_the_model_lacks(monkeypatch):
    provider = CodexResponsesProvider(api_key="tok", account_id="acct-1", provider_name="openai_codex")
    monkeypatch.setitem(catalog._CACHE, "openai_codex", [
        catalog.Model(id="gpt-5.6-luna", name="Luna", efforts=["low", "medium", "high"]),
    ])
    assert _codex_effort(provider, "gpt-5.6-luna", "ultra") == "medium"
    assert _codex_effort(provider, "gpt-5.6-luna", "high") == "high"


@pytest.mark.asyncio
async def test_codex_background_calls_do_not_use_the_agents_effort(monkeypatch):
    provider = CodexResponsesProvider(api_key="tok", account_id="acct-1")
    provider.reasoning_effort = "max"
    seen: list[str] = []

    def build(*args, **kwargs):
        seen.append(kwargs.get("effort", ""))
        raise RuntimeError("stop here")

    monkeypatch.setattr(provider, "_build_payload", build)
    await provider.chat(messages=[{"role": "user", "content": "x"}], reasoning=False)
    await provider.chat(messages=[{"role": "user", "content": "x"}])
    assert seen[0] == "" and seen[-1] == "max"


def _anthropic_payload(model: str, effort: str) -> dict:
    return AnthropicProvider(api_key="sk-ant-test")._build_payload(
        messages=[{"role": "user", "content": "hi"}], tools=None, model=model,
        max_tokens=100, temperature=0.7, tool_choice=None, effort=effort,
    )


def test_anthropic_turns_carry_effort_only_on_models_that_take_it():
    assert _anthropic_payload("claude-opus-4-7", "xhigh")["output_config"] == {"effort": "xhigh"}
    assert "output_config" not in _anthropic_payload("claude-opus-4-6", "xhigh")  # 4.6 has no xhigh
    assert "output_config" not in _anthropic_payload("claude-haiku-4-5", "low")
    assert "output_config" not in _anthropic_payload("claude-opus-4-7", "")
    assert "thinking" not in _anthropic_payload("claude-opus-4-7", "high")


def test_every_built_provider_carries_the_configured_effort():
    config = Config()
    config.agents.defaults.reasoning_effort = "High"
    active = ActiveProvider(key="openrouter", api_key="k", api_base=None, source="test")
    assert build_provider(active, default_model="a/m", config=config).reasoning_effort == "high"
    config.agents.defaults.reasoning_effort = "nonsense"
    assert build_provider(active, default_model="a/m", config=config).reasoning_effort == ""


def test_the_setting_round_trips_in_camel_case():
    config = Config()
    config.agents.defaults.reasoning_effort = "xhigh"
    on_disk = convert_to_camel(config.model_dump())
    assert on_disk["agents"]["defaults"]["reasoningEffort"] == "xhigh"
    assert Config.model_validate(convert_keys(on_disk)).agents.defaults.reasoning_effort == "xhigh"


# ── model.set / model.list / provider.active ─────────────────────────────


@pytest.fixture
def main_agent(monkeypatch):
    cfg = Config()
    cfg.agents.defaults.model = "gpt-5.6-terra"
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
        catalog.Model(id="gpt-5.6-terra", name="Terra", efforts=["low", "medium", "high", "xhigh", "max", "ultra"],
                      default_effort="medium"),
        catalog.Model(id="gpt-5.6-luna", name="Luna", efforts=["low", "medium"], default_effort="medium"),
        catalog.Model(id="gpt-plain", name="Plain"),
    ])
    return cfg, saved


@pytest.mark.asyncio
async def test_model_set_saves_an_effort_the_model_takes(main_agent):
    cfg, saved = main_agent
    result = await feature_rpc.model_set({"effort": "xhigh"})
    assert result == {"ok": True, "model": "gpt-5.6-terra", "effort": "xhigh", "effortReset": False,
                      "serviceTier": "", "serviceTierReset": False, "willRestart": False}
    assert cfg.agents.defaults.reasoning_effort == "xhigh" and saved == [cfg]
    assert feature_rpc.provider_active()["effort"] == "xhigh"


@pytest.mark.asyncio
@pytest.mark.parametrize(("params", "code"), [
    ({"effort": "warp"}, "INVALID"),
    ({"effort": 3}, "INVALID"),
    ({}, "INVALID"),
    ({"model": "gpt-5.6-luna", "effort": "high"}, "EFFORT_UNSUPPORTED"),
    ({"model": "gpt-plain", "effort": "low"}, "EFFORT_UNSUPPORTED"),
])
async def test_model_set_refuses_an_effort_it_cannot_run(main_agent, params, code):
    cfg, saved = main_agent
    with pytest.raises(feature_rpc.FeatureRpcError) as refused:
        await feature_rpc.model_set(params)
    assert refused.value.code == code
    assert saved == [] and cfg.agents.defaults.reasoning_effort == ""


@pytest.mark.asyncio
async def test_a_new_model_without_the_saved_effort_drops_it(main_agent):
    cfg, _saved = main_agent
    cfg.agents.defaults.reasoning_effort = "max"
    result = await feature_rpc.model_set({"model": "gpt-5.6-luna"})
    assert (result["model"], result["effort"], result["effortReset"]) == ("gpt-5.6-luna", "", True)
    cfg.agents.defaults.reasoning_effort = "low"
    kept = await feature_rpc.model_set({"model": "gpt-5.6-terra"})
    assert (kept["effort"], kept["effortReset"]) == ("low", False)


@pytest.mark.asyncio
async def test_an_empty_effort_goes_back_to_the_models_default(main_agent):
    cfg, _saved = main_agent
    cfg.agents.defaults.reasoning_effort = "high"
    assert (await feature_rpc.model_set({"effort": ""}))["effort"] == ""
    assert cfg.agents.defaults.reasoning_effort == ""


@pytest.mark.asyncio
async def test_a_model_the_catalogue_does_not_know_takes_any_level(main_agent):
    assert (await feature_rpc.model_set({"model": "typed/by-hand", "effort": "high"}))["effort"] == "high"


@pytest.mark.asyncio
async def test_the_free_flowly_plan_cannot_save_more_than_medium(main_agent, monkeypatch):
    monkeypatch.setattr(
        "flowly.integrations.active_provider.resolve_active_provider",
        lambda _cfg: ActiveProvider(key="flowly", api_key="flw_x", api_base=None, source="test"),
    )
    monkeypatch.setattr(catalog, "_FLOWLY_POLICY", catalog.FlowlyModelPolicy(plan="free", default_model="m"))
    monkeypatch.setitem(catalog._CACHE, "flowly", [
        catalog.Model(id="deepseek/r", name="R", efforts=["low", "medium", "high"], default_effort="medium"),
    ])
    with pytest.raises(feature_rpc.FeatureRpcError) as refused:
        await feature_rpc.model_set({"model": "deepseek/r", "effort": "high"})
    assert refused.value.code == "EFFORT_NOT_IN_PLAN"
    assert (await feature_rpc.model_set({"model": "deepseek/r", "effort": "medium"}))["effort"] == "medium"


@pytest.mark.asyncio
async def test_model_list_says_which_levels_each_model_takes(main_agent, monkeypatch):
    listed = await feature_rpc.model_list({})
    terra = next(m for m in listed["models"] if m["id"] == "gpt-5.6-terra")
    plain = next(m for m in listed["models"] if m["id"] == "gpt-plain")
    assert terra["efforts"][-1] == "ultra" and terra["defaultEffort"] == "medium"
    assert plain["efforts"] == [] and plain["defaultEffort"] is None
    assert listed["effortCap"] is None


# ── named agents ──────────────────────────────────────────────────────────


@pytest.fixture
def profile_roots(tmp_path, monkeypatch):
    default = tmp_path / ".flowly"
    root = default / "profiles"
    monkeypatch.setattr(profiles, "_DEFAULT_HOME", default)
    monkeypatch.setattr(profiles, "_PROFILES_ROOT", root)
    monkeypatch.setenv("FLOWLY_HOME", str(default))
    default.mkdir(parents=True)
    (default / "workspace").mkdir()
    (default / "config.json").write_text(json.dumps({"agents": {"defaults": {"model": "gpt-5.6-terra"}}}))
    monkeypatch.setattr(
        "flowly.integrations.active_provider.resolve_active_provider",
        lambda _cfg: ActiveProvider(key="openai_codex", api_key="k", api_base=None, source="test"),
    )
    monkeypatch.setitem(catalog._CACHE, "openai_codex", [
        catalog.Model(id="gpt-5.6-terra", name="Terra", efforts=["low", "medium", "high", "max"]),
        catalog.Model(id="gpt-5.6-luna", name="Luna", efforts=["low", "medium"]),
    ])
    return default, root


def test_a_named_agents_effort_is_its_own(profile_roots):
    default, _root = profile_roots
    created = profiles.create_profile("writer", local_runtime=True)
    settings = profiles.update_profile_settings("writer", reasoning_effort="High")
    assert settings["reasoningEffort"] == "high"
    on_disk = json.loads((created / "config.json").read_text())
    assert on_disk["agents"]["defaults"]["reasoningEffort"] == "high"
    assert "reasoningEffort" not in json.loads((default / "config.json").read_text())["agents"]["defaults"]
    with pytest.raises(ValueError, match="Unknown reasoning effort"):
        profiles.update_profile_settings("writer", reasoning_effort="warp")


@pytest.mark.asyncio
async def test_profiles_configure_checks_the_effort_against_the_agents_model(profile_roots):
    profiles.create_profile("writer", local_runtime=True)
    host = ProfileHost()

    result = await host.configure({"name": "writer", "reasoningEffort": "max"})  # follows terra
    assert result["settings"]["reasoningEffort"] == "max"

    with pytest.raises(ProfileHostError) as refused:
        await host.configure({"name": "writer", "model": "gpt-5.6-luna", "reasoningEffort": "high"})
    assert refused.value.code == "EFFORT_UNSUPPORTED"

    moved = await host.configure({"name": "writer", "model": "gpt-5.6-luna"})
    assert (moved["settings"]["model"], moved["settings"]["reasoningEffort"]) == ("gpt-5.6-luna", "")

    with pytest.raises(ProfileHostError) as unknown:
        await host.configure({"name": "writer", "reasoningEffort": "warp"})
    assert unknown.value.code == "INVALID_PARAMS"
    with pytest.raises(ProfileHostError) as main:
        await host.configure({"name": "default", "reasoningEffort": "low"})
    assert main.value.code == "DEFAULT_PROFILE_DIRECT"
