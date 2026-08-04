"""Two compactions of one session must not overwrite each other.

Manual ``/compact`` is reachable from the TUI, the desktop, every chat
channel and any second device, while automatic compaction runs inside a turn.
Both end in a commit that clears the session and rewrites it, and
``SessionManager.get_or_create`` hands every caller the SAME session object —
so an interleaving commits a summary derived from history the other one has
already replaced.
"""

import asyncio

import pytest

from flowly.compaction.service import CompactionService
from flowly.compaction.types import CompactionConfig
from flowly.providers.base import LLMResponse


class _SlowProvider:
    """Summarizes slowly, so two callers genuinely overlap."""

    provider_name = "stub"

    def __init__(self, delay: float = 0.05):
        self.delay = delay
        self.concurrent = 0
        self.max_concurrent = 0

    async def chat(self, *args, **kwargs) -> LLMResponse:
        self.concurrent += 1
        self.max_concurrent = max(self.max_concurrent, self.concurrent)
        try:
            await asyncio.sleep(self.delay)
            return LLMResponse(content="A summary.", finish_reason="stop")
        finally:
            self.concurrent -= 1


def _service(provider) -> CompactionService:
    return CompactionService(
        provider=provider,
        model="m",
        config=CompactionConfig(mode="default", context_window=2_000,
                                reserve_tokens_floor=100),
    )


async def test_one_session_compacts_one_at_a_time():
    provider = _SlowProvider()
    service = _service(provider)

    async def compact_once():
        async with service.session_lock("chat-1"):
            await provider.chat(messages=[])

    await asyncio.gather(*(compact_once() for _ in range(4)))

    assert provider.max_concurrent == 1, (
        "compactions for one session overlapped; the loser would commit a "
        "summary of history the winner already replaced"
    )


async def test_different_sessions_do_not_block_each_other():
    provider = _SlowProvider()
    service = _service(provider)

    async def compact(session_key: str):
        async with service.session_lock(session_key):
            await provider.chat(messages=[])

    await asyncio.gather(compact("a"), compact("b"), compact("c"))

    assert provider.max_concurrent > 1, (
        "a slow compaction in one conversation must not stall the others"
    )


async def test_lock_is_released_when_compaction_raises():
    service = _service(_SlowProvider())

    with pytest.raises(RuntimeError):
        async with service.session_lock("chat-1"):
            raise RuntimeError("summarizer exploded")

    # A wedged lock would hang every later compaction for this session.
    await asyncio.wait_for(
        service.session_lock("chat-1").acquire(), timeout=1.0,
    )
    service.session_lock("chat-1").release()


async def test_a_busy_session_is_never_evicted():
    """Evicting mid-compaction would hand the next caller a fresh lock and
    silently undo the exclusion the in-flight commit depends on."""
    service = _service(_SlowProvider())
    busy = "busy-session"

    async with service.session_lock(busy):
        for i in range(service.MAX_TRACKED_SESSIONS + 50):
            service.record_compaction_failure(f"filler-{i}")

        assert busy in service._sessions
        assert service._sessions[busy].lock.locked()


# ── Generation guard ──────────────────────────────────────────────────────


class _Session:
    """Just the surface ``_compaction_generation`` reads."""

    def __init__(self, count: int = 0):
        self.metadata = {"compaction_count": count}


def test_generation_tracks_the_committed_compaction_count():
    from flowly.agent.loop import AgentLoop

    assert AgentLoop._compaction_generation(_Session(0)) == 0
    assert AgentLoop._compaction_generation(_Session(7)) == 7


@pytest.mark.parametrize("metadata", [{}, {"compaction_count": None},
                                      {"compaction_count": "junk"}])
def test_generation_survives_a_missing_or_broken_counter(metadata):
    from flowly.agent.loop import AgentLoop

    session = _Session()
    session.metadata = metadata

    assert AgentLoop._compaction_generation(session) == 0


def test_generation_changes_after_a_commit_so_a_stale_writer_can_tell():
    from flowly.agent.loop import AgentLoop

    session = _Session(3)
    before = AgentLoop._compaction_generation(session)

    # What _commit_compaction does to the counter.
    session.metadata["compaction_count"] = session.metadata["compaction_count"] + 1

    assert AgentLoop._compaction_generation(session) != before


# ── Whole-list tool-pair integrity ────────────────────────────────────────


def _assistant_call(call_id: str, *, key: str = "id", content: str = ""):
    return {
        "role": "assistant",
        "content": content,
        "tool_calls": [{key: call_id, "type": "function",
                        "function": {"name": "run", "arguments": "{}"}}],
    }


def _tool_result(call_id: str):
    return {"role": "tool", "tool_call_id": call_id, "name": "run", "content": "ok"}


def _sanitize(messages):
    from flowly.session.manager import _drop_orphan_tool_pairs

    return _drop_orphan_tool_pairs(messages)


def test_a_well_formed_list_is_returned_unchanged():
    messages = [
        {"role": "user", "content": "go"},
        _assistant_call("c1"),
        _tool_result("c1"),
        {"role": "assistant", "content": "done"},
    ]

    assert _sanitize(messages) is messages


def test_orphan_result_at_the_head_is_removed():
    """The tail-only repair cannot see this one."""
    messages = [
        _tool_result("gone"),
        {"role": "user", "content": "next"},
        {"role": "assistant", "content": "hi"},
    ]

    result = _sanitize(messages)

    assert all(m.get("role") != "tool" for m in result)
    assert len(result) == 2


def test_unanswered_call_is_stripped_but_the_words_survive():
    messages = [
        {"role": "user", "content": "go"},
        _assistant_call("never-answered", content="Let me check that."),
        {"role": "user", "content": "actually never mind"},
    ]

    result = _sanitize(messages)

    assistant = [m for m in result if m.get("role") == "assistant"][0]
    assert "tool_calls" not in assistant
    assert assistant["content"] == "Let me check that."


def test_partially_answered_batch_keeps_only_the_answered_call():
    messages = [
        {"role": "user", "content": "go"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "a", "function": {"name": "run", "arguments": "{}"}},
                {"id": "b", "function": {"name": "run", "arguments": "{}"}},
            ],
        },
        _tool_result("a"),
    ]

    result = _sanitize(messages)

    calls = [m for m in result if m.get("role") == "assistant"][0]["tool_calls"]
    assert [c["id"] for c in calls] == ["a"]


def test_call_id_field_is_honoured_when_it_differs_from_id():
    """Some provider formats carry both; results are keyed by call_id."""
    messages = [
        {"role": "user", "content": "go"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "internal-1", "call_id": "wire-1",
                            "function": {"name": "run", "arguments": "{}"}}],
        },
        _tool_result("wire-1"),
    ]

    result = _sanitize(messages)

    assert result is messages, "a valid pair keyed by call_id must not be touched"


def test_sanitizer_runs_as_part_of_get_history():
    from flowly.session.manager import Session

    session = Session(key="cli:x")
    session.add_message("tool", "orphan result", tool_call_id="ghost", name="run")
    session.add_message("user", "hello")
    session.add_message("assistant", "hi")

    history = session.get_history(max_messages=10)

    assert all(m.get("role") != "tool" for m in history)


def test_both_repairs_agree_on_which_id_a_result_references():
    """The tail repair and the whole-list pass must read the SAME id field.
    If they disagree, each 'fixes' what the other considers valid — and a
    complete pair keyed by call_id looks like an orphan to one of them."""
    from flowly.session.manager import _repair_tool_sequence

    messages = [
        {"role": "user", "content": "go"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "internal-1", "call_id": "wire-1",
                            "function": {"name": "run", "arguments": "{}"}}],
        },
        _tool_result("wire-1"),
    ]

    repaired = _repair_tool_sequence([dict(m) for m in messages])

    assert len(repaired) == 3, (
        "a complete call/result pair keyed by call_id was trimmed as an orphan"
    )
    assert repaired[-1]["role"] == "tool"


# ── Eligibility is judged against what we actually hold ───────────────────


def _eligible(history):
    from flowly.agent.loop import AgentLoop

    return AgentLoop._compaction_ineligible_reason(history)


def _chat(turns: int, words: int = 60):
    filler = "word " * words
    out = []
    for i in range(turns):
        out.append({"role": "user", "content": f"q{i} {filler}"})
        out.append({"role": "assistant", "content": f"a{i} {filler}"})
    return out


def test_a_substantial_conversation_is_eligible():
    assert _eligible(_chat(10)) is None


@pytest.mark.parametrize(
    "history,expected",
    [
        ([], "No history"),
        ([{"role": "system", "content": "[Previous conversation summary]\n\nx"}],
         "Already compacted"),
        (_chat(1), "Not enough messages"),
        (_chat(3, words=1), "too small"),
    ],
)
def test_ineligible_histories_are_named(history, expected):
    reason = _eligible(history)

    assert reason is not None
    assert expected in reason


def test_a_history_shrunk_by_a_concurrent_compaction_becomes_ineligible():
    """The second device to ask for /compact re-checks after the lock. Without
    that it would spend an LLM call summarising the tiny result of the first
    compaction, then report the outcome as a failure."""
    before = _chat(20)
    assert _eligible(before) is None

    # What the winner leaves behind: a summary plus a short kept tail.
    after = [
        {"role": "system", "content": "[Previous conversation summary]\n\nprior work"},
        {"role": "user", "content": "ok"},
    ]

    assert _eligible(after) is not None


async def test_the_session_lock_is_not_reentrant():
    """The auto-compaction path holds this lock across summarisation while the
    /compact command path returns before reaching it. That early return is
    load-bearing: re-entering here would wait on ourselves forever."""
    service = _service(_SlowProvider())

    async with service.session_lock("s1"):
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(
                service.session_lock("s1").acquire(), timeout=0.2,
            )
