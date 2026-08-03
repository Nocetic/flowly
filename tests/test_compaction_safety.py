"""Compaction must never destroy context it failed to summarize.

These lock down the failure modes that make compaction dangerous rather
than merely suboptimal:

  1. Providers report API failures as ordinary content
     (``content="Error calling LLM: …", finish_reason="error"``) instead
     of raising. Committing that as the summary replaces the whole
     conversation with an outage message.
  2. A compaction summary lives in the sliding history window, so it
     slides out once enough newer messages arrive and the model loses
     everything the summary was protecting.
  3. Keeping "the last N messages" verbatim can cut between an
     ``assistant.tool_calls`` and its ``tool`` results, which providers
     reject with a 400.
  4. Failure counters are per session — one conversation's outage must
     not suppress compaction for every other conversation, and must not
     wedge permanently.
"""

import pytest

from flowly.compaction.service import CompactionService
from flowly.compaction.types import (
    CompactionConfig,
    CompactionError,
    KeepRecentConfig,
    SUMMARY_MARKER,
    is_summary_message,
)
from flowly.providers.base import LLMResponse


class _Provider:
    """Stub provider returning a scripted LLMResponse per chat() call."""

    provider_name = "stub"

    def __init__(self, response: LLMResponse):
        self._response = response
        self.calls = 0
        self.prompts: list[str] = []

    async def chat(self, *args, **kwargs) -> LLMResponse:
        self.calls += 1
        messages = kwargs.get("messages") or (args[0] if args else [])
        for m in messages:
            content = m.get("content")
            if isinstance(content, str):
                self.prompts.append(content)
        return self._response

    def saw(self, needle: str) -> bool:
        return any(needle in p for p in self.prompts)


def _service(provider, **config_kwargs) -> CompactionService:
    config = CompactionConfig(mode="default", **config_kwargs)
    return CompactionService(provider=provider, model="m", config=config)


def _conversation(turns: int, filler: str = "word " * 50) -> list[dict]:
    msgs: list[dict] = []
    for i in range(turns):
        msgs.append({"role": "user", "content": f"question {i} {filler}"})
        msgs.append({"role": "assistant", "content": f"answer {i} {filler}"})
    return msgs


# ── 1. Provider error must never become the summary ───────────────────────


@pytest.mark.parametrize(
    "response",
    [
        LLMResponse(content="Error calling LLM: simulated outage", finish_reason="error"),
        LLMResponse(content="Error calling LLM: rate limited", finish_reason="stop"),
        LLMResponse(content="", finish_reason="stop"),
        LLMResponse(content="   ", finish_reason="stop"),
    ],
    ids=["finish_reason_error", "error_envelope_as_content", "empty", "whitespace"],
)
async def test_provider_failure_raises_instead_of_committing(response):
    service = _service(_Provider(response))

    with pytest.raises(CompactionError):
        await service.compact(_conversation(6))


async def test_failed_compaction_leaves_messages_untouched():
    provider = _Provider(
        LLMResponse(content="Error calling LLM: outage", finish_reason="error")
    )
    service = _service(provider)
    messages = _conversation(6)
    snapshot = [dict(m) for m in messages]

    result_messages, result = await service.compact_if_needed(
        messages, session_key="s1",
    )

    assert result is None
    assert result_messages == snapshot, "history must survive a failed compaction"
    assert service.compaction_count_for("s1") == 0


async def test_successful_compaction_commits_summary():
    provider = _Provider(LLMResponse(content="A real summary.", finish_reason="stop"))
    service = _service(provider, context_window=2_000, reserve_tokens_floor=100)

    messages, result = await service.compact_if_needed(
        _conversation(40), session_key="s1",
    )

    assert result is not None
    assert is_summary_message(messages[0])
    assert "A real summary." in messages[0]["content"]
    assert result.tokens_after < result.tokens_before
    assert service.compaction_count_for("s1") == 1


async def test_compaction_that_does_not_shrink_is_refused():
    # A "summary" far larger than the input would grow the context.
    provider = _Provider(
        LLMResponse(content="padding " * 5_000, finish_reason="stop")
    )
    service = _service(provider)

    with pytest.raises(CompactionError, match="would not reduce"):
        await service.compact(_conversation(3))


# ── 2. Tool-call blocks stay intact ───────────────────────────────────────


def _tool_turn(i: int, filler: str = "word " * 40) -> list[dict]:
    call_id = f"call-{i}"
    return [
        {"role": "user", "content": f"do task {i} {filler}"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": call_id, "type": "function",
                 "function": {"name": "run", "arguments": "{}"}},
            ],
        },
        {"role": "tool", "tool_call_id": call_id, "name": "run",
         "content": f"result {i} {filler}"},
        {"role": "assistant", "content": f"done {i} {filler}"},
    ]


def _assert_tool_protocol_valid(messages: list[dict]) -> None:
    """Every tool result must be preceded by the assistant that called it."""
    open_ids: set[str] = set()
    for msg in messages:
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            open_ids = {tc["id"] for tc in msg["tool_calls"]}
        elif msg.get("role") == "tool":
            assert msg["tool_call_id"] in open_ids, (
                f"orphan tool result {msg['tool_call_id']} in {[m['role'] for m in messages]}"
            )


def test_keep_recent_never_strands_a_tool_result():
    service = _service(_Provider(LLMResponse(content="s", finish_reason="stop")))
    messages: list[dict] = []
    for i in range(8):
        messages.extend(_tool_turn(i))

    # Sweep every plausible budget: none may produce a half block.
    for max_tokens in range(20, 400, 7):
        service.config.keep_recent = KeepRecentConfig(
            min_tokens=1, min_messages=1, max_tokens=max_tokens,
        )
        kept = service._calculate_keep_recent(messages)
        if kept:
            assert kept[0]["role"] == "user"
            _assert_tool_protocol_valid(kept)


async def test_compacted_history_is_a_valid_tool_sequence():
    provider = _Provider(LLMResponse(content="Summary text.", finish_reason="stop"))
    service = _service(provider, context_window=2_000, reserve_tokens_floor=100)
    # Small keep window so a tail really is preserved verbatim — that is the
    # boundary where a mid-block cut would strand a tool result.
    service.config.keep_recent = KeepRecentConfig(
        min_tokens=100, min_messages=1, max_tokens=600,
    )
    messages: list[dict] = []
    for i in range(12):
        messages.extend(_tool_turn(i))

    compacted, result = await service.compact_if_needed(messages, session_key="s1")

    assert result is not None
    assert result.kept_messages, "expected a verbatim tail for this to be meaningful"
    _assert_tool_protocol_valid(compacted)
    assert compacted[1]["role"] == "user", "kept tail must start on a turn boundary"


# ── 3. Metrics describe the real reduction ────────────────────────────────


async def test_messages_removed_is_the_net_reduction():
    provider = _Provider(LLMResponse(content="Summary.", finish_reason="stop"))
    service = _service(provider, context_window=2_000, reserve_tokens_floor=100)
    service.config.mode = "safeguard"
    service.config.keep_recent = KeepRecentConfig(
        min_tokens=100, min_messages=1, max_tokens=600,
    )
    messages = _conversation(30)

    compacted, result = await service.compact_if_needed(messages, session_key="s1")

    assert result is not None
    kept = len(result.kept_messages)
    assert kept, "expected a verbatim tail"
    assert result.messages_removed == len(messages) - kept
    # summary + kept == the new working context
    assert len(compacted) == kept + 1


async def test_safeguard_does_not_resummarize_the_kept_tail():
    """The kept tail is preserved verbatim; summarizing it too would put the
    same turns in BOTH the summary and the working context, and inflate every
    number we report."""
    provider = _Provider(LLMResponse(content="Summary.", finish_reason="stop"))
    service = _service(provider, context_window=2_000, reserve_tokens_floor=100)
    service.config.mode = "safeguard"
    service.config.keep_recent = KeepRecentConfig(
        min_tokens=100, min_messages=1, max_tokens=600,
    )
    messages = _conversation(40)
    sentinel = "SENTINEL-NEWEST-TURN"
    messages[-1] = {"role": "assistant", "content": sentinel}

    result = await service.compact(messages)

    kept_text = " ".join(str(m.get("content", "")) for m in result.kept_messages)
    assert sentinel in kept_text, "newest turn should be kept verbatim"
    assert not provider.saw(sentinel), (
        "kept tail was also fed to the summarizer — duplicated into the summary"
    )


# ── 4. Failure state is per session and self-healing ──────────────────────


def test_failure_suppression_is_scoped_to_one_session():
    service = _service(
        _Provider(LLMResponse(content="s", finish_reason="stop")),
        context_window=1_000,
        reserve_tokens_floor=100,
    )
    for _ in range(service.MAX_CONSECUTIVE_FAILURES):
        service.record_compaction_failure("busted")

    over_threshold = 10_000
    assert service.should_compact(over_threshold, "busted") is False
    assert service.should_compact(over_threshold, "healthy") is True


def test_failure_suppression_expires_instead_of_wedging():
    service = _service(
        _Provider(LLMResponse(content="s", finish_reason="stop")),
        context_window=1_000,
        reserve_tokens_floor=100,
    )
    for _ in range(service.MAX_CONSECUTIVE_FAILURES):
        service.record_compaction_failure("s1")

    over_threshold = 10_000
    checks = [
        service.should_compact(over_threshold, "s1")
        for _ in range(service.FAILURE_PROBE_INTERVAL)
    ]

    assert checks[0] is False, "backs off immediately after failures"
    assert checks[-1] is True, "recovers on its own once the cooldown elapses"


def test_memory_flush_cycle_is_per_session():
    service = _service(
        _Provider(LLMResponse(content="s", finish_reason="stop")),
        context_window=1_000,
        reserve_tokens_floor=100,
    )
    over = 10_000

    assert service.should_memory_flush(over, "a") is True
    service.mark_memory_flush_done("a")

    assert service.should_memory_flush(over, "a") is False, "already flushed"
    assert service.should_memory_flush(over, "b") is True, "other session unaffected"


def test_summary_marker_recognises_legacy_manual_marker():
    assert is_summary_message({"role": "system", "content": f"{SUMMARY_MARKER}\n\nx"})
    assert is_summary_message(
        {"role": "system", "content": "[Compacted conversation summary]\n\nx"}
    )
    assert not is_summary_message({"role": "user", "content": SUMMARY_MARKER})
    assert not is_summary_message({"role": "system", "content": "ordinary note"})


# ── 5. Budget follows the model, not a hardcoded constant ─────────────────


def _built_service(model: str, provider_name: str = "stub", **user_settings):
    """Service built the way the CLIs build it, from user settings."""
    from flowly.compaction.builder import build_compaction_config
    from flowly.config.schema import CompactionConfig as SchemaCompactionConfig

    provider = _Provider(LLMResponse(content="s", finish_reason="stop"))
    provider.provider_name = provider_name
    config = build_compaction_config(SchemaCompactionConfig(**user_settings))
    return CompactionService(provider=provider, model=model, config=config)


@pytest.mark.parametrize(
    "model,expected",
    [
        ("anthropic/claude-haiku-4.5", 200_000),
        ("google/gemini-2.5-pro", 1_000_000),
        ("moonshotai/kimi-k2.5", 262_144),
        ("openai/gpt-3.5-turbo", 16_385),
    ],
)
def test_window_follows_the_model_family(model, expected):
    assert _built_service(model).model_context_window == expected


def test_explicit_user_setting_wins_over_detection():
    service = _built_service("anthropic/claude-haiku-4.5", context_window=64_000)

    assert service.model_context_window == 64_000, (
        "an operator who pinned contextWindow must not be overridden"
    )


def test_flowly_proxy_input_cap_still_clamps():
    service = _built_service("anthropic/claude-haiku-4.5", provider_name="flowly")

    assert service.model_context_window == 200_000
    assert service.effective_context_window == service.FLOWLY_PROXY_MAX_INPUT_TOKENS


def test_threshold_tracks_the_window():
    service = _built_service("anthropic/claude-haiku-4.5", reserve_tokens_floor=20_000)

    assert service.compaction_threshold == 180_000
    assert service.should_compact(179_000, "s") is False
    assert service.should_compact(181_000, "s") is True


def test_reserve_larger_than_window_is_rejected_at_config_time():
    from flowly.config.schema import CompactionConfig as SchemaCompactionConfig

    with pytest.raises(ValueError, match="reserveTokensFloor"):
        SchemaCompactionConfig(context_window=32_000, reserve_tokens_floor=32_000)


def test_microcompact_and_keep_recent_are_configurable():
    service = _built_service(
        "anthropic/claude-haiku-4.5",
        microcompact={"keep_recent_full": 2, "truncate_chars": 60},
        keep_recent={"max_tokens": 3_000},
    )

    assert service.config.microcompact.keep_recent_full == 2
    assert service.config.microcompact.truncate_chars == 60
    assert service.config.keep_recent.max_tokens == 3_000


def test_session_counters_do_not_grow_without_bound():
    service = _service(_Provider(LLMResponse(content="s", finish_reason="stop")))

    for i in range(service.MAX_TRACKED_SESSIONS + 250):
        service.record_compaction_failure(f"session-{i}")

    assert len(service._sessions) <= service.MAX_TRACKED_SESSIONS + 1
    # The newest session keeps its state despite the eviction.
    newest = f"session-{service.MAX_TRACKED_SESSIONS + 249}"
    assert service._state(newest).consecutive_failures == 1
