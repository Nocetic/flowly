"""Background passes ask thinking models not to think (``background_answer``).

The dreamer, memory import and consolidation and skill curation ask for a short
JSON answer under a fixed token cap. Measured 2026-10-31 on
``deepseek/deepseek-v4.1-flash`` through the Flowly backend, the dreamer's own
call (68 messages, 7 591 prompt tokens, ``max_tokens=2048``): every one of the
2 048 completion tokens was thinking, ``finish_reason=length``, no answer — three
attempts in a row, so the dreamer failed and held its watermark. With
``reasoning.enabled=false`` the same prompt answered in 559 tokens with valid
JSON. Consolidation had taken the same failure for flaky proxy streams.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from flowly.memory.dreamer import MessageRow
from flowly.memory.extractor import ExtractionError, SubagentExtractor
from flowly.providers import openrouter_provider
from flowly.providers.background import BackgroundAnswerError, background_answer
from flowly.providers.base import LLMResponse
from flowly.providers.openrouter_provider import OpenRouterProvider


class _ThinkingModel:
    """A thinking model under a small cap: all of it goes to thinking unless told not to."""

    def __init__(self, answer: str = "[]") -> None:
        self.answer = answer
        self.calls: list[dict[str, Any]] = []

    async def chat_stream(self, messages, *, model=None, max_tokens=4096, temperature=0.7, reasoning=None, **_: Any):
        self.calls.append({"max_tokens": max_tokens, "reasoning": reasoning})
        if reasoning is not False:
            yield LLMResponse(content=None, finish_reason="length",
                              usage={"completion_tokens": max_tokens, "reasoning_tokens": max_tokens})
            return
        yield LLMResponse(content=self.answer, finish_reason="")
        yield LLMResponse(content=None, finish_reason="stop")


class _FailingBackend:
    """What a provider streams when the call fails: its error as text."""

    def __init__(self) -> None:
        self.calls = 0

    async def chat_stream(self, messages, **_: Any):
        self.calls += 1
        yield LLMResponse(content="Error calling LLM: Error code: 503 - upstream unavailable", finish_reason="error")


_DELTA = [MessageRow(id=1, session_key="web:a", role="user", content="I prefer green tea.", timestamp=0.0)]


@pytest.mark.asyncio
async def test_a_background_answer_turns_thinking_off():
    model = _ThinkingModel(answer='[{"kind": "preference"}]')
    assert await background_answer(model, "extract", model="m", max_tokens=2048, temperature=0.2,
                                   label="test") == '[{"kind": "preference"}]'
    assert model.calls == [{"max_tokens": 2048, "reasoning": False}]


def test_the_dreamer_hears_a_thinking_model():
    extractor = SubagentExtractor(provider=_ThinkingModel(answer="[]"), model="deepseek/deepseek-v4.1-flash")
    assert extractor.extract(_DELTA) == []


@pytest.mark.asyncio
async def test_a_provider_error_is_never_an_answer():
    backend = _FailingBackend()
    with pytest.raises(BackgroundAnswerError):
        await background_answer(backend, "extract", model="m", max_tokens=2048, temperature=0.2, label="test")
    assert backend.calls == 3


def test_the_dreamer_holds_its_place_on_a_provider_error():
    """Read as an answer, the error text parsed to "nothing to learn" and the
    dreamer moved past messages it never read."""
    with pytest.raises(ExtractionError):
        SubagentExtractor(provider=_FailingBackend(), model="m").extract(_DELTA)


# ── the switch on the stream ─────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _fresh_mandatory_models(monkeypatch):
    monkeypatch.setattr(openrouter_provider, "_REASONING_MANDATORY_MODELS", set())


MANDATORY = ("Error code: 400 - {'error': {'message': 'Reasoning is mandatory for this endpoint "
             "and cannot be disabled.', 'code': 400}}")


class _Ended:
    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration

    async def close(self) -> None:
        pass


def _streaming(name: str, refuses: set[str] = frozenset()) -> tuple[OpenRouterProvider, list[dict[str, Any]]]:
    provider = OpenRouterProvider(api_key="test-key", provider_name=name)
    sent: list[dict[str, Any]] = []

    async def create(**kwargs: Any) -> object:
        sent.append({**kwargs, "extra_body": dict(kwargs["extra_body"])} if "extra_body" in kwargs else dict(kwargs))
        if kwargs["model"] in refuses and "reasoning" in kwargs.get("extra_body", {}):
            raise RuntimeError(MANDATORY)
        return _Ended()

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    provider._client_for_key = lambda _key: SimpleNamespace(with_options=lambda **_: client)  # type: ignore[method-assign]
    return provider, sent


async def _drain(stream) -> None:
    async for _ in stream:
        pass


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["openrouter", "flowly"])
async def test_the_stream_carries_the_switch(name):
    provider, sent = _streaming(name)
    await _drain(provider.chat_stream([{"role": "user", "content": "hi"}], reasoning=False))
    await _drain(provider.chat_stream([{"role": "user", "content": "hi"}]))
    assert sent[0]["extra_body"] == {"reasoning": {"enabled": False}}
    assert "extra_body" not in sent[1]  # the agent's own turns keep the model's default

    provider, sent = _streaming("openai")
    await _drain(provider.chat_stream([{"role": "user", "content": "hi"}], reasoning=False))
    assert "extra_body" not in sent[0]


@pytest.mark.asyncio
async def test_a_model_that_always_thinks_streams_without_the_switch_and_is_remembered():
    provider, sent = _streaming("flowly", refuses={"openai/gpt-5-mini"})
    await _drain(provider.chat_stream([{"role": "user", "content": "hi"}], model="openai/gpt-5-mini", reasoning=False))
    assert [call.get("extra_body") for call in sent] == [{"reasoning": {"enabled": False}}, None]
    sent.clear()
    await _drain(provider.chat_stream([{"role": "user", "content": "hi"}], model="openai/gpt-5-mini", reasoning=False))
    assert "extra_body" not in sent[0]
