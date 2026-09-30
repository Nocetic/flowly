"""A streamed summary call is bounded by progress, not by a fixed total.

Updating a running summary writes the whole record out each time. In a long
run, calls crossed the fixed 120 s bound 23 times while still producing
output; each failure left the history over budget until the next attempt.
The bounds are scaled down here so the tests run in about a second.
"""
from __future__ import annotations

import asyncio

import pytest

from flowly.compaction import summarizer
from flowly.compaction.summarizer import _chat_bounded, validated_summary_text
from flowly.compaction.types import CompactionError
from flowly.providers.base import LLMProvider, LLMResponse

MESSAGES = [{"role": "user", "content": "summarise"}]


@pytest.fixture(autouse=True)
def _fast_bounds(monkeypatch):
    monkeypatch.setattr(summarizer, "SUMMARY_STREAM_IDLE_SECONDS", 0.3)
    monkeypatch.setattr(summarizer, "SUMMARY_STREAM_TOTAL_SECONDS", 1.5)
    monkeypatch.setattr(summarizer, "SUMMARY_CALL_TIMEOUT_SECONDS", 0.5)
    monkeypatch.setattr(summarizer, "_CALL_POLL_SECONDS", 0.02)
    monkeypatch.setattr(summarizer, "_CANCEL_GRACE_SECONDS", 0.1)


class _Stream:
    """A provider whose stream yields ``parts`` with ``gap`` seconds between them."""

    provider_name = "stub"

    def __init__(self, parts, gap=0.1, fail_after=None, forever=False):
        self.parts = parts
        self.gap = gap
        self.fail_after = fail_after
        self.forever = forever
        self.chat_calls = 0

    async def chat_stream(self, messages, model=None, max_tokens=0, **_):
        index = 0
        while True:
            if self.fail_after is not None and index == self.fail_after:
                raise RuntimeError("stream broke")
            if index >= len(self.parts) and not self.forever:
                return
            await asyncio.sleep(self.gap)
            yield self.parts[index % len(self.parts)]
            index += 1

    async def chat(self, *args, **kwargs):
        self.chat_calls += 1
        return LLMResponse(content="## Decisions\nfrom the plain call", finish_reason="stop")


def _text(*chunks, finish="stop"):
    return [LLMResponse(content=chunk, finish_reason="") for chunk in chunks] + [
        LLMResponse(content=None, finish_reason=finish)
    ]


async def test_a_slow_but_steady_stream_outlives_the_fixed_bound():
    # 1.2 s of output in 0.1 s steps: past the 0.5 s fixed bound, never idle.
    provider = _Stream(_text(*[f"part {i} " for i in range(11)]))
    response = await _chat_bounded(provider, messages=MESSAGES, model="m", max_tokens=100)
    assert response.content == "".join(f"part {i} " for i in range(11))
    assert response.finish_reason == "stop"
    assert provider.chat_calls == 0


async def test_a_stalled_stream_fails_as_no_progress():
    provider = _Stream(_text("first ", "second"), gap=0.6)
    with pytest.raises(CompactionError, match="no progress"):
        await _chat_bounded(provider, messages=MESSAGES, model="m", max_tokens=100)


async def test_a_stream_that_never_ends_hits_the_overall_cap():
    provider = _Stream([LLMResponse(content="more ", finish_reason="")], forever=True)
    # The outer bound turns a missing cap into a failure instead of a hang.
    with pytest.raises(CompactionError, match="timed out after"):
        await asyncio.wait_for(
            _chat_bounded(provider, messages=MESSAGES, model="m", max_tokens=100), timeout=5,
        )


async def test_a_truncated_stream_is_still_rejected():
    provider = _Stream(_text("half a sum", finish="length"))
    response = await _chat_bounded(provider, messages=MESSAGES, model="m", max_tokens=100)
    with pytest.raises(CompactionError, match="cut off"):
        validated_summary_text(response)


async def test_an_error_reported_in_the_stream_is_still_rejected():
    provider = _Stream([LLMResponse(content="Error calling LLM: overloaded", finish_reason="error")])
    response = await _chat_bounded(provider, messages=MESSAGES, model="m", max_tokens=100)
    with pytest.raises(CompactionError, match="provider call failed"):
        validated_summary_text(response)


async def test_a_stream_that_fails_before_any_output_falls_back_to_one_request():
    provider = _Stream(_text("never"), fail_after=0)
    response = await _chat_bounded(provider, messages=MESSAGES, model="m", max_tokens=100)
    assert provider.chat_calls == 1
    assert "from the plain call" in response.content


async def test_a_stream_that_breaks_midway_is_not_papered_over():
    provider = _Stream(_text("first ", "second "), fail_after=1)
    with pytest.raises(RuntimeError, match="stream broke"):
        await _chat_bounded(provider, messages=MESSAGES, model="m", max_tokens=100)
    assert provider.chat_calls == 0


async def test_stop_cancels_a_stream_in_flight():
    provider = _Stream([LLMResponse(content="more ", finish_reason="")], forever=True)
    calls = {"n": 0}

    def cancelled() -> bool:
        calls["n"] += 1
        return calls["n"] > 5

    with pytest.raises(CompactionError, match="cancelled"):
        await asyncio.wait_for(
            _chat_bounded(provider, messages=MESSAGES, model="m", max_tokens=100, should_cancel=cancelled),
            timeout=5,
        )


class _PlainOnly:
    provider_name = "stub"

    def __init__(self):
        self.calls = 0

    async def chat(self, *args, **kwargs):
        self.calls += 1
        return LLMResponse(content="## Decisions\nplain", finish_reason="stop")


class _BaseFallback(LLMProvider):
    """Inherits the base chat_stream, which only replays chat()."""

    def __init__(self):
        self.calls = 0

    async def chat(self, *args, **kwargs):
        self.calls += 1
        await asyncio.sleep(0.8)  # past the scaled 0.5 s fixed bound
        return LLMResponse(content="late", finish_reason="stop")

    def get_default_model(self) -> str:
        return "m"


async def test_a_provider_without_streaming_uses_one_bounded_request():
    provider = _PlainOnly()
    response = await _chat_bounded(provider, messages=MESSAGES, model="m", max_tokens=100)
    assert provider.calls == 1 and response.content.endswith("plain")


async def test_the_base_replay_is_not_treated_as_a_stream():
    # Streaming through the base class would lift the fixed bound for a call
    # that reports no progress at all; it must keep the fixed bound.
    provider = _BaseFallback()
    with pytest.raises(CompactionError, match="timed out after"):
        await _chat_bounded(provider, messages=MESSAGES, model="m", max_tokens=100)
