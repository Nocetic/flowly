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
    SUMMARY_MARKER,
    SUMMARY_REFERENCE_PREAMBLE,
    CompactionConfig,
    CompactionError,
    KeepRecentConfig,
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
    assert SUMMARY_REFERENCE_PREAMBLE in messages[0]["content"]
    assert any(
        message.get("role") == "user" and "question 39" in message.get("content", "")
        for message in messages[1:]
    )
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


def test_flowly_proxy_cap_is_the_model_window():
    # The proxy's input ceiling IS the model's window now (flowly-app's
    # modelMaxInputTokens mirrors the same family table) — an 80K flat cap
    # used to choke a 200K Claude for no reason.
    service = _built_service("anthropic/claude-haiku-4.5", provider_name="flowly")

    assert service.model_context_window == 200_000
    assert service.effective_context_window == 200_000


def test_flowly_proxy_unknown_family_falls_back_conservatively():
    # An unknown family gets the backend's fallback ceiling (128K): the bot
    # must never budget more than the proxy will accept.
    service = _built_service("acme/mystery-model-9", provider_name="flowly")

    assert service.effective_context_window <= 128_000


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


# ── 6. The trigger must be about history, not request size ────────────────


def test_a_large_system_prompt_alone_does_not_trigger_compaction():
    """The bug this pins: a real install carries ~70K of system prompt and
    tool schemas on EVERY request. Judging the trigger on the total meant
    saying "hello" tripped the threshold, the agent tried to compact a
    two-message chat, could not reduce anything, and reported that failure
    to the user every single turn."""
    service = _service(
        _Provider(LLMResponse(content="s", finish_reason="stop")),
        context_window=120_000,
        reserve_tokens_floor=20_000,
    )

    # "hello" — 45 tokens of history behind 70K of fixed overhead.
    assert service.should_compact(45, "s1", overhead_tokens=70_000) is False


def test_history_that_fills_the_remaining_room_does_trigger():
    service = _service(
        _Provider(LLMResponse(content="s", finish_reason="stop")),
        context_window=120_000,
        reserve_tokens_floor=20_000,
    )

    # Budget = 120k - 20k reserve - 70k overhead = 30k for history.
    assert service.should_compact(29_000, "s1", overhead_tokens=70_000) is False
    assert service.should_compact(31_000, "s1", overhead_tokens=70_000) is True


def test_overhead_bigger_than_the_window_refuses_instead_of_looping():
    """Nothing compaction removes can make this request fit. Trying anyway
    burns a summarisation call per turn to change nothing."""
    service = _service(
        _Provider(LLMResponse(content="s", finish_reason="stop")),
        context_window=66_000,
        reserve_tokens_floor=3_000,
    )

    assert service.should_compact(500, "s1", overhead_tokens=70_000) is False
    assert service.history_budget(70_000) <= 0


def test_memory_flush_follows_the_same_signal():
    """It exists to save memories shortly before a compaction, so firing it
    on total size made it run every turn of an idle chat."""
    service = _service(
        _Provider(LLMResponse(content="s", finish_reason="stop")),
        context_window=120_000,
        reserve_tokens_floor=20_000,
    )

    assert service.should_memory_flush(45, "s1", overhead_tokens=70_000) is False
    assert service.should_memory_flush(29_000, "s1", overhead_tokens=70_000) is True


def test_budget_shrinks_as_overhead_grows():
    service = _service(
        _Provider(LLMResponse(content="s", finish_reason="stop")),
        context_window=100_000,
        reserve_tokens_floor=10_000,
    )

    assert service.history_budget(0) == 90_000
    assert service.history_budget(30_000) == 60_000


def test_the_preserved_tail_scales_with_the_room_available():
    """The second live failure. keep_recent's cap was an absolute 20K, so on a
    small budget it preserved 93% of an 8K history verbatim, left almost
    nothing to summarise, and produced a "compaction" that GREW the context:

        Keeping 13 recent messages (~7716 tokens), summarizing 8
        would not reduce context (8269 -> 8351 tokens)
    """
    service = _service(_Provider(LLMResponse(content="s", finish_reason="stop")))

    assert service._keep_recent_cap(6_300) < 6_300 / 2, "tail must leave room to compress"
    assert service._keep_recent_cap(0) == service.config.keep_recent.max_tokens
    # Never exceeds the absolute ceiling on a large window.
    assert service._keep_recent_cap(10_000_000) == service.config.keep_recent.max_tokens


async def test_the_exact_live_failure_now_reduces():
    """Reproduces the logged numbers: ~8K of history inside a ~6.3K budget."""
    provider = _Provider(LLMResponse(content="## Decisions\nShort.", finish_reason="stop"))
    service = _service(provider, context_window=79_000, reserve_tokens_floor=3_000)
    budget = service.history_budget(69_700)

    messages = _conversation(21, filler="word " * 90)
    result = await service.compact(messages, history_budget=budget)

    assert result.tokens_after < result.tokens_before
    assert result.tokens_after <= budget, "the point is to fit in the room we have"


async def test_a_tiny_budget_still_produces_a_reduction():
    provider = _Provider(LLMResponse(content="Summary.", finish_reason="stop"))
    service = _service(provider, context_window=20_000, reserve_tokens_floor=2_000)

    messages = _conversation(30)
    result = await service.compact(messages, history_budget=1_500)

    assert result.tokens_after < result.tokens_before


# ── The preserved tail must never be empty ────────────────────────────────


def _tool_heavy_block(user_words: int = 8, tool_chars: int = 40_000) -> list[dict]:
    """One turn whose tool traffic dwarfs its words — the live 'kept 0
    recent' shape: the newest block alone busts the share cap."""
    return [
        {"role": "user", "content": "check the deploy logs " + "word " * user_words},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "c1", "function": {"name": "exec", "arguments": "{}"}}],
        },
        {"role": "tool", "tool_call_id": "c1", "name": "exec",
         "content": "log line " * (tool_chars // 9)},
        {"role": "assistant", "content": "The deploy failed on shard nine."},
    ]


def test_a_cap_busting_newest_block_is_kept_when_it_fits_the_budget():
    """Share cap says no, but half the history budget says yes: the user's
    last exchange survives whole instead of 'kept 0 recent'."""
    service = _service(_Provider(LLMResponse(content="s", finish_reason="stop")))
    block = _tool_heavy_block(tool_chars=12_000)  # ~3K tokens
    messages = _conversation(10) + block
    budget = 8_000  # share cap = 2K < block; half-budget limit = 4K ≥ block
    from flowly.compaction.estimator import estimate_messages_tokens

    block_tokens = estimate_messages_tokens(block)
    assert block_tokens > service._keep_recent_cap(budget), "must bust the cap"
    assert block_tokens <= budget // 2, "must fit the half-budget limit"

    kept = service._calculate_keep_recent(messages, history_budget=budget)

    assert kept == block, (
        "the newest block must survive whole instead of 'kept 0 recent'"
    )


def test_a_block_too_big_for_the_budget_degrades_to_question_and_answer():
    service = _service(_Provider(LLMResponse(content="s", finish_reason="stop")))
    block = _tool_heavy_block(tool_chars=60_000)  # ~15K tokens of tool output
    messages = _conversation(10) + block

    kept = service._calculate_keep_recent(messages, history_budget=6_000)

    assert kept, "the tail must never be empty"
    roles = [m.get("role") for m in kept]
    assert roles[0] == "user"
    assert roles[-1] == "assistant"
    assert all(r != "tool" for r in roles), "tool traffic belongs to the summary"
    assert all(not m.get("tool_calls") for m in kept), "no dangling tool calls"
    assert "deploy failed on shard nine" in kept[-1]["content"]


def test_newest_user_request_is_never_replaced_only_by_a_summary() -> None:
    service = _service(_Provider(LLMResponse(content="s", finish_reason="stop")))
    newest = {
        "role": "user",
        "content": "ACTIVE-REQUEST " + ("critical detail " * 20_000),
    }
    messages = _conversation(10) + [newest]

    kept = service._calculate_keep_recent(messages, history_budget=2_000)

    assert any(message is newest for message in kept)
    assert kept[-1]["content"] == newest["content"]


def test_newest_user_protection_is_not_disabled_with_tail_optimization() -> None:
    service = _service(_Provider(LLMResponse(content="s", finish_reason="stop")))
    service.config.keep_recent = KeepRecentConfig(enabled=False)
    messages = _conversation(6)
    newest = messages[-2]

    kept = service._calculate_keep_recent(messages, history_budget=2_000)

    assert kept == [newest]


async def test_an_extracted_tail_still_summarizes_the_whole_block():
    """Suffix arithmetic on an extracted tail would silently drop the block's
    tool traffic from BOTH the summary and the tail. Extract mode must feed
    the full history to the summarizer instead."""
    provider = _Provider(
        LLMResponse(content="A summary of the deploy investigation.",
                    finish_reason="stop")
    )
    service = _service(provider)
    block = _tool_heavy_block(tool_chars=60_000)
    messages = _conversation(10) + block

    result = await service.compact(messages, history_budget=6_000)

    assert [m.get("role") for m in result.kept_messages] == ["user", "assistant"]
    assert provider.saw("check the deploy logs"), (
        "the oversized block never reached the summarizer"
    )
    # The reported count is what a person would count, so it is SMALLER than
    # the protocol delta (this block's tool frames are not messages to them).
    assert 0 < result.messages_removed < len(messages) - len(result.kept_messages)


# ── Small models must still be able to compact ────────────────────────────


def test_a_reserve_larger_than_the_window_cannot_wedge_compaction():
    """The default reserve (20K) is sized for a 128K window. Applied literally
    to a 16,385-token model it exceeded the window: threshold collapsed to 1,
    the history budget went NEGATIVE, and should_compact then refused every
    turn with "fixed overhead leaves no room" — so the request never fit and
    compaction could never help."""
    from flowly.config.schema import CompactionConfig as SchemaConfig

    service = _service(
        _Provider(LLMResponse(content="s", finish_reason="stop")),
        context_window=16_385,
        context_window_explicit=True,
        reserve_tokens_floor=SchemaConfig().reserve_tokens_floor,  # the real default
    )

    assert service.effective_reserve_tokens < service.effective_context_window
    assert service.compaction_threshold > 1
    assert service.history_budget(5_000) > 0
    assert service.should_compact(50_000, "s", overhead_tokens=5_000), (
        "a 16K model with an over-full history still could not compact"
    )


@pytest.mark.parametrize("window", [4_000, 8_192, 16_385, 32_768, 128_000, 1_000_000])
def test_the_reserve_always_leaves_room_for_a_conversation(window):
    service = _service(
        _Provider(LLMResponse(content="s", finish_reason="stop")),
        context_window=window,
        context_window_explicit=True,
        reserve_tokens_floor=20_000,
    )

    assert 0 < service.effective_reserve_tokens < window
    assert service.compaction_threshold > 0
    assert service.history_budget() > 0


def test_a_large_window_keeps_the_configured_reserve():
    """The clamp is a ceiling for small models, not a rewrite for everyone."""
    service = _service(
        _Provider(LLMResponse(content="s", finish_reason="stop")),
        context_window=200_000,
        context_window_explicit=True,
        reserve_tokens_floor=20_000,
    )

    assert service.effective_reserve_tokens == 20_000


# ── The reported count is the one a person can verify ─────────────────────


def test_tool_traffic_is_not_counted_as_messages():
    """A three-exchange chat reported "12 msgs summarized" because tool-call
    frames, tool results and the previous summary are all messages to the
    model. The number is only ever shown to a human, who counts bubbles."""
    from flowly.compaction.service import count_conversational_messages

    history = [
        {"role": "system", "content": f"{SUMMARY_MARKER}\n\nearlier work"},
        {"role": "user", "content": "read the log"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c1"}]},
        {"role": "tool", "tool_call_id": "c1", "content": "…4000 lines…"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c2"}]},
        {"role": "tool", "tool_call_id": "c2", "content": "…"},
        {"role": "assistant", "content": "Here is what the log says."},
        {"role": "user", "content": "thanks"},
        {"role": "assistant", "content": "anytime"},
    ]

    assert len(history) == 9
    assert count_conversational_messages(history) == 4


def test_multimodal_turns_still_count():
    from flowly.compaction.service import count_conversational_messages

    assert count_conversational_messages([
        {"role": "user", "content": [{"type": "image_url"}]},
    ]) == 1


def test_empty_turns_do_not_count():
    from flowly.compaction.service import count_conversational_messages

    assert count_conversational_messages([
        {"role": "assistant", "content": ""},
        {"role": "assistant", "content": "   "},
    ]) == 0


async def test_the_result_reports_what_the_user_can_count():
    provider = _Provider(LLMResponse(content="A summary.", finish_reason="stop"))
    service = _service(provider, context_window=2_000, reserve_tokens_floor=100)
    history: list[dict] = []
    for i in range(6):
        history.extend(_tool_turn(i))

    result = await service.compact(history)

    protocol_frames = len(history) - len(result.kept_messages)
    assert result.messages_removed < protocol_frames, (
        "tool frames are being reported to the user as messages again"
    )
    assert result.messages_removed > 0


def test_internal_triggers_wearing_the_user_role_do_not_count():
    """Subagent announces and board results are written as user turns so the
    agent reacts to them, but nobody typed them. Counting them would report
    messages the user cannot find in their own transcript."""
    from flowly.compaction.service import count_conversational_messages

    assert count_conversational_messages([
        {"role": "user", "content": "[System: subagent] finished", "_display_hidden": True},
        {"role": "user", "content": "what did it find?"},
        {"role": "assistant", "content": "It found three retries."},
    ]) == 2


def test_one_definition_of_a_message_across_the_feature():
    """Eligibility used to filter by role alone, so a single exchange wrapped
    in three tool calls counted as five and passed a threshold that means
    'there is a conversation here worth summarising'."""
    from flowly.agent.loop import AgentLoop
    from flowly.compaction.service import count_conversational_messages

    history = [
        {"role": "user", "content": "read the log"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c1"}]},
        {"role": "tool", "tool_call_id": "c1", "content": "…"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c2"}]},
        {"role": "tool", "tool_call_id": "c2", "content": "…"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c3"}]},
        {"role": "tool", "tool_call_id": "c3", "content": "…"},
        {"role": "assistant", "content": "Here it is."},
    ]

    reason = AgentLoop._compaction_ineligible_reason(history)

    assert reason is not None and "Not enough messages" in reason
    assert f"({count_conversational_messages(history)} messages)" in reason, (
        "the refusal reports a different count than the feature's own"
    )


def test_the_compaction_seam_is_not_itself_a_message():
    """The boundary row a compaction leaves is a divider. Counted, a resumed
    session reported one more message than the user could see — and every
    compaction added another phantom."""
    from flowly.compaction.service import count_conversational_messages
    from flowly.compaction.types import CONTEXT_BOUNDARY_CONTENT

    history = [
        {"role": "user", "content": "read the log"},
        {"role": "assistant", "content": "here it is"},
        {"role": "assistant", "content": CONTEXT_BOUNDARY_CONTENT,
         "kind": "context_boundary"},
        {"role": "user", "content": "thanks"},
    ]

    assert count_conversational_messages(history) == 3


def test_a_boundary_written_before_the_typed_field_still_does_not_count():
    from flowly.compaction.service import count_conversational_messages
    from flowly.compaction.types import CONTEXT_BOUNDARY_CONTENT

    assert count_conversational_messages([
        {"role": "assistant", "content": CONTEXT_BOUNDARY_CONTENT},
    ]) == 0


def test_wire_shaped_history_counts_the_same():
    """chat.history hands clients content as a list of blocks, not a string."""
    from flowly.compaction.service import count_conversational_messages

    assert count_conversational_messages([
        {"role": "user", "content": [{"type": "text", "text": "read the log"}]},
        {"role": "assistant", "content": [{"type": "text", "text": ""}],
         "tool_calls": [{"id": "c1"}]},
        {"role": "tool", "tool_call_id": "c1",
         "content": [{"type": "text", "text": "…"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "here it is"}]},
    ]) == 2


def test_a_materialised_default_is_not_an_operator_pin():
    """config.json is written with model_dump(), so every default is
    materialised into the file and every field looks "set". Read literally,
    that made contextWindow explicit for EVERY user and killed model-aware
    detection outright — a Gemini or Claude user was capped at the 128K
    fallback while the catalog knew better."""
    from flowly.compaction.builder import build_compaction_config
    from flowly.config.schema import CompactionConfig as SchemaConfig

    # What the loader produces from a file containing the whole block.
    written_out = SchemaConfig.model_validate(SchemaConfig().model_dump())

    assert "context_window" in written_out.model_fields_set, (
        "this test is meaningless unless the field really does look set"
    )
    assert not build_compaction_config(written_out).context_window_explicit


def test_a_deliberate_pin_still_wins():
    """An operator who caps the window on purpose must not be second-guessed
    by a catalog that thinks the model is bigger."""
    from flowly.compaction.builder import build_compaction_config
    from flowly.config.schema import CompactionConfig as SchemaConfig

    pinned = SchemaConfig(context_window=32_000)

    assert build_compaction_config(pinned).context_window_explicit


def test_detection_reaches_the_model_when_nothing_is_pinned():
    from flowly.compaction.builder import build_compaction_config
    from flowly.config.schema import CompactionConfig as SchemaConfig

    config = build_compaction_config(
        SchemaConfig.model_validate(SchemaConfig().model_dump())
    )
    service = CompactionService(
        provider=_Provider(LLMResponse(content="s", finish_reason="stop")),
        model="anthropic/claude-haiku-4.5",
        config=config,
    )

    assert service.effective_context_window == 200_000, (
        "a 200K model was still being compacted as if it held 128K"
    )
