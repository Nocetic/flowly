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


def _provider(name: str) -> tuple[OpenRouterProvider, list[dict[str, Any]]]:
    provider = OpenRouterProvider(api_key="test-key", provider_name=name)
    sent: list[dict[str, Any]] = []

    async def create(**kwargs: Any) -> object:
        sent.append(kwargs)
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
