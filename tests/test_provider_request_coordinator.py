"""Provider requests are bounded and overflow recovery never retries blindly."""

from __future__ import annotations

from dataclasses import dataclass

from flowly.agent.loop import AgentLoop
from flowly.agent.run_abort import RunAbortController
from flowly.compaction.request_guard import (
    ProviderRequestCoordinator,
    request_fingerprint,
)
from flowly.compaction.service import CompactionService
from flowly.compaction.types import (
    EPHEMERAL_NUDGE_KEY,
    CompactionConfig,
    KeepRecentConfig,
    MemoryFlushConfig,
    is_summary_message,
)
from flowly.providers.base import LLMErrorInfo, LLMResponse


class _SummaryProvider:
    provider_name = "stub"

    def __init__(self):
        self.calls: list[dict] = []

    async def chat(self, **kwargs):
        self.calls.append(kwargs)
        return LLMResponse(
            content=(
                "Earlier turns established the deployment facts and constraints. "
                "The most recent historical issue remained under investigation."
            ),
            finish_reason="stop",
        )


def _service(provider, *, window: int = 4_000) -> CompactionService:
    return CompactionService(
        provider=provider,
        model="test/model",
        config=CompactionConfig(
            mode="default",
            context_window=window,
            context_window_explicit=True,
            reserve_tokens_floor=512,
            memory_flush=MemoryFlushConfig(enabled=False),
            keep_recent=KeepRecentConfig(
                enabled=True,
                min_tokens=200,
                min_messages=2,
                max_tokens=800,
                max_share=0.25,
            ),
        ),
    )


def _history(turns: int = 18, words: int = 55):
    filler = "deployment shard retry evidence constraint " * words
    messages = [{"role": "system", "content": "AUTHORITATIVE SYSTEM POLICY"}]
    for index in range(turns):
        messages.append({"role": "user", "content": f"question {index} {filler}"})
        messages.append({"role": "assistant", "content": f"answer {index} {filler}"})
    messages.append({"role": "user", "content": "CURRENT RAW REQUEST — inspect shard nine"})
    return messages


async def test_preflight_compacts_working_copy_and_preserves_authority_and_raw_user():
    provider = _SummaryProvider()
    coordinator = ProviderRequestCoordinator(_service(provider))
    messages = _history()
    messages.append({
        "role": "user",
        "content": "prompt-only checkpoint",
        EPHEMERAL_NUDGE_KEY: True,
    })

    fitted = await coordinator.fit(messages, [], "test/model")

    assert fitted.fits
    assert fitted.changed
    assert fitted.after.estimated_input_tokens <= fitted.after.input_limit
    assert fitted.messages[0] == messages[0]
    assert any(is_summary_message(message) for message in fitted.messages)
    assert any(
        message.get("content") == "CURRENT RAW REQUEST — inspect shard nine"
        for message in fitted.messages
    )
    assert any(
        message.get("content") == "prompt-only checkpoint"
        for message in fitted.messages
    )


async def test_tool_schema_budget_can_fail_before_any_conversation_is_sent():
    provider = _SummaryProvider()
    coordinator = ProviderRequestCoordinator(_service(provider, window=1_200))
    tools = [{
        "type": "function",
        "function": {
            "name": f"large_tool_{index}",
            "description": "schema detail " * 400,
            "parameters": {"type": "object", "properties": {}},
        },
    } for index in range(4)]
    messages = [
        {"role": "system", "content": "policy"},
        {"role": "user", "content": "hello"},
    ]

    fitted = await coordinator.fit(messages, tools, "test/model")

    assert not fitted.fits
    assert "leave no room" in (fitted.failure or "")
    assert provider.calls == [], "preflight itself must not dispatch the rejected request"


async def test_failed_preflight_summary_fails_closed_without_partial_history_dispatch():
    class _FailingSummaryProvider(_SummaryProvider):
        async def chat(self, **kwargs):
            self.calls.append(kwargs)
            return LLMResponse(
                content="opaque summarizer outage",
                finish_reason="error",
                error_info=LLMErrorInfo(status_code=503),
            )

    provider = _FailingSummaryProvider()
    harness = _CallHarness(provider, _service(provider, window=4_000))
    messages = _history()
    original_fingerprint = request_fingerprint(messages)

    response, returned = await harness._call_provider_with_context_recovery(
        messages=messages,
        tools=[],
        model="test/model",
        temperature=0.7,
        tool_choice="auto",
        session_key="web:summary-outage",
    )

    assert response.finish_reason == "error"
    assert response.error_info == LLMErrorInfo(
        status_code=413,
        code="input_too_large",
        type="request_too_large",
    )
    assert request_fingerprint(returned) == original_fingerprint
    assert provider.calls, "preflight should attempt a summary before failing"
    assert all("tool_choice" not in call for call in provider.calls), (
        "the main model must not receive a silently truncated history"
    )


async def test_force_reduction_materially_changes_a_locally_fitting_payload():
    provider = _SummaryProvider()
    coordinator = ProviderRequestCoordinator(_service(provider, window=8_000))
    messages = _history(turns=8, words=15)
    before = coordinator.budget(messages, [], "test/model")
    assert before.estimated_input_tokens <= before.input_limit

    fitted = await coordinator.fit(
        messages,
        [],
        "test/model",
        force_reduction=True,
    )

    assert fitted.fits
    assert fitted.changed
    assert fitted.after.compactable_tokens < fitted.before.compactable_tokens
    assert request_fingerprint(fitted.messages) != request_fingerprint(messages)


class _ScriptedProvider(_SummaryProvider):
    def __init__(self, first: LLMResponse):
        super().__init__()
        self.first = first
        self.main_calls: list[list[dict]] = []

    async def chat(self, **kwargs):
        self.calls.append(kwargs)
        if "tool_choice" not in kwargs:
            return LLMResponse(content="Compact factual continuity summary.")
        self.main_calls.append(kwargs["messages"])
        if len(self.main_calls) == 1:
            return self.first
        return LLMResponse(content="Recovered answer.", finish_reason="stop")


@dataclass
class _CallHarness:
    provider: object
    compaction: CompactionService

    _chat_without_stream = AgentLoop._chat_without_stream
    _chat_with_stream = AgentLoop._chat_with_stream
    _request_coordinator = AgentLoop._request_coordinator
    _request_too_large_response = staticmethod(AgentLoop._request_too_large_response)
    _call_provider_with_context_recovery = AgentLoop._call_provider_with_context_recovery
    is_run_aborted = AgentLoop.is_run_aborted

    def __post_init__(self):
        self._provider_requests = ProviderRequestCoordinator(self.compaction)
        self._run_aborts = RunAbortController()
        self._api_call_count = 0

    def _touch_activity(self, _detail: str) -> None:
        pass


def _harness(first: LLMResponse, *, window: int = 8_000):
    provider = _ScriptedProvider(first)
    return _CallHarness(provider, _service(provider, window=window)), provider


async def test_structured_413_retries_once_only_after_payload_reduction():
    harness, provider = _harness(LLMResponse(
        content="opaque provider failure",
        finish_reason="error",
        error_info=LLMErrorInfo(status_code=413),
    ))
    messages = _history(turns=8, words=15)

    response, recovered = await harness._call_provider_with_context_recovery(
        messages=messages,
        tools=[],
        model="test/model",
        temperature=0.7,
        tool_choice="auto",
        session_key="web:one",
    )

    assert response.content == "Recovered answer."
    assert len(provider.main_calls) == 2
    assert request_fingerprint(provider.main_calls[0]) != request_fingerprint(
        provider.main_calls[1]
    )
    assert request_fingerprint(recovered) == request_fingerprint(provider.main_calls[1])
    assert any(
        message.get("content") == "CURRENT RAW REQUEST — inspect shard nine"
        for message in recovered
    )


async def test_429_never_enters_context_recovery():
    harness, provider = _harness(LLMResponse(
        content="tier says request too large right now",
        finish_reason="error",
        error_info=LLMErrorInfo(status_code=429),
    ))
    messages = _history(turns=4, words=8)

    response, returned = await harness._call_provider_with_context_recovery(
        messages=messages,
        tools=[],
        model="test/model",
        temperature=0.7,
        tool_choice="auto",
        session_key="web:rate",
    )

    assert response.finish_reason == "error"
    assert len(provider.main_calls) == 1
    assert returned == messages
    assert len(provider.calls) == 1, "rate limiting must not spend a summary call"


class _PartialStreamProvider(_SummaryProvider):
    def __init__(self):
        super().__init__()
        self.stream_calls = 0

    async def chat_stream(self, **kwargs):
        self.stream_calls += 1
        yield LLMResponse(content="Visible partial text", finish_reason="")
        yield LLMResponse(
            content="hidden raw error",
            finish_reason="error",
            error_info=LLMErrorInfo(status_code=413),
        )


async def test_overflow_after_streamed_text_is_never_retried():
    provider = _PartialStreamProvider()
    harness = _CallHarness(provider, _service(provider, window=8_000))
    delivered: list[str] = []

    async def stream(text: str) -> None:
        delivered.append(text)

    response, _messages = await harness._call_provider_with_context_recovery(
        messages=_history(turns=3, words=5),
        tools=[],
        model="test/model",
        temperature=0.7,
        tool_choice="auto",
        session_key="web:stream",
        stream_callback=stream,
    )

    assert response.finish_reason == "error"
    assert response.partial_content_delivered is True
    assert delivered == ["Visible partial text"]
    assert provider.stream_calls == 1
    assert provider.calls == [], "partial delivery must suppress summary/retry work"
