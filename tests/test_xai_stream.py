"""xAI Responses SSE streaming (XAIResponsesProvider.chat_stream).

xai_oauth credentials aren't available in CI, so we drive the parser with a
fake httpx client that replays canned SSE frames in the exact shape the xAI /
OpenAI Responses API emits. Guards the contract loop.py:_chat_with_stream
consumes: text deltas (finish_reason="") then ONE final chunk (content=None,
finish_reason set, tool_calls + usage).
"""

import asyncio

import flowly.providers.xai_responses_provider as xai
from flowly.providers.xai_responses_provider import XAIResponsesProvider


class _FakeStream:
    def __init__(self, status_code, lines):
        self.status_code = status_code
        self._lines = lines

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def aiter_lines(self):
        for ln in self._lines:
            yield ln

    async def aread(self):
        return b""


class _FakeClient:
    def __init__(self, lines, status=200):
        self._lines = lines
        self._status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def stream(self, method, url, **kw):
        return _FakeStream(self._status, self._lines)


def _provider():
    return XAIResponsesProvider(api_key="test-key", api_base=None, default_model="grok-4.3")


def _run(lines, status=200):
    prov = _provider()
    xai.httpx.AsyncClient = lambda **kw: _FakeClient(lines, status)  # type: ignore
    out = []

    async def go():
        async for ch in prov.chat_stream(messages=[{"role": "user", "content": "hi"}]):
            out.append(ch)

    asyncio.run(go())
    return out


def test_text_deltas_then_final():
    lines = [
        'data: {"type":"response.output_text.delta","delta":"Hel"}',
        '',
        'data: {"type":"response.output_text.delta","delta":"lo"}',
        '',
        'data: {"type":"response.completed","response":{"status":"completed",'
        '"output":[{"type":"message","content":[{"type":"output_text","text":"Hello"}]}],'
        '"usage":{"input_tokens":3,"output_tokens":2,"total_tokens":5}}}',
        '',
    ]
    out = _run(lines)
    deltas = [c.content for c in out if c.content]
    assert deltas == ["Hel", "lo"]
    final = out[-1]
    assert final.content is None
    assert final.finish_reason == "stop"
    assert not final.tool_calls


def test_tool_call_streamed_in_final():
    lines = [
        'data: {"type":"response.completed","response":{"status":"completed",'
        '"output":[{"type":"function_call","name":"get_weather",'
        '"arguments":"{\\"city\\":\\"Istanbul\\"}","call_id":"call_1"}],'
        '"usage":{"input_tokens":4,"output_tokens":1,"total_tokens":5}}}',
        '',
    ]
    out = _run(lines)
    final = out[-1]
    assert final.finish_reason == "tool_calls"
    assert len(final.tool_calls) == 1
    assert final.tool_calls[0].name == "get_weather"
    assert final.tool_calls[0].arguments == {"city": "Istanbul"}


def test_no_deltas_full_text_in_completed_still_delivered():
    # Some turns send only the final payload (no output_text.delta). The reply
    # must still reach the user (as one chunk) rather than vanish.
    lines = [
        'data: {"type":"response.completed","response":{"status":"completed",'
        '"output":[{"type":"message","content":[{"type":"output_text","text":"Whole answer"}]}]}}',
        '',
    ]
    out = _run(lines)
    deltas = [c.content for c in out if c.content]
    assert deltas == ["Whole answer"]
    assert out[-1].content is None
    assert out[-1].finish_reason == "stop"


def test_stream_failure_falls_back_to_blocking(monkeypatch):
    # A stream that closes with no recognizable output must not leave the turn
    # empty — it falls back to the blocking chat() call.
    async def _fake_chat(**kw):
        from flowly.providers.base import LLMResponse
        return LLMResponse(content="fallback reply", finish_reason="stop")

    prov = _provider()
    monkeypatch.setattr(prov, "chat", _fake_chat)
    xai.httpx.AsyncClient = lambda **kw: _FakeClient(['data: {"type":"noise"}', ''], 200)  # type: ignore

    out = []

    async def go():
        async for ch in prov.chat_stream(messages=[{"role": "user", "content": "hi"}]):
            out.append(ch)

    asyncio.run(go())
    assert any(c.content == "fallback reply" for c in out)
