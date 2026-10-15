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
    _steering = AgentLoop._steering

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


# ── Learned provider input ceilings ─────────────────────────────────────────
#
# A provider that rejects a request as too large has revealed a real ceiling
# the model catalog cannot see (TPM tier, per-request byte cap, proxy guard).
# The service learns it once, in-process, and every later budget — durable
# compaction and pre-dispatch fitting alike — shrinks to fit, so the session
# stops paying a transient recovery on every single turn.


def test_observed_input_limit_clamps_window_and_threshold():
    svc = _service(_SummaryProvider(), window=128_000)
    assert svc.effective_context_window_for("test/model") == 128_000

    assert svc.note_observed_input_limit("test/model", 30_000) == 30_000
    assert svc.observed_input_limit_for("test/model") == 30_000
    assert svc.effective_context_window_for("test/model") == 30_000
    assert svc.compaction_threshold_for("test/model") < 30_000

    # Lower-only: a later, looser reading never widens the clamp…
    assert svc.note_observed_input_limit("test/model", 60_000) == 30_000
    assert svc.effective_context_window_for("test/model") == 30_000
    # …while a stricter observation tightens it.
    assert svc.note_observed_input_limit("test/model", 20_000) == 20_000
    assert svc.effective_context_window_for("test/model") == 20_000


def test_observed_input_limit_rejects_unusable_readings():
    svc = _service(_SummaryProvider(), window=128_000)

    assert svc.note_observed_input_limit("test/model", 100) is None
    assert svc.note_observed_input_limit("test/model", 128_000) is None
    assert svc.note_observed_input_limit("test/model", 300_000) is None
    assert svc.observed_input_limit_for("test/model") is None
    assert svc.effective_context_window_for("test/model") == 128_000
    # A blank model resolves to the service default, like every other
    # ``*_for`` accessor — it must not create a phantom "" entry.
    assert svc.note_observed_input_limit(None, 30_000) == 30_000
    assert svc.observed_input_limit_for("test/model") == 30_000


def test_observed_limit_makes_durable_compaction_fire_next_turn():
    svc = _service(_SummaryProvider(), window=128_000)
    # A 40K history sits comfortably inside a 128K window…
    assert not svc.should_compact(40_000, "web:ceiling")
    svc.note_observed_input_limit("test/model", 30_000)
    # …but exceeds the provider's real ceiling, so the next pre-turn check
    # compacts durably instead of leaving every turn to transient recovery.
    assert svc.should_compact(40_000, "web:ceiling")


def test_extract_input_limit_tokens_reads_the_advertised_limit():
    from flowly.agent.error_classifier import extract_input_limit_tokens

    response = LLMResponse(
        content=(
            "Error calling LLM: Request too large for gpt-x in organization "
            "org-1 on tokens per min (TPM): Limit 30000, Requested 34712."
        ),
        finish_reason="error",
        error_info=LLMErrorInfo(status_code=413),
    )
    assert extract_input_limit_tokens(response) == 30_000


def test_extract_input_limit_tokens_handles_separators_and_absence():
    from flowly.agent.error_classifier import extract_input_limit_tokens

    assert extract_input_limit_tokens(
        LLMResponse(content="input too large: Limit 30,000 tokens", finish_reason="error")
    ) == 30_000
    assert extract_input_limit_tokens(
        LLMResponse(content="request too large", finish_reason="error")
    ) is None
    assert extract_input_limit_tokens(
        LLMResponse(content="", finish_reason="error")
    ) is None


async def test_structured_413_teaches_the_provider_ceiling():
    harness, provider = _harness(
        LLMResponse(
            content=(
                "Error calling LLM: input too large on tokens per min (TPM): "
                "Limit 6000, Requested 9000"
            ),
            finish_reason="error",
            error_info=LLMErrorInfo(status_code=413),
        ),
        window=128_000,
    )
    messages = _history(turns=8, words=15)

    response, _recovered = await harness._call_provider_with_context_recovery(
        messages=messages,
        tools=[],
        model="test/model",
        temperature=0.7,
        tool_choice="auto",
        session_key="web:learn",
    )

    assert response.content == "Recovered answer."
    assert harness.compaction.observed_input_limit_for("test/model") == 6_000
    # The learned ceiling governs the whole service, not just this call.
    assert harness.compaction.effective_context_window_for("test/model") == 6_000


async def test_unparseable_413_still_learns_a_conservative_ceiling():
    harness, provider = _harness(
        LLMResponse(
            content="opaque provider failure",
            finish_reason="error",
            error_info=LLMErrorInfo(status_code=413),
        ),
        window=128_000,
    )
    # Big enough that 90% of the rejected estimate clears the sanity floor.
    messages = _history(turns=30, words=100)

    await harness._call_provider_with_context_recovery(
        messages=messages,
        tools=[],
        model="test/model",
        temperature=0.7,
        tool_choice="auto",
        session_key="web:fallback",
    )

    learned = harness.compaction.observed_input_limit_for("test/model")
    assert learned is not None
    assert learned >= 4_096
    assert learned < 128_000


# ── Options a subagent uses (the main agent passes none) ─────────────────────

def _tool_loop(steps: int = 14, chars: int = 1_400, words: int = 200):
    """A subagent's history: its brief, then a tool loop whose assistant
    text (which no cut shortens) outgrows a 4K window on its own."""
    messages = [{"role": "system", "content": "AUTHORITATIVE SYSTEM POLICY"},
                {"role": "user", "content": "BRIEF: inspect every shard and report"}]
    for index in range(steps):
        messages.append({"role": "assistant", "content": f"step {index} " + "analysis " * words,
                         "tool_calls": [
            {"id": f"c{index}", "type": "function",
             "function": {"name": "read_file", "arguments": "{}"}}]})
        messages.append({"role": "tool", "tool_call_id": f"c{index}", "name": "read_file",
                         "content": f"shard {index} " + "evidence " * (chars // 9)})
    return messages


async def test_pinned_messages_are_kept_verbatim_after_the_summary_and_never_summarized():
    provider = _SummaryProvider()
    coordinator = ProviderRequestCoordinator(_service(provider))
    messages = _tool_loop()

    fit = await coordinator.fit(messages, None, "test/model", pinned=1,
                                custom_instructions="RECORD THE WORK")

    assert fit.fits and fit.compaction is not None
    assert fit.messages[0] == messages[0] and is_summary_message(fit.messages[1])
    assert fit.messages[2] == messages[1]
    prompts = " ".join(str(call["messages"]) for call in provider.calls)
    assert "BRIEF:" not in prompts and "RECORD THE WORK" in prompts
    # A second fit keeps one summary: the earlier one is folded in, the pin stays.
    grown = fit.messages + _tool_loop()[2:]
    again = await coordinator.fit(grown, None, "test/model", pinned=1)
    assert again.fits and sum(is_summary_message(m) for m in again.messages) == 1
    assert again.messages[2] == messages[1]


async def test_without_pins_the_budget_and_the_cut_are_unchanged():
    coordinator = ProviderRequestCoordinator(_service(_SummaryProvider(), window=40_000))
    messages = _tool_loop(steps=8)
    plain = coordinator.budget(messages, None, "test/model")
    pinned = coordinator.budget(messages, None, "test/model", pinned=1)
    assert plain.messages_tokens == pinned.messages_tokens
    assert pinned.fixed_prefix_tokens > plain.fixed_prefix_tokens
    assert pinned.compactable_tokens < plain.compactable_tokens


async def test_a_summary_that_fails_is_marked_retryable_and_a_structural_failure_is_not():
    class _Failing(_SummaryProvider):
        async def chat(self, **kwargs):
            self.calls.append(kwargs)
            return LLMResponse(content="Error calling LLM: upstream 502", finish_reason="error")

    coordinator = ProviderRequestCoordinator(_service(_Failing()))
    failed = await coordinator.fit(_tool_loop(), None, "test/model", pinned=1)
    assert not failed.fits and failed.retryable
    huge_tools = [{"type": "function", "function": {"name": "t", "description": "x " * 8_000}}]
    structural = await coordinator.fit(_tool_loop(), huge_tools, "test/model", pinned=1)
    assert not structural.fits and not structural.retryable


def test_request_too_large_response_is_classified_input_too_large():
    from flowly.agent.error_classifier import ErrorCategory, classify_response
    from flowly.compaction.request_guard import request_too_large_response

    response = request_too_large_response("no room")
    assert response.finish_reason == "error"
    assert classify_response(response) is ErrorCategory.INPUT_TOO_LARGE
    assert AgentLoop._request_too_large_response("no room").error_info == response.error_info


def test_microcompact_and_strip_are_pure_and_idempotent():
    from flowly.compaction.pruning import microcompact_tool_results, strip_old_tool_results

    messages = _tool_loop(steps=8)
    snapshot = [dict(m) for m in messages]
    once = microcompact_tool_results(messages, keep_recent_full=5, truncate_chars=200)
    assert messages == snapshot, "never mutated"
    assert microcompact_tool_results(once, keep_recent_full=5, truncate_chars=200) is once
    service = _service(_SummaryProvider())
    service.config.microcompact.keep_recent_full = 5
    service.config.microcompact.truncate_chars = 200
    assert service.microcompact(messages) == once
    stripped = strip_old_tool_results(messages, keep_last=3, max_old_chars=300)
    assert strip_old_tool_results(stripped, keep_last=3, max_old_chars=300) == stripped
    assert sum(m["role"] == "tool" and len(m["content"]) > 400 for m in stripped) == 3
