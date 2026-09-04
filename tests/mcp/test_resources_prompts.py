"""Resource and prompt utilities preserve pages and rich protocol fields."""

from __future__ import annotations

import asyncio
import base64
import json
from types import SimpleNamespace

from mcp import types

import flowly.mcp.tool as tool_module
from flowly.mcp.tool import (
    MCPGetPromptTool,
    MCPListPromptsTool,
    MCPListResourcesTool,
    MCPReadResourceTool,
)


async def _local_mcp_run(**kwargs):
    return await kwargs["coro_factory"](kwargs["server_task"].session)


def _task(session):
    return SimpleNamespace(
        name="catalog",
        session=session,
        rpc_lock=asyncio.Lock(),
        tool_timeout=5.0,
        pagination_max_pages=10,
        pagination_max_items=100,
        max_binary_bytes=1024 * 1024,
    )


def test_list_resources_follows_cursors_and_preserves_metadata(monkeypatch) -> None:
    monkeypatch.setattr(tool_module, "_run_on_mcp_loop", _local_mcp_run)

    class _Session:
        async def list_resources(self, *, cursor=None):
            if cursor is None:
                return types.ListResourcesResult(
                    resources=[types.Resource(
                        name="one",
                        title="First",
                        uri="docs://one",
                        mimeType="text/plain",
                        size=10,
                        icons=[types.Icon(src="https://example.invalid/one.svg")],
                        annotations=types.Annotations(priority=0.9),
                        _meta={"source": "a"},
                    )],
                    nextCursor="page-2",
                )
            assert cursor == "page-2"
            return types.ListResourcesResult(
                resources=[types.Resource(name="two", uri="docs://two")],
            )

    async def _run():
        return await MCPListResourcesTool(server_task=_task(_Session())).execute()

    payload = json.loads(asyncio.run(_run()))
    assert [item["name"] for item in payload["resources"]] == ["one", "two"]
    assert payload["resources"][0]["title"] == "First"
    assert payload["resources"][0]["icons"][0]["src"].endswith("one.svg")
    assert payload["resources"][0]["annotations"]["priority"] == 0.9
    assert payload["resources"][0]["_meta"] == {"source": "a"}
    assert payload["pagination"] == {"pages": 2, "truncated": False}


def test_list_prompts_follows_cursors_and_preserves_metadata(monkeypatch) -> None:
    monkeypatch.setattr(tool_module, "_run_on_mcp_loop", _local_mcp_run)

    class _Session:
        async def list_prompts(self, *, cursor=None):
            if cursor is None:
                return types.ListPromptsResult(
                    prompts=[types.Prompt(
                        name="draft",
                        title="Draft",
                        description="Draft a note",
                        arguments=[types.PromptArgument(
                            name="topic",
                            description="Subject",
                            required=True,
                        )],
                        icons=[types.Icon(src="https://example.invalid/draft.svg")],
                        _meta={"version": 2},
                    )],
                    nextCursor="more",
                )
            return types.ListPromptsResult(
                prompts=[types.Prompt(name="review")],
            )

    async def _run():
        return await MCPListPromptsTool(server_task=_task(_Session())).execute()

    payload = json.loads(asyncio.run(_run()))
    assert [item["name"] for item in payload["prompts"]] == ["draft", "review"]
    assert payload["prompts"][0]["arguments"][0]["required"] is True
    assert payload["prompts"][0]["_meta"] == {"version": 2}
    assert payload["pagination"]["pages"] == 2


def test_read_resource_caches_binary_and_keeps_result_fields(
    tmp_path, monkeypatch,
) -> None:
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path))
    monkeypatch.setattr(tool_module, "_run_on_mcp_loop", _local_mcp_run)

    class _Session:
        async def read_resource(self, uri):
            assert uri == "file://bundle.zip"
            return types.ReadResourceResult(
                contents=[
                    types.TextResourceContents(uri=uri, text="manifest"),
                    types.BlobResourceContents(
                        uri=uri,
                        mimeType="application/zip",
                        blob=base64.b64encode(b"zip-data").decode(),
                    ),
                ],
                ttlMs=5000,
                cacheScope="public",
                _meta={"etag": "v1"},
            )

    async def _run():
        return await MCPReadResourceTool(server_task=_task(_Session())).execute(
            uri="file://bundle.zip"
        )

    payload = json.loads(asyncio.run(_run()))
    assert "manifest" in payload["result"]
    assert payload["contents"][1]["size"] == len(b"zip-data")
    assert payload["ttlMs"] == 5000
    assert payload["cacheScope"] == "public"
    assert payload["_meta"] == {"etag": "v1"}


def test_get_prompt_preserves_audio_and_result_state(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path))
    monkeypatch.setattr(tool_module, "_run_on_mcp_loop", _local_mcp_run)

    class _Session:
        async def get_prompt(self, name, arguments=None):
            assert name == "voice"
            assert arguments == {"tone": "calm"}
            return types.GetPromptResult(
                description="Voice prompt",
                messages=[
                    types.PromptMessage(
                        role="assistant",
                        content=types.AudioContent(
                            data=base64.b64encode(b"audio").decode(),
                            mimeType="audio/mpeg",
                        ),
                    ),
                ],
                resultType="input_required",
                _meta={"step": 1},
            )

    async def _run():
        return await MCPGetPromptTool(server_task=_task(_Session())).execute(
            name="voice",
            arguments={"tone": "calm"},
        )

    payload = json.loads(asyncio.run(_run()))
    message = payload["messages"][0]
    assert message["content"]["type"] == "audio"
    assert message["content"]["media"].startswith("MEDIA:")
    assert payload["resultType"] == "input_required"
    assert payload["_meta"] == {"step": 1}
