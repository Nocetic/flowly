"""Meeting Coach asks thinking models not to think (``chat(reasoning=False)``).

Every Coach call is a short answer under a small token cap (gate 1 has 300).
A thinking model behind OpenRouter spent that cap on its thinking and returned
nothing (``finish=length``): gate 1 retried, then counted it as "no tip", so
tips went missing at random. Measured 2026-10-23 on
``deepseek/deepseek-v4.1-flash`` through the Flowly backend: ~116 of ~150
completion tokens were thinking by default, 0 with ``reasoning.enabled=false``,
and the answer was whole either way.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from flowly.coaching import gate
from flowly.providers.base import LLMResponse
from flowly.providers import openrouter_provider
from flowly.providers.openrouter_provider import OpenRouterProvider


class _Recorder:
    """An ``LLMProvider`` stand-in that answers every call and keeps its kwargs."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def chat(self, **kwargs: Any) -> LLMResponse:
        self.calls.append(kwargs)
        return LLMResponse(content='{"score": 0.9, "reason": "named", "tip": "t", "keep": true}', finish_reason="stop")


@pytest.mark.asyncio
async def test_every_coach_call_asks_for_no_thinking():
    provider = _Recorder()
    await gate.relevance_gate(provider, "m", "Aristoteles?", "philosophy", "", 0.4)
    await gate.generate_tip(provider, "m", "Aristoteles?", "philosophy", "")
    await gate.critic(provider, "m", "tip", "Aristoteles?", "philosophy")
    await gate.summarize_meeting(provider, "m", "Aristoteles?", "philosophy")
    await gate.extract_entities(provider, "m", "summary")
    assert len(provider.calls) == 5
    assert all(call.get("reasoning") is False for call in provider.calls)
    assert all(call.get("purpose") == "coaching" for call in provider.calls)


@pytest.fixture(autouse=True)
def _fresh_mandatory_models(monkeypatch):
    monkeypatch.setattr(openrouter_provider, "_REASONING_MANDATORY_MODELS", set())


MANDATORY = ("Error code: 400 - {'error': {'message': 'Reasoning is mandatory for this endpoint "
             "and cannot be disabled.', 'code': 400}}")


def _provider(name: str, refuses: set[str] = frozenset()) -> tuple[OpenRouterProvider, list[dict[str, Any]]]:
    provider = OpenRouterProvider(api_key="test-key", provider_name=name)
    sent: list[dict[str, Any]] = []

    async def create(**kwargs: Any) -> object:
        sent.append({**kwargs, "extra_body": dict(kwargs["extra_body"])} if "extra_body" in kwargs else dict(kwargs))
        if kwargs["model"] in refuses and "reasoning" in kwargs.get("extra_body", {}):
            raise RuntimeError(MANDATORY)
        return object()

    completions = SimpleNamespace(create=create)
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    provider._client_for_key = lambda _key: SimpleNamespace(with_options=lambda **_: client)  # type: ignore[method-assign]
    provider._parse_response = lambda _response: LLMResponse(content="ok")  # type: ignore[method-assign]
    return provider, sent


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["openrouter", "flowly"])
async def test_openrouter_and_the_flowly_backend_get_the_switch(name):
    provider, sent = _provider(name)
    await provider.chat(messages=[{"role": "user", "content": "hi"}], purpose="coaching", reasoning=False)
    assert sent[0]["extra_body"] == {"purpose": "coaching", "reasoning": {"enabled": False}}


@pytest.mark.asyncio
async def test_the_default_and_other_compatible_apis_send_no_switch():
    provider, sent = _provider("flowly")
    await provider.chat(messages=[{"role": "user", "content": "hi"}], purpose="coaching")
    assert sent[0]["extra_body"] == {"purpose": "coaching"}

    # A plain OpenAI-compatible API may refuse an unknown body field.
    provider, sent = _provider("openai")
    await provider.chat(messages=[{"role": "user", "content": "hi"}], reasoning=False)
    assert "extra_body" not in sent[0]


@pytest.mark.asyncio
async def test_a_model_that_always_thinks_is_asked_again_without_the_switch_and_remembered():
    """Measured 2026-10-24: openai/gpt-5-mini, openai/o4-mini and google/gemini-2.5-pro answer
    the switch with a 400 "Reasoning is mandatory"; without this every Coach call failed on them."""
    provider, sent = _provider("flowly", refuses={"openai/gpt-5-mini"})
    reply = await provider.chat(messages=[{"role": "user", "content": "hi"}], model="openai/gpt-5-mini",
                                purpose="coaching", reasoning=False)
    assert reply.content == "ok"
    assert [call["extra_body"] for call in sent] == [
        {"purpose": "coaching", "reasoning": {"enabled": False}},
        {"purpose": "coaching"},
    ]
    # Next time it is asked without the switch at once; other models still get it.
    sent.clear()
    await provider.chat(messages=[{"role": "user", "content": "hi"}], model="openai/gpt-5-mini", reasoning=False)
    await provider.chat(messages=[{"role": "user", "content": "hi"}], model="deepseek/deepseek-v4.1-flash", reasoning=False)
    assert "extra_body" not in sent[0]
    assert sent[1]["extra_body"] == {"reasoning": {"enabled": False}}


@pytest.mark.asyncio
async def test_any_other_error_is_not_retried_without_the_switch():
    provider = OpenRouterProvider(api_key="test-key", provider_name="flowly")
    calls: list[dict[str, Any]] = []

    async def create(**kwargs: Any) -> object:
        calls.append(kwargs)
        raise RuntimeError("Error code: 429 - rate limited")

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    provider._client_for_key = lambda _key: SimpleNamespace(with_options=lambda **_: client)  # type: ignore[method-assign]
    reply = await provider.chat(messages=[{"role": "user", "content": "hi"}], model="m", reasoning=False)
    assert reply.finish_reason == "error"
    assert len(calls) == 1
