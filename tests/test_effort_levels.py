"""The levels each model takes, from where its provider says them (model-settings.md).

Before: an agent's level on a model that lacked it fell back to the model's
default (``ultra`` on a model that stops at ``max`` ran at ``medium``), changing
models wiped the saved level, OpenRouter and Flowly offered the same three
levels on every model whatever it took, xAI got no level at all, and the
ChatGPT catalogue offline showed no levels. Now a level is lowered to the
nearest one the model takes and the setting is kept, OpenRouter's per-model
``reasoning`` object is read, Grok takes its family's levels (and a model that
refuses the field is asked again without it), and the last live ChatGPT
catalogue is kept on disk for its account.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from openai import BadRequestError

import flowly.integrations.model_catalog as catalog
from flowly.providers import openrouter_provider, xai_responses_provider
from flowly.providers.base import LLMResponse
from flowly.providers.effort import clamp_effort, openrouter_efforts, xai_efforts
from flowly.providers.openrouter_provider import OpenRouterProvider
from flowly.providers.xai_responses_provider import XAIResponsesProvider

CODEX = ["low", "medium", "high", "xhigh", "max"]


@pytest.fixture(autouse=True)
def _fresh_state(monkeypatch):
    monkeypatch.setattr(openrouter_provider, "_REASONING_MANDATORY_MODELS", set())
    monkeypatch.setattr(openrouter_provider, "_EFFORT_REFUSED_MODELS", set(), raising=False)
    monkeypatch.setattr(xai_responses_provider, "_EFFORT_REFUSED_MODELS", set())
    monkeypatch.setattr(catalog, "_CACHE", {})
    monkeypatch.setattr(catalog, "_FLOWLY_POLICY", None)


# ── lowering a level ──────────────────────────────────────────────────────


def test_a_level_the_model_lacks_is_lowered_never_raised():
    assert clamp_effort("ultra", CODEX) == "max"
    assert clamp_effort("xhigh", ["low", "medium", "high", "max"]) == "high"
    assert clamp_effort("high", CODEX) == "high"


def test_with_nothing_weaker_it_is_the_models_weakest():
    assert clamp_effort("minimal", ["low", "high"]) == "low"


def test_an_enabled_ask_never_lands_on_off_and_off_on_a_model_that_always_thinks_is_its_weakest():
    assert clamp_effort("minimal", ["none", "low", "high"]) == "low"
    assert clamp_effort("none", ["low", "medium", "high", "xhigh"]) == "low"


def test_nothing_known_or_nothing_asked_is_left_alone():
    assert clamp_effort("xhigh", None) == "xhigh"
    assert clamp_effort("xhigh", []) == "xhigh"
    assert clamp_effort("", CODEX) == ""


# ── OpenRouter's per-model levels (shapes seen live, 2026-11-02) ─────────


def test_an_openrouter_row_says_its_own_levels_default_and_whether_it_thinks_unasked():
    claude = {"reasoning": {"mandatory": False, "default_enabled": False, "default_effort": "high",
                            "supported_efforts": ["max", "xhigh", "high", "medium", "low", "none"]}}
    assert openrouter_efforts(claude) == (["none", "low", "medium", "high", "xhigh", "max"], "high", False)
    grok = {"reasoning": {"mandatory": True, "default_enabled": True, "default_effort": "high",
                          "supported_efforts": ["xhigh", "high", "medium", "low"]}}
    assert openrouter_efforts(grok) == (["low", "medium", "high", "xhigh"], "high", True)


def test_a_model_that_always_thinks_offers_no_off_even_if_listed():
    row = {"reasoning": {"mandatory": True, "supported_efforts": ["high", "low", "none"], "default_effort": "low"}}
    assert openrouter_efforts(row)[0] == ["low", "high"]


def test_null_levels_are_every_gateway_level_and_absent_ones_none():
    assert openrouter_efforts({"reasoning": {"supported_efforts": None}})[0] == [
        "none", "minimal", "low", "medium", "high", "xhigh", "max"]
    assert openrouter_efforts({"reasoning": {"mandatory": False}}) == ([], None, True)
    assert openrouter_efforts({"reasoning": {"supported_efforts": ["none"]}}) == ([], None, True)


def test_a_row_without_the_object_keeps_the_old_reading():
    assert openrouter_efforts({"supported_parameters": ["tools", "reasoning"]}) == (
        ["none", "low", "medium", "high"], "medium", True)
    assert openrouter_efforts({"supported_parameters": ["tools"]}) == ([], None, True)


def test_a_missing_default_is_medium_or_the_nearest_below_it():
    assert openrouter_efforts({"reasoning": {"supported_efforts": ["max", "high", "low"]}})[1] == "low"
    assert openrouter_efforts({"reasoning": {"supported_efforts": ["high", "medium"]}})[1] == "medium"


def test_flowly_rows_carry_whether_the_model_thinks_unasked():
    row = catalog._flowly_catalog_row({"id": "anthropic/claude-opus-4.7", "reasoning": {
        "default_enabled": False, "default_effort": "high", "supported_efforts": ["high", "low", "none"]}})
    assert (row.efforts, row.default_effort, row.default_thinking) == (["none", "low", "high"], "high", False)


# ── requests ──────────────────────────────────────────────────────────────


def _openai_compatible(name: str, effort: str, refuse: str | None = None):
    provider = OpenRouterProvider(api_key="test-key", provider_name=name)
    provider.reasoning_effort = effort
    sent: list[dict[str, Any]] = []

    async def create(**kwargs: Any) -> object:
        sent.append(json.loads(json.dumps(kwargs, default=str)))
        if refuse and refuse in json.dumps(kwargs.get("extra_body", {})):
            raise BadRequestError(
                f"Error code: 400 - Argument not supported on this model: {refuse}",
                response=httpx.Response(400, request=httpx.Request("POST", "https://x")), body=None,
            )
        return object()

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    provider._client_for_key = lambda _key: SimpleNamespace(with_options=lambda **_: client)  # type: ignore[method-assign]
    provider._parse_response = lambda _response: LLMResponse(content="ok")  # type: ignore[method-assign]
    return provider, sent


async def _turn(provider, model: str, reasoning: bool | None = None):
    return await provider.chat(messages=[{"role": "user", "content": "hi"}], model=model, reasoning=reasoning)


@pytest.mark.asyncio
async def test_openrouter_turns_are_lowered_to_what_the_catalogue_says_the_model_takes(monkeypatch):
    monkeypatch.setitem(catalog._CACHE, "openrouter", [
        catalog.Model(id="x-ai/grok-4.6", name="Grok", efforts=["low", "medium", "high", "xhigh"]),
        catalog.Model(id="a/plain", name="Plain", efforts=[]),
    ])
    provider, sent = _openai_compatible("openrouter", "max")
    await _turn(provider, "x-ai/grok-4.6")
    assert sent[-1]["extra_body"] == {"reasoning": {"effort": "xhigh"}}
    await _turn(provider, "a/plain")  # exposes no effort: nothing sent
    assert "extra_body" not in sent[-1]
    await _turn(provider, "a/unknown")  # not in the catalogue: as set
    assert sent[-1]["extra_body"] == {"reasoning": {"effort": "max"}}


@pytest.mark.asyncio
async def test_off_on_a_model_that_always_thinks_is_its_weakest_level_and_background_calls_switch_off(monkeypatch):
    monkeypatch.setitem(catalog._CACHE, "openrouter", [
        catalog.Model(id="x-ai/grok-4.6", name="Grok", efforts=["low", "medium", "high", "xhigh"]),
    ])
    provider, sent = _openai_compatible("openrouter", "none")
    await _turn(provider, "x-ai/grok-4.6")
    assert sent[-1]["extra_body"] == {"reasoning": {"effort": "low"}}
    await _turn(provider, "x-ai/grok-4.6", reasoning=False)
    assert sent[-1]["extra_body"] == {"reasoning": {"enabled": False}}


@pytest.mark.asyncio
async def test_xai_turns_carry_reasoning_effort_in_the_family_levels():
    provider, sent = _openai_compatible("xai", "ultra")
    await _turn(provider, "grok-4.6")
    assert sent[-1]["extra_body"] == {"reasoning_effort": "xhigh"}
    await _turn(provider, "grok-4.20-reasoning")
    assert sent[-1]["extra_body"] == {"reasoning_effort": "high"}
    await _turn(provider, "grok-4-fast-non-reasoning")
    assert "extra_body" not in sent[-1]


@pytest.mark.asyncio
async def test_xai_background_calls_and_off_send_nothing_grok_cannot_switch_off():
    provider, sent = _openai_compatible("xai", "high")
    await _turn(provider, "grok-4.6", reasoning=False)
    assert "extra_body" not in sent[-1]
    off, sent_off = _openai_compatible("xai", "none")
    await _turn(off, "grok-4.6")
    assert sent_off[-1]["extra_body"] == {"reasoning_effort": "low"}


@pytest.mark.asyncio
async def test_an_xai_model_that_refuses_the_level_is_asked_again_without_it_and_remembered():
    provider, sent = _openai_compatible("xai", "high", refuse="reasoning_effort")
    result = await _turn(provider, "grok-4.20-reasoning")
    assert result.content == "ok"
    assert sent[0]["extra_body"] == {"reasoning_effort": "high"} and "extra_body" not in sent[1]
    await _turn(provider, "grok-4.20-reasoning")
    assert "extra_body" not in sent[2]


@pytest.mark.asyncio
async def test_a_streamed_xai_turn_carries_the_level_too():
    provider, sent = _openai_compatible("xai", "medium")
    async for _ in provider.chat_stream(messages=[{"role": "user", "content": "hi"}], model="grok-4.6"):
        break
    assert sent[0]["extra_body"] == {"reasoning_effort": "medium"}


class _Answer:
    def __init__(self, status: int, body: Any):
        self.status_code = status
        self._body = body
        self.text = json.dumps(body)

    def json(self) -> Any:
        return self._body


def _xai_signed_in(monkeypatch, refuse: bool):
    provider = XAIResponsesProvider(api_key="tok", api_base="https://api.x.ai/v1", provider_name="xai_oauth")
    provider.reasoning_effort = "ultra"
    sent: list[dict[str, Any]] = []

    class _Client:
        def __init__(self, *_, **__):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

        async def post(self, _url, json, headers):
            sent.append(dict(json))
            if refuse and "reasoning" in json:
                return _Answer(400, {"error": "reasoning is not supported by this model"})
            return _Answer(200, {"status": "completed", "output": [], "output_text": "ok"})

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    return provider, sent


@pytest.mark.asyncio
async def test_signed_in_xai_turns_carry_reasoning_effort(monkeypatch):
    provider, sent = _xai_signed_in(monkeypatch, refuse=False)
    result = await provider.chat(messages=[{"role": "user", "content": "hi"}], model="grok-4.6")
    assert result.content == "ok" and sent[0]["reasoning"] == {"effort": "xhigh"}


@pytest.mark.asyncio
async def test_signed_in_xai_refusing_the_level_is_asked_again_without_it_and_remembered(monkeypatch):
    provider, sent = _xai_signed_in(monkeypatch, refuse=True)
    result = await provider.chat(messages=[{"role": "user", "content": "hi"}], model="grok-4.20-reasoning")
    assert result.content == "ok"
    assert "reasoning" in sent[0] and "reasoning" not in sent[1]
    await provider.chat(messages=[{"role": "user", "content": "again"}], model="grok-4.20-reasoning")
    assert "reasoning" not in sent[2]


def test_xai_rows_take_their_family_levels():
    rows = {row.id: row for row in catalog._xai_models_from_payload([
        {"id": "grok-4.6"}, {"id": "grok-4.20-reasoning"}, {"id": "grok-4-fast-non-reasoning"}, {"id": "grok-imagine-image"},
    ])}
    assert rows["grok-4.6"].efforts == ["low", "medium", "high", "xhigh"]
    assert rows["grok-4.20-reasoning"].efforts == ["low", "medium", "high"]
    assert rows["grok-4-fast-non-reasoning"].efforts == []
    assert "grok-imagine-image" not in rows
    assert xai_efforts("gpt-5") == ([], None)


# ── the ChatGPT catalogue kept for its account ───────────────────────────


@pytest.fixture
def flowly_home(tmp_path, monkeypatch):
    monkeypatch.setattr("flowly.profile.get_flowly_home", lambda: tmp_path)
    return tmp_path


def _codex_payload() -> dict:
    return {"models": [{"slug": "gpt-5.6-terra", "visibility": "list", "priority": 1,
                        "supported_reasoning_levels": [{"effort": level} for level in CODEX + ["ultra"]],
                        "default_reasoning_level": "medium"}]}


def _codex_backend(monkeypatch, answer):
    creds = SimpleNamespace(api_key="tok", account_id="acct-1", base_url="https://chatgpt.com/backend-api/codex")
    monkeypatch.setattr("flowly.auth.openai_codex.resolve_runtime_credentials", lambda **_: creds)

    class _Client:
        def __init__(self, *_, **__):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

        async def get(self, *_, **__):
            return answer()

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    return creds


def _ok():
    return httpx.Response(200, json=_codex_payload(), request=httpx.Request("GET", "https://x"))


def _down():
    raise httpx.ConnectError("offline")


@pytest.mark.asyncio
async def test_the_last_live_catalogue_serves_its_account_when_the_backend_is_down(flowly_home, monkeypatch):
    _codex_backend(monkeypatch, _ok)
    live = await catalog._fetch_openai_codex()
    assert live[0].efforts == CODEX + ["ultra"]
    kept = json.loads((flowly_home / "codex_models_cache.json").read_text())
    assert "acct-1" not in json.dumps(kept)  # only a digest of the account
    _codex_backend(monkeypatch, _down)
    offline = await catalog._fetch_openai_codex()
    assert [row.id for row in offline] == ["gpt-5.6-terra"] and offline[0].efforts == CODEX + ["ultra"]


@pytest.mark.asyncio
async def test_another_accounts_catalogue_is_never_served(flowly_home, monkeypatch):
    _codex_backend(monkeypatch, _ok)
    await catalog._fetch_openai_codex()
    creds = _codex_backend(monkeypatch, _down)
    creds.account_id = "acct-2"
    offline = await catalog._fetch_openai_codex()
    assert offline and all(not row.efforts for row in offline)  # the curated fallback


@pytest.mark.asyncio
async def test_model_list_says_whether_each_model_thinks_unasked(monkeypatch):
    from flowly.channels import feature_rpc
    from flowly.config.schema import Config
    from flowly.integrations.active_provider import ActiveProvider

    monkeypatch.setattr("flowly.config.loader.load_config", lambda: Config())
    monkeypatch.setattr("flowly.integrations.active_provider.resolve_active_provider",
                        lambda _cfg: ActiveProvider(key="openrouter", api_key="k", api_base=None, source="test"))
    monkeypatch.setitem(catalog._CACHE, "openrouter", [
        catalog.Model(id="a/claude", name="Claude", efforts=["none", "low", "high"], default_effort="high",
                      default_thinking=False),
    ])
    row = (await feature_rpc.model_list({}))["models"][0]
    assert (row["efforts"], row["defaultEffort"], row["defaultThinking"]) == (["none", "low", "high"], "high", False)
