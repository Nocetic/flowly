import json

import pytest

from flowly.integrations.active_provider import ActiveProvider
from flowly.providers.anthropic_provider import AnthropicProvider
from flowly.providers.factory import build_provider


def test_factory_uses_native_anthropic_provider():
    provider = build_provider(
        ActiveProvider(
            key="anthropic",
            api_key="sk-ant-api03-test",
            api_base="https://api.anthropic.com/v1",
            source="BYOK · anthropic",
        ),
        default_model="claude-haiku-4-5",
    )

    assert isinstance(provider, AnthropicProvider)


@pytest.mark.asyncio
async def test_chat_posts_to_native_messages_api_and_parses_tool_use(monkeypatch):
    captured = {}

    class FakeResponse:
        status_code = 200
        text = ""

        def json(self):
            return {
                "content": [
                    {"type": "text", "text": "I'll check."},
                    {
                        "type": "tool_use",
                        "id": "toolu_1",
                        "name": "get_weather",
                        "input": {"city": "Istanbul"},
                    },
                ],
                "stop_reason": "tool_use",
                "usage": {
                    "input_tokens": 10,
                    "output_tokens": 4,
                    "cache_read_input_tokens": 3,
                    "cache_creation_input_tokens": 2,
                },
            }

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            captured["timeout"] = kwargs.get("timeout")

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, json=None, headers=None):
            captured["url"] = url
            captured["json"] = json
            captured["headers"] = headers
            return FakeResponse()

    import flowly.providers.anthropic_provider as mod

    monkeypatch.setattr(mod.httpx, "AsyncClient", FakeAsyncClient)

    provider = AnthropicProvider(
        api_key="sk-ant-api03-secret",
        api_base="https://api.anthropic.com/v1",
    )
    response = await provider.chat(
        messages=[
            {"role": "system", "content": "Be concise."},
            {"role": "user", "content": "Weather?"},
        ],
        tools=[{
            "type": "function",
            "function": {
                "name": "get_weather",
                "description": "Get weather.",
                "parameters": {
                    "type": "object",
                    "properties": {"city": {"type": "string"}},
                },
            },
        }],
        model="anthropic/claude-haiku-4.5",
        max_tokens=123,
        temperature=0.2,
    )

    assert captured["url"] == "https://api.anthropic.com/v1/messages"
    assert captured["headers"]["x-api-key"] == "sk-ant-api03-secret"
    assert "Authorization" not in captured["headers"]
    assert captured["headers"]["anthropic-version"] == "2023-06-01"
    assert captured["json"]["model"] == "claude-haiku-4-5"
    assert captured["json"]["max_tokens"] == 123
    assert captured["json"]["temperature"] == 0.2
    assert captured["json"]["system"][0]["type"] == "text"
    assert captured["json"]["messages"] == [
        {
            "role": "user",
            "content": [{
                "type": "text",
                "text": "Weather?",
                "cache_control": {"type": "ephemeral", "ttl": "1h"},
            }],
        }
    ]
    assert captured["json"]["tools"] == [{
        "name": "get_weather",
        "description": "Get weather.",
        "input_schema": {
            "type": "object",
            "properties": {"city": {"type": "string"}},
        },
    }]
    assert captured["json"]["tool_choice"] == {"type": "auto"}

    assert response.content == "I'll check."
    assert response.finish_reason == "tool_calls"
    assert response.tool_calls[0].id == "toolu_1"
    assert response.tool_calls[0].name == "get_weather"
    assert response.tool_calls[0].arguments == {"city": "Istanbul"}
    assert response.usage == {
        "prompt_tokens": 10,
        "completion_tokens": 4,
        "total_tokens": 14,
        "cache_read_tokens": 3,
        "cache_write_tokens": 2,
    }


@pytest.mark.asyncio
async def test_tool_loop_messages_convert_to_tool_use_and_tool_result(monkeypatch):
    captured = {}

    class FakeResponse:
        status_code = 200
        text = ""

        def json(self):
            return {"content": [{"type": "text", "text": "done"}], "stop_reason": "end_turn"}

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, json=None, headers=None):
            captured["json"] = json
            return FakeResponse()

    import flowly.providers.anthropic_provider as mod

    monkeypatch.setattr(mod.httpx, "AsyncClient", FakeAsyncClient)

    provider = AnthropicProvider(api_key="sk-ant-api03-secret")
    await provider.chat(
        messages=[
            {"role": "user", "content": "Weather?"},
            {
                "role": "assistant",
                "content": "Checking.",
                "tool_calls": [{
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": "get_weather",
                        "arguments": json.dumps({"city": "Istanbul"}),
                    },
                }],
            },
            {
                "role": "tool",
                "tool_call_id": "call_1",
                "name": "get_weather",
                "content": "Sunny",
            },
        ],
        model="claude-haiku-4-5",
    )

    assistant = captured["json"]["messages"][1]
    assert assistant["role"] == "assistant"
    assert assistant["content"][0]["type"] == "text"
    assert assistant["content"][0]["text"] == "Checking."
    assert assistant["content"][1] == {
        "type": "tool_use",
        "id": "call_1",
        "name": "get_weather",
        "input": {"city": "Istanbul"},
    }

    tool_result = captured["json"]["messages"][2]
    assert tool_result == {
        "role": "user",
        "content": [{
            "type": "tool_result",
            "tool_use_id": "call_1",
            "content": "Sunny",
        }],
    }


@pytest.mark.asyncio
async def test_chat_stream_accumulates_text_usage_and_tool_calls(monkeypatch):
    class FakeStreamResponse:
        status_code = 200
        text = ""

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def aread(self):
            return b""

        async def aiter_lines(self):
            events = [
                {
                    "type": "message_start",
                    "message": {"usage": {"input_tokens": 7}},
                },
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": "Hi"},
                },
                {
                    "type": "content_block_start",
                    "index": 1,
                    "content_block": {
                        "type": "tool_use",
                        "id": "toolu_1",
                        "name": "get_weather",
                        "input": {},
                    },
                },
                {
                    "type": "content_block_delta",
                    "index": 1,
                    "delta": {"type": "input_json_delta", "partial_json": '{"city":'},
                },
                {
                    "type": "content_block_delta",
                    "index": 1,
                    "delta": {"type": "input_json_delta", "partial_json": '"Istanbul"}'},
                },
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "tool_use"},
                    "usage": {"output_tokens": 3},
                },
                {"type": "message_stop"},
            ]
            for event in events:
                yield f"data: {json.dumps(event)}"

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        def stream(self, *args, **kwargs):
            return FakeStreamResponse()

    import flowly.providers.anthropic_provider as mod

    monkeypatch.setattr(mod.httpx, "AsyncClient", FakeAsyncClient)

    provider = AnthropicProvider(api_key="sk-ant-api03-secret")
    chunks = [
        chunk
        async for chunk in provider.chat_stream(
            messages=[{"role": "user", "content": "hi"}],
            tools=[{
                "type": "function",
                "function": {"name": "get_weather", "parameters": {"type": "object"}},
            }],
            model="claude-haiku-4-5",
        )
    ]

    assert chunks[0].content == "Hi"
    final = chunks[-1]
    assert final.finish_reason == "tool_calls"
    assert final.usage == {
        "prompt_tokens": 7,
        "completion_tokens": 3,
        "total_tokens": 10,
    }
    assert final.tool_calls[0].id == "toolu_1"
    assert final.tool_calls[0].name == "get_weather"
    assert final.tool_calls[0].arguments == {"city": "Istanbul"}
