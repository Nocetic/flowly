from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from flowly.agent.tools.shared_service import (
    SharedArtifactTool,
    SharedBoardListTool,
)
from flowly.artifacts.store import ArtifactStore
from flowly.artifacts.context import internal_context_metadata
from flowly.board.store import BoardStore
from flowly.shared_service import (
    SharedServiceError,
    invoke_shared_service,
    validate_shared_service_request,
)


def request(service: str, tool: str, arguments: dict) -> dict:
    return {
        "service": service,
        "tool": tool,
        "arguments": arguments,
        "sourceProfile": "research",
        "sourceSessionKey": "desktop:profile:research:one",
        "turnOrigin": "user",
        "correlationId": "call-1",
    }


def test_shared_service_rejects_arbitrary_rpc_and_spoofed_sources() -> None:
    with pytest.raises(SharedServiceError) as unknown:
        validate_shared_service_request(request("gateway", "exec", {}))
    assert unknown.value.code == "SHARED_SERVICE_UNKNOWN"

    bad = request("board", "board_list", {})
    bad["sourceProfile"] = "../default"
    with pytest.raises(SharedServiceError) as source:
        validate_shared_service_request(bad)
    assert source.value.code == "SHARED_SOURCE_INVALID"


@pytest.mark.asyncio
async def test_shared_artifact_is_primary_owned_and_carries_provenance(tmp_path) -> None:
    artifact_store = ArtifactStore(tmp_path / "artifacts.sqlite")
    on_change = AsyncMock()
    result = await invoke_shared_service(
        request("artifacts", "artifact", {
            "action": "create",
            "type": "markdown",
            "title": "Research brief",
            "content": "Findings",
        }),
        board_store=None,
        board_orchestrator=None,
        artifact_store=artifact_store,
        artifact_on_change=on_change,
    )

    output = json.loads(result["output"])
    artifact = artifact_store.get(output["artifact"]["id"])
    assert artifact is not None
    assert artifact["session_key"] == "desktop:profile:research:one"
    assert artifact["metadata"]["flowlyProvenance"] == {
        "createdByProfile": "research",
        "sourceProfile": "research",
        "sourceSessionKey": "desktop:profile:research:one",
        "turnOrigin": "user",
        "correlationId": "call-1",
    }
    on_change.assert_awaited_once()


@pytest.mark.asyncio
async def test_shared_board_uses_profile_actor_and_single_store(tmp_path) -> None:
    board_store = BoardStore(tmp_path / "board.db")
    created = await invoke_shared_service(
        request("board", "board_add", {"title": "Review release"}),
        board_store=board_store,
        board_orchestrator=None,
        artifact_store=None,
    )
    card = json.loads(created["output"])["card"]
    assert card["created_by"] == "profile:research"

    listed = await invoke_shared_service(
        request("board", "board_list", {}),
        board_store=board_store,
        board_orchestrator=None,
        artifact_store=None,
    )
    assert json.loads(listed["output"])["cards"][0]["id"] == card["id"]

    replay = await invoke_shared_service(
        request("board", "board_add", {"title": "Review release"}),
        board_store=board_store,
        board_orchestrator=None,
        artifact_store=None,
    )
    assert json.loads(replay["output"])["card"]["id"] == card["id"]
    assert len(board_store.list_cards()) == 1


@pytest.mark.asyncio
async def test_named_tool_adapters_preserve_normal_tool_names_and_outputs() -> None:
    gateway = AsyncMock()
    gateway.send_shared_service_request.return_value = {
        "ok": True,
        "output": '{"ok":true}',
    }

    board = SharedBoardListTool(gateway)
    artifact = SharedArtifactTool(gateway, local_store=None)
    assert board.name == "board_list"
    assert artifact.name == "artifact"
    assert await board.execute(status="todo") == '{"ok":true}'
    assert await artifact.execute(action="list") == '{"ok":true}'
    assert gateway.send_shared_service_request.await_count == 2


@pytest.mark.asyncio
async def test_named_artifact_adapter_keeps_internal_spills_private_but_promotable(
    tmp_path,
) -> None:
    local = ArtifactStore(tmp_path / "profile-artifacts.sqlite")
    private = local.create(
        type="markdown",
        title="Tool context",
        content="private context",
        metadata=internal_context_metadata(source="web_fetch", original_chars=15),
        tags=["internal:context"],
    )
    gateway = AsyncMock()
    gateway.send_shared_service_request.return_value = {
        "ok": True,
        "output": '{"action":"create","artifact":{"id":"shared-1"}}',
    }
    tool = SharedArtifactTool(gateway, local)

    local_result = json.loads(await tool.execute(
        action="get", artifact_id=private["id"], limit=100,
    ))
    assert local_result["artifact"]["content"] == "private context"
    gateway.send_shared_service_request.assert_not_awaited()

    await tool.execute(action="promote", artifact_id=private["id"])
    arguments = gateway.send_shared_service_request.await_args.kwargs["arguments"]
    assert arguments["action"] == "create"
    assert arguments["content"] == "private context"
    assert "internal:context" not in arguments["tags"]
    assert local.get(private["id"]) is not None
