import json

import pytest

from flowly.providers.xai_responses_provider import (
    XAIResponsesProvider,
    _messages_to_responses_input,
    _responses_tools,
)


def test_messages_convert_chat_tool_loop_to_responses_items():
    instructions, items = _messages_to_responses_input([
        {"role": "system", "content": "be useful"},
        {"role": "user", "content": "search"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": "call_abc",
                "type": "function",
                "function": {"name": "x_search", "arguments": "{\"query\":\"grok\"}"},
            }],
        },
        {"role": "tool", "tool_call_id": "call_abc", "name": "x_search", "content": "done"},
    ])

    assert instructions == "be useful"
    assert items[0] == {"role": "user", "content": "search"}
    assert items[1]["type"] == "function_call"
    assert items[1]["call_id"] == "call_abc"
    assert items[2] == {"type": "function_call_output", "call_id": "call_abc", "output": "done"}


def test_responses_tools_strip_xai_rejected_schema_keywords():
    converted = _responses_tools([{
        "type": "function",
        "function": {
            "name": "tool",
            "description": "desc",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "pattern": "^x$"},
                    "model": {"enum": ["Qwen/Qwen3", "plain"]},
                    "date": {"type": "string", "format": "date"},
                },
            },
        },
    }])

    params = converted[0]["parameters"]
    assert "pattern" not in params["properties"]["path"]
    assert "enum" not in params["properties"]["model"]
    assert "format" not in params["properties"]["date"]


def test_provider_falls_back_when_config_model_is_not_xai():
    provider = XAIResponsesProvider(
        api_key="oauth-token",
        api_base="https://api.x.ai/v1",
        default_model="moonshotai/kimi-k2.5",
    )

    assert provider.get_default_model().startswith("grok")


@pytest.mark.asyncio
async def test_provider_posts_to_responses_and_parses_tool_call(monkeypatch: pytest.MonkeyPatch):
    captured = {}

    class FakeResponse:
        status_code = 200
        text = ""

        def json(self):
            return {
                "status": "completed",
                "output": [{
                    "type": "function_call",
                    "call_id": "call_1",
                    "name": "x_search",
                    "arguments": json.dumps({"query": "grok"}),
                }],
                "usage": {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12},
            }

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, json=None, headers=None):
            captured["url"] = url
            captured["json"] = json
            captured["headers"] = headers
            return FakeResponse()

    import flowly.providers.xai_responses_provider as mod
    monkeypatch.setattr(mod.httpx, "AsyncClient", FakeAsyncClient)

    provider = XAIResponsesProvider(api_key="oauth-token", api_base="https://api.x.ai/v1")
    response = await provider.chat(
        messages=[{"role": "user", "content": "hi"}],
        tools=[{
            "type": "function",
            "function": {
                "name": "x_search",
                "description": "Search X",
                "parameters": {"type": "object", "properties": {}},
            },
        }],
        tool_choice="required",
    )

    assert captured["url"] == "https://api.x.ai/v1/responses"
    assert captured["headers"]["Authorization"] == "Bearer oauth-token"
    assert captured["json"]["tool_choice"] == "required"
    assert response.tool_calls[0].name == "x_search"
    assert response.tool_calls[0].arguments == {"query": "grok"}
    assert response.usage["total_tokens"] == 12
