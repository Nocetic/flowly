"""Thinking on, off and its level on OpenAI (API key), Gemini, Groq and Z.AI GLM.

Before: these providers got no thinking field at all, so the apps offered no
switch and no level, whatever the model took; OpenAI's reasoning models were
also sent ``max_tokens`` and ``temperature``, which they refuse. Now the levels
come from models.dev's ``reasoning_options`` (the rows below are copied from
https://models.dev/api.json on 2026-11-06), each request carries them in the
provider's own field, and a model that refuses a field is asked again without
it.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from openai import BadRequestError

import flowly.integrations.model_catalog as catalog
from flowly.integrations import models_dev
from flowly.providers import openrouter_provider
from flowly.providers.base import LLMResponse
from flowly.providers.effort import SWITCH_LEVELS, models_dev_efforts
from flowly.providers.openrouter_provider import OpenRouterProvider


def _row(options: list | None, *, temperature: bool = True) -> dict[str, Any]:
    row: dict[str, Any] = {"tool_call": True, "reasoning": bool(options), "temperature": temperature}
    if options is not None:
        row["reasoning_options"] = options
    return row


def _effort(*values: str) -> dict[str, Any]:
    return {"type": "effort", "values": list(values)}


TOGGLE = {"type": "toggle"}

PAYLOAD = {
    "openai": {"models": {
        "gpt-4.1": _row(None),
        "gpt-5": _row([_effort("minimal", "low", "medium", "high")], temperature=False),
        "gpt-5-pro": _row([_effort("high")], temperature=False),
        "gpt-5.4": _row([_effort("none", "low", "medium", "high", "xhigh")]),
    }},
    "google": {"models": {
        "gemini-2.5-flash": _row([TOGGLE, {"type": "budget_tokens", "min": 0, "max": 24576}]),
        "gemini-2.5-pro": _row([{"type": "budget_tokens", "min": 128, "max": 32768}]),
        "gemini-3.5-flash": _row([_effort("minimal", "low", "medium", "high")]),
        "gemma-4-31b-it": _row([TOGGLE]),
    }},
    "groq": {"models": {
        "openai/gpt-oss-120b": _row([_effort("low", "medium", "high")]),
        "qwen/qwen3.6-27b": _row([_effort("none", "default")]),
        "qwen/qwen3.8-27b": _row([_effort("none", "default", "low", "medium", "high")]),
    }},
    "zai": {"models": {
        "glm-4.7": _row([TOGGLE]),
        "glm-5.2": _row([TOGGLE, _effort("high", "max")]),
        "glm-5.3": _row([_effort("low", "high", "max")]),
    }},
    "zai-coding-plan": {"models": {
        "glm-4.7": _row([TOGGLE]),
        "glm-5-turbo": _row([TOGGLE]),
        "glm-5.2": _row([TOGGLE, _effort("high", "max")]),
    }},
    "anthropic": {"models": {
        "claude-opus-4-7": _row([_effort("low", "medium", "high", "xhigh", "max")], temperature=False),
    }},
}


@pytest.fixture(autouse=True)
def _fresh_state(monkeypatch):
    monkeypatch.setattr(openrouter_provider, "_REASONING_MANDATORY_MODELS", set())
    monkeypatch.setattr(openrouter_provider, "_EFFORT_REFUSED_MODELS", set())
    monkeypatch.setattr(openrouter_provider, "_TEMPERATURE_REFUSED_MODELS", set())
    monkeypatch.setattr(catalog, "_CACHE", {})

    async def payload():
        return PAYLOAD

    monkeypatch.setattr(models_dev, "_fetch_payload", payload)


async def _catalogue(provider_key: str) -> dict[str, catalog.Model]:
    return {model.id: model for model in await catalog.fetch_models(provider_key, force_refresh=True)}


# ── what each model takes ─────────────────────────────────────────────────


@pytest.mark.parametrize(("provider", "options", "levels", "default"), [
    ("openai", [_effort("minimal", "low", "medium", "high")], ["minimal", "low", "medium", "high"], "medium"),
    ("openai", [_effort("high")], ["high"], "high"),
    ("openai", [_effort("none", "low", "medium", "high", "xhigh")], ["none", "low", "medium", "high", "xhigh"], "medium"),
    # Gemini: a budget is reached through a level; a budget from 0 can be off.
    ("gemini", [TOGGLE, {"type": "budget_tokens", "min": 0, "max": 24576}], ["none", "low", "medium", "high"], "high"),
    ("gemini", [{"type": "budget_tokens", "min": 128}], ["low", "medium", "high"], "high"),
    ("gemini", [_effort("minimal", "low", "medium", "high")], ["minimal", "low", "medium", "high"], "high"),
    # Groq's "default" is the model's own, not a level.
    ("groq", [_effort("none", "default", "low", "medium", "high")], ["none", "low", "medium", "high"], "medium"),
    ("zhipu", [TOGGLE, _effort("high", "max")], ["none", "high", "max"], "max"),
    ("zhipu", [_effort("low", "high", "max")], ["low", "high", "max"], "max"),
])
def test_levels_come_from_the_models_reasoning_options(provider, options, levels, default):
    control = models_dev_efforts(options, provider)
    assert (control.levels, control.default, control.switch_only) == (levels, default, False)


@pytest.mark.parametrize(("provider", "options"), [
    ("zhipu", [TOGGLE]),
    ("gemini", [TOGGLE]),
    ("groq", [_effort("none", "default")]),
])
def test_a_model_that_can_only_be_switched_gets_the_switch_alone(provider, options):
    control = models_dev_efforts(options, provider)
    assert (control.levels, control.default, control.switch_only) == (list(SWITCH_LEVELS), "high", True)


@pytest.mark.parametrize("options", [None, [], [{"type": "budget_tokens", "min": 1024}], "x", [_effort("default")]])
def test_nothing_usable_is_no_control(options):
    assert models_dev_efforts(options, "openai").levels == []


async def test_the_picker_rows_carry_the_levels_and_whether_temperature_is_taken():
    openai = await _catalogue("openai")
    assert openai["gpt-5"].efforts == ["minimal", "low", "medium", "high"]
    assert openai["gpt-5"].takes_temperature is False
    assert openai["gpt-5.4"].efforts[0] == "none" and openai["gpt-5.4"].takes_temperature is True
    assert openai["gpt-4.1"].efforts == []
    zhipu = await _catalogue("zhipu")
    assert zhipu["glm-4.7"].efforts == list(SWITCH_LEVELS) and zhipu["glm-4.7"].thinking_switch_only
    assert zhipu["glm-5.2"].efforts == ["none", "high", "max"]


async def test_anthropic_keeps_the_levels_of_its_model_families():
    anthropic = await _catalogue("anthropic")
    assert anthropic["claude-opus-4-7"].efforts == ["low", "medium", "high", "xhigh", "max"]
    assert anthropic["claude-opus-4-7"].default_effort == "high"


async def test_the_coding_plan_list_is_filled_in_from_its_models_dev_catalogue(monkeypatch):
    monkeypatch.setattr("flowly.auth.zai_coding.load_token_payload", lambda: SimpleNamespace(api_key="k"))
    plan = await _catalogue("zai_coding")
    assert plan["glm-5.2"].efforts == ["none", "high", "max"] and plan["glm-5.2"].default_effort == "max"
    assert plan["glm-4.7"].thinking_switch_only
    assert set(plan) == {"glm-5.2", "glm-5-turbo", "glm-4.7"}


# ── requests ──────────────────────────────────────────────────────────────


def _refusal(text: str) -> BadRequestError:
    return BadRequestError(
        f"Error code: 400 - {text}",
        response=httpx.Response(400, request=httpx.Request("POST", "https://x")), body=None,
    )


def _provider(name: str, effort: str, refuse=None):
    provider = OpenRouterProvider(api_key="test-key", provider_name=name)
    provider.reasoning_effort = effort
    sent: list[dict[str, Any]] = []

    async def create(**kwargs: Any) -> object:
        sent.append(json.loads(json.dumps(kwargs, default=str)))
        if refuse is not None:
            refused = refuse(kwargs)
            if refused:
                raise _refusal(refused)
        return object()

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    provider._client_for_key = lambda _key: SimpleNamespace(with_options=lambda **_: client)  # type: ignore[method-assign]
    provider._parse_response = lambda _response: LLMResponse(content="ok")  # type: ignore[method-assign]
    return provider, sent


async def _turn(provider, model: str, reasoning: bool | None = None):
    return await provider.chat(messages=[{"role": "user", "content": "hi"}], model=model, reasoning=reasoning)


async def _sent(name: str, effort: str, model: str, reasoning: bool | None = None) -> dict[str, Any]:
    await catalog.fetch_models(name, force_refresh=True)
    provider, sent = _provider(name, effort)
    await _turn(provider, model, reasoning)
    return sent[-1]


@pytest.mark.parametrize(("name", "effort", "model", "body"), [
    ("openai", "max", "gpt-5", {"reasoning_effort": "high"}),
    ("openai", "none", "gpt-5", {"reasoning_effort": "minimal"}),  # cannot stop thinking
    ("openai", "none", "gpt-5.4", {"reasoning_effort": "none"}),
    ("gemini", "none", "gemini-2.5-flash", {"reasoning_effort": "none"}),
    ("gemini", "xhigh", "gemini-3.5-flash", {"reasoning_effort": "high"}),
    ("gemini", "none", "gemma-4-31b-it", {"reasoning_effort": "none"}),
    ("groq", "none", "qwen/qwen3.6-27b", {"reasoning_effort": "none"}),
    ("groq", "max", "openai/gpt-oss-120b", {"reasoning_effort": "high"}),
    ("zhipu", "none", "glm-4.7", {"thinking": {"type": "disabled"}}),
    ("zhipu", "high", "glm-4.7", {"thinking": {"type": "enabled"}}),
    ("zhipu", "medium", "glm-5.2", {"thinking": {"type": "enabled"}, "reasoning_effort": "high"}),
    ("zhipu", "none", "glm-5.3", {"reasoning_effort": "low"}),  # cannot stop thinking
])
async def test_a_turn_carries_the_level_in_the_providers_own_field(name, effort, model, body):
    assert (await _sent(name, effort, model)).get("extra_body") == body


@pytest.mark.parametrize(("name", "effort", "model", "reasoning"), [
    ("gemini", "high", "gemma-4-31b-it", None),  # on: the model's own
    ("groq", "high", "qwen/qwen3.6-27b", None),
    ("openai", "high", "gpt-4.1", None),  # takes no level
    ("openai", "high", "gpt-9-unknown", None),  # not in the catalogue
    ("openai", "", "gpt-5", None),  # no level set
    ("zhipu", "high", "glm-5.2", False),  # background work
])
async def test_nothing_is_sent_where_nothing_is_asked_or_known(name, effort, model, reasoning):
    assert "extra_body" not in await _sent(name, effort, model, reasoning)


async def test_the_coding_plan_turns_carry_glm_fields(monkeypatch):
    monkeypatch.setattr("flowly.auth.zai_coding.load_token_payload", lambda: SimpleNamespace(api_key="k"))
    assert (await _sent("zai_coding", "none", "glm-5.2"))["extra_body"] == {"thinking": {"type": "disabled"}}


async def test_openai_reasoning_models_get_max_completion_tokens_and_no_temperature_while_thinking():
    thinking = await _sent("openai", "high", "gpt-5.4")
    assert "max_tokens" not in thinking and thinking["max_completion_tokens"] == 4096
    assert "temperature" not in thinking
    off = await _sent("openai", "none", "gpt-5.4")
    assert off["temperature"] == 0.7
    assert "temperature" not in await _sent("openai", "none", "gpt-5")  # never takes it
    plain = await _sent("openai", "", "gpt-4.1")
    assert plain["temperature"] == 0.7 and plain["max_completion_tokens"] == 4096


async def test_other_providers_keep_max_tokens_and_temperature():
    sent = await _sent("gemini", "high", "gemini-3.5-flash")
    assert sent["max_tokens"] == 4096 and sent["temperature"] == 0.7


async def test_a_refused_thinking_field_is_dropped_whole_and_remembered():
    await catalog.fetch_models("zhipu", force_refresh=True)
    provider, sent = _provider(
        "zhipu", "high",
        refuse=lambda kwargs: "thinking is not supported" if "extra_body" in kwargs else None,
    )
    assert (await _turn(provider, "glm-5.2")).content == "ok"
    assert sent[0]["extra_body"] == {"thinking": {"type": "enabled"}, "reasoning_effort": "high"}
    assert "extra_body" not in sent[1]
    await _turn(provider, "glm-5.2")
    assert len(sent) == 3 and "extra_body" not in sent[2]


async def test_a_refused_temperature_is_dropped_and_remembered():
    await catalog.fetch_models("gemini", force_refresh=True)
    provider, sent = _provider(
        "gemini", "high",
        refuse=lambda kwargs: "Unsupported parameter: 'temperature'" if "temperature" in kwargs else None,
    )
    assert (await _turn(provider, "gemini-3.5-flash")).content == "ok"
    assert "temperature" in sent[0] and "temperature" not in sent[1]
    assert sent[1]["extra_body"] == {"reasoning_effort": "high"}  # the level stays
    await _turn(provider, "gemini-3.5-flash")
    assert len(sent) == 3 and "temperature" not in sent[2]


async def test_an_unrelated_refusal_is_not_retried():
    await catalog.fetch_models("openai", force_refresh=True)
    provider, sent = _provider("openai", "high", refuse=lambda _kwargs: "Invalid messages")
    result = await _turn(provider, "gpt-5")
    assert result.finish_reason == "error" and len(sent) == 1


async def test_a_streamed_turn_carries_the_same_fields():
    await catalog.fetch_models("openai", force_refresh=True)
    provider, sent = _provider("openai", "low", refuse=None)
    async for _ in provider.chat_stream(messages=[{"role": "user", "content": "hi"}], model="gpt-5"):
        break
    assert sent[0]["extra_body"] == {"reasoning_effort": "low"}
    assert "temperature" not in sent[0] and sent[0]["max_completion_tokens"] == 4096
