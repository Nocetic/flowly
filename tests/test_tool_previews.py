"""Tool preparation is observable before completion and never executable."""

import json

import httpx
import pytest
from openai import AsyncOpenAI

from flowly.providers.openrouter_provider import OpenRouterProvider


@pytest.mark.asyncio
async def test_openrouter_previews_precede_completion_without_becoming_calls():
    consumed = []
    deltas = [
        {"content": "Checking."},
        {"tool_calls": [{"index": 0, "id": "call-first", "type": "function",
                         "function": {"name": "read_file", "arguments": '{"path":'}}]},
        {"tool_calls": [{"index": 1, "id": "call-second", "type": "function",
                         "function": {"name": "exec", "arguments": '{"command":"pwd"}'}}]},
        {"tool_calls": [{"index": 0, "function": {"arguments": '"report.md"}'}}]},
        {"content": " One moment."},
    ]

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            for index, delta in enumerate(deltas):
                consumed.append(index)
                payload = {"id": "fixture", "choices": [{"index": 0, "delta": delta,
                                                         "finish_reason": None}]}
                yield ("data: " + json.dumps(payload) + "\n\n").encode()
            consumed.append("finished")
            yield b'data: {"id":"fixture","choices":[{"index":0,"delta":{},"finish_reason":"tool_calls"}]}\n\n'
            yield b"data: [DONE]\n\n"

    async def handle(_request):
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Stream())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        client = AsyncOpenAI(api_key="fixture", base_url="https://fixture.invalid/v1",
                             http_client=http, max_retries=0)
        provider = OpenRouterProvider(api_key="fixture", api_base="https://fixture.invalid/v1")
        provider._client_for_key = lambda _: client
        previews, text, final = [], [], None
        async for chunk in provider.chat_stream(messages=[{"role": "user", "content": "Fixture"}]):
            if chunk.finish_reason:
                final = chunk
                continue
            assert not chunk.tool_calls, "Preparation must never enter the executable call list"
            if chunk.content:
                text.append(chunk.content)
            for preview in chunk.tool_call_previews:
                assert "finished" not in consumed, "Preview waited for the end of the request"
                previews.append(preview)
        assert text == ["Checking.", " One moment."]
        assert [(p.index, p.name, p.arguments) for p in previews] == [
            (0, "read_file", '{"path":'),
            (1, "exec", '{"command":"pwd"}'),
            (0, "read_file", '{"path":"report.md"}'),
        ]
        assert [p.id for p in previews] == ["call-first", "call-second", "call-first"]
        assert [c.arguments for c in final.tool_calls] == [{"path": "report.md"}, {"command": "pwd"}]


@pytest.mark.asyncio
async def test_interrupted_stream_never_promotes_partial_arguments_to_a_call():
    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"id":"fixture","choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"id":"unfinished","type":"function","function":{"name":"exec","arguments":"{\\"command\\":"}}]},"finish_reason":null}]}\n\n'
            raise httpx.ReadError("fixture connection lost")

    async def handle(_request):
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Stream())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        client = AsyncOpenAI(api_key="fixture", base_url="https://fixture.invalid/v1",
                             http_client=http, max_retries=0)
        provider = OpenRouterProvider(api_key="fixture", api_base="https://fixture.invalid/v1")
        provider._client_for_key = lambda _: client
        chunks = [chunk async for chunk in provider.chat_stream(messages=[])]
    assert not any(chunk.tool_calls for chunk in chunks)
    assert chunks[-1].finish_reason == "error"


def test_response_previews_are_bounded_deduplicated_and_ignore_reasoning():
    from flowly.providers.tool_preview import ResponseToolPreviews

    previews = ResponseToolPreviews()
    assert previews.consume({"type": "response.output_item.added", "output_index": 0,
                             "item": {"type": "reasoning", "summary": "private"}}) is None
    first = previews.consume({"type": "response.output_item.added", "output_index": 1,
                              "item": {"type": "function_call", "id": "item-a", "call_id": "call-a",
                                       "name": "write_file", "arguments": ""}})
    assert first.id == "call-a" and first.name == "write_file"
    delta = {"type": "response.function_call_arguments.delta", "item_id": "item-a",
             "output_index": 1, "delta": "x" * 20000}
    update = previews.consume(delta)
    assert len(update.arguments) == 16384 and update.arguments_truncated
    assert previews.consume(delta) is None
    assert first.arguments == "", "Snapshots must not mutate after delivery"
    assert previews.consume({"type": "response.output_text.delta", "delta": "text"}) is None


def test_response_preview_accepts_done_only_and_late_identity():
    from flowly.providers.tool_preview import ResponseToolPreviews

    previews = ResponseToolPreviews()
    partial = previews.consume({"type": "response.function_call_arguments.delta", "output_index": 3,
                                "item_id": "item-a", "delta": '{"path":'})
    assert partial is None, "Do not display a nameless call"
    done = previews.consume({"type": "response.output_item.done", "output_index": 3,
                             "item": {"type": "function_call", "id": "item-a", "call_id": "call-a",
                                      "name": "read_file", "arguments": '{"path":"a"}'}})
    assert done.index == 3 and done.id == "call-a" and done.arguments == '{"path":"a"}'


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_kind", ["codex", "xai"])
async def test_native_responses_adapters_emit_preparation_immediately(monkeypatch, provider_kind):
    from flowly.providers.codex_responses_provider import CodexResponsesProvider
    from flowly.providers.xai_responses_provider import XAIResponsesProvider

    observed = []
    item = {"type": "function_call", "id": "item-a", "call_id": "call-a",
            "name": "read_file", "arguments": '{"path":"report.md"}'}
    frames = [
        {"type": "response.output_item.added", "output_index": 0,
         "item": {**item, "arguments": ""}},
        {"type": "response.function_call_arguments.delta", "output_index": 0,
         "item_id": "item-a", "delta": '{"path":"report.md"}'},
        {"type": "response.output_item.done", "output_index": 0, "item": item},
        {"type": "response.completed", "response": {"status": "completed", "output": [item]}},
    ]

    class FakeStream:
        status_code = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

        async def aiter_lines(self):
            for index, frame in enumerate(frames):
                if index == 1:
                    assert observed == [""], "Name was not delivered before the next input delta"
                yield "data: " + json.dumps(frame)

    class FakeClient:
        def __init__(self, **_):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

        def stream(self, *_args, **_kwargs):
            return FakeStream()

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    provider = (CodexResponsesProvider(api_key="fixture", account_id="fixture")
                if provider_kind == "codex" else XAIResponsesProvider(api_key="fixture"))
    final = None
    async for chunk in provider.chat_stream(messages=[{"role": "user", "content": "Fixture"}]):
        for preview in chunk.tool_call_previews:
            assert not chunk.tool_calls and not chunk.finish_reason
            observed.append(preview.arguments)
        if chunk.finish_reason:
            final = chunk
    assert observed == ["", '{"path":"report.md"}']
    assert final.tool_calls[0].arguments == {"path": "report.md"}


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_kind", ["openrouter", "anthropic"])
@pytest.mark.parametrize("ending,args,reason", [
    (None, '{"command":"pwd"}', "error"),
    ("length", '{"command":"pwd"}', "length"),
    ("tool_calls", '{"command":', "error"),
    ("tool_calls", '[]', "error"),
    ("tool_calls", '{"command":"pwd"}', "tool_calls"),
])
async def test_argument_batch_requires_complete_stream_and_valid_object(monkeypatch, provider_kind, ending, args, reason):
    from flowly.providers.anthropic_provider import AnthropicProvider

    if provider_kind == "openrouter":
        frames = [{"choices": [{"index": 0, "delta": {"content": "Visible text."}, "finish_reason": None}]},
                  {"choices": [{"index": 0, "delta": {"tool_calls": [
                      {"index": 0, "id": "call-a", "type": "function", "function": {"name": "exec", "arguments": args}}
                  ]}, "finish_reason": None}]}]
        if ending:
            frames.append({"choices": [{"index": 0, "delta": {}, "finish_reason": ending}]})
    else:
        frames = [{"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Visible text."}},
                  {"type": "content_block_start", "index": 1, "content_block": {"type": "tool_use", "id": "call-a", "name": "exec", "input": {}}},
                  {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": args}}]
        if ending:
            frames.append({"type": "message_delta", "delta": {"stop_reason": "max_tokens" if ending == "length" else "tool_use"}})
    wire = "".join("data: " + json.dumps(frame) + "\n\n" for frame in frames)
    transport = httpx.MockTransport(lambda _: httpx.Response(200, text=wire, headers={"content-type": "text/event-stream"}))
    real_client = httpx.AsyncClient
    if provider_kind == "openrouter":
        async with real_client(transport=transport) as http:
            client = AsyncOpenAI(api_key="fixture", http_client=http, max_retries=0)
            provider = OpenRouterProvider(api_key="fixture")
            provider._client_for_key = lambda _: client
            chunks = [chunk async for chunk in provider.chat_stream(messages=[])]
    else:
        monkeypatch.setattr(httpx, "AsyncClient", lambda **_: real_client(transport=transport))
        provider = AnthropicProvider(api_key="fixture")
        chunks = [chunk async for chunk in provider.chat_stream(messages=[])]
    assert chunks[0].content == "Visible text."
    assert chunks[-1].finish_reason == reason
    assert not any(chunk.tool_calls for chunk in chunks[:-1])
    assert bool(chunks[-1].tool_calls) == (reason == "tool_calls")


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_kind", ["codex", "xai"])
@pytest.mark.parametrize("ending", ["eof", "error", "incomplete", "malformed"])
@pytest.mark.parametrize("text_first", [True, False])
async def test_native_stream_never_restarts_or_executes_incomplete_work(monkeypatch, provider_kind, ending, text_first):
    from unittest.mock import AsyncMock

    from flowly.providers.codex_responses_provider import CodexResponsesProvider
    from flowly.providers.xai_responses_provider import XAIResponsesProvider

    item = {"type": "function_call", "id": "item-a", "call_id": "call-a", "name": "exec", "arguments": '{"command":"pwd"}'}
    if ending == "malformed":
        item["arguments"] = '{"command":'
    frames = [{"type": "response.output_text.delta", "delta": "Visible text."},
              {"type": "response.output_item.added", "output_index": 0, "item": item},
              {"type": "response.output_item.done", "output_index": 0, "item": item}]
    if not text_first:
        frames.pop(0)
    if ending == "error":
        frames.append({"type": "error", "error": {"message": "Fixture lost connection"}})
    elif ending in {"incomplete", "malformed"}:
        frames.append({"type": "response.incomplete" if ending == "incomplete" else "response.completed",
                       "response": {"status": "incomplete" if ending == "incomplete" else "completed", "output": [item]}})
    wire = "".join("data: " + json.dumps(frame) + "\n\n" for frame in frames)
    transport = httpx.MockTransport(lambda _: httpx.Response(200, text=wire, headers={"content-type": "text/event-stream"}))
    real_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **_: real_client(transport=transport))
    provider = CodexResponsesProvider(api_key="fixture", account_id="fixture") if provider_kind == "codex" else XAIResponsesProvider(api_key="fixture")
    provider.chat = AsyncMock(side_effect=AssertionError("Must not restart a visible stream"))
    chunks = [chunk async for chunk in provider.chat_stream(messages=[])]
    provider.chat.assert_not_awaited()
    if text_first:
        assert chunks[0].content == "Visible text."
    assert chunks[-1].finish_reason == ("length" if ending == "incomplete" else "error")
    assert not any(chunk.tool_calls for chunk in chunks)
    assert any(chunk.tool_call_previews for chunk in chunks)
