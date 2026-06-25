import json

import pytest

from flowly.agent.tools import x_search as mod
from flowly.agent.tools.x_search import XSearchTool


def test_sync_xai_search_tool_tracks_credentials(tmp_path, monkeypatch):
    # x_search should appear after `xai login` and vanish after logout
    # without a gateway restart — driven by AgentLoop.sync_xai_search_tool,
    # which the provider hot-reload calls.
    import time

    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path))
    from flowly.auth import xai_oauth
    monkeypatch.setattr(xai_oauth, "_try_keyring", lambda: None)

    from flowly.agent.loop import AgentLoop
    from flowly.agent.tools.registry import ToolRegistry
    from flowly.config.schema import Config

    loop = AgentLoop.__new__(AgentLoop)  # bypass __init__
    loop.tools = ToolRegistry()
    loop._main_config = Config()

    assert loop.sync_xai_search_tool() is False
    assert not loop.tools.has("x_search")

    xai_oauth.save_token_payload(
        xai_oauth.XAITokenPayload(access_token="t", refresh_token="r",
                                  expires_at=int(time.time()) + 3600)
    )
    assert loop.sync_xai_search_tool() is True
    assert loop.tools.has("x_search")

    xai_oauth.clear_token_payload()
    assert loop.sync_xai_search_tool() is False
    assert not loop.tools.has("x_search")


def test_x_search_validates_handles_and_dates():
    assert mod._normalize_handles(["@OpenAI", "openai"]) == ["OpenAI"]

    with pytest.raises(ValueError):
        mod._normalize_handles(["bad/handle"])
    with pytest.raises(ValueError):
        mod._validate_date("2026/01/01", field="from_date")


@pytest.mark.asyncio
async def test_x_search_builds_responses_payload(monkeypatch: pytest.MonkeyPatch):
    captured = {}

    async def fake_resolve(config=None):
        return "token", "https://api.x.ai/v1", "xai_oauth"

    class FakeResponse:
        status_code = 200

        def json(self):
            return {
                "output_text": "answer",
                "citations": [{"url": "https://x.com/a/status/1", "title": "post"}],
            }

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, headers=None, json=None):
            captured["url"] = url
            captured["headers"] = headers
            captured["json"] = json
            return FakeResponse()

    monkeypatch.setattr(mod, "_resolve_credentials", fake_resolve)
    monkeypatch.setattr(mod.httpx, "AsyncClient", FakeAsyncClient)

    result_raw = await XSearchTool().execute(
        query="latest Grok posts",
        allowed_x_handles=["xai"],
        from_date="2026-01-01",
        enable_image_understanding=True,
    )
    result = json.loads(result_raw)

    assert captured["url"] == "https://api.x.ai/v1/responses"
    assert captured["headers"]["Authorization"] == "Bearer token"
    assert captured["json"]["tools"][0]["type"] == "x_search"
    assert captured["json"]["tools"][0]["allowed_x_handles"] == ["xai"]
    assert captured["json"]["tools"][0]["from_date"] == "2026-01-01"
    assert captured["json"]["tools"][0]["enable_image_understanding"] is True
    assert result["answer"] == "answer"
    assert result["citations"][0]["url"] == "https://x.com/a/status/1"
    assert result["source"] == "xai_oauth"


@pytest.mark.asyncio
async def test_x_search_rejects_mutually_exclusive_handle_filters():
    result = await XSearchTool().execute(
        query="test",
        allowed_x_handles=["xai"],
        excluded_x_handles=["openai"],
    )

    assert "mutually exclusive" in result
