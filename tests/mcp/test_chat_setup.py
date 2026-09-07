import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from flowly.agent.tool_context import tool_execution_scope
from flowly.agent.tools.mcp_connection import MCPConnectionRequestTool
from flowly.channels import feature_rpc
from flowly.mcp.connections import MCPConnectionService
from flowly.mcp.setup import MCPSetupError


@pytest.fixture
async def service(tmp_path, monkeypatch):
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path))
    instance = MCPConnectionService(
        tmp_path / "config.json", lambda: object(),
        probe=AsyncMock(return_value=(True, ["read", "write"], "")),
        apply=AsyncMock(return_value={"ok": True, "connected": True}),
    )
    monkeypatch.setattr(feature_rpc, "_mcp_connection_service", instance)
    yield instance
    await instance.close()


async def request(service):
    task = asyncio.create_task(service.chat.request({
        "name": "demo", "reason": "Read the requested project notes", "config": {"command": "temporary-peer"},
    }, "web:owner"))
    await asyncio.sleep(0)
    return task, service.chat.pending()["requests"][0]


async def test_proposal_is_inert_and_cancel_returns_to_owning_conversation(service):
    task, req = await request(service)
    assert req["sessionKey"] == "web:owner"
    assert service.chat.pending("web:other")["requests"] == []
    service.manager.probe.assert_not_awaited()
    assert not service.manager.store.path.exists()
    assert not service.manager.operations
    await service.invoke("chat.cancel", {"id": req["id"]})
    assert (await task)["status"] == "cancelled"
    with pytest.raises(MCPSetupError, match="ended"):
        service.chat.begin({"chatRequestId": req["id"], "requestId": "request-123456789"})


async def test_owner_start_and_permission_confirmation_resume_waiting_tool(service):
    task, req = await request(service)
    started = await service.invoke("begin", {
        "chatRequestId": req["id"], "requestId": "request-123456789", "name": "spoofed", "sessionKey": "web:other",
        "config": {"command": "must-not-run"},
    })
    await asyncio.sleep(0)
    op = service.manager.get(started["id"])
    assert op.name == "demo" and op.session_key == "web:owner"
    assert service.manager.probe.call_args.args[1]["command"] == "temporary-peer"
    assert not service.manager.store.path.exists() and not task.done()
    service.manager.confirm(op.id, {"mode": "selected", "include": ["read"]})
    result = await asyncio.wait_for(task, 2)
    assert result["status"] == "complete" and result["saved"] and result["connected"]
    assert "temporary-peer" not in json.dumps(result)
    assert json.loads(service.manager.store.path.read_text())["mcpServers"]["demo"]["tools"]["include"] == ["read"]


async def test_lost_begin_ack_reuses_operation_and_other_desktop_cannot_take_receiver(service):
    task, req = await request(service)
    params = {"chatRequestId": req["id"], "requestId": "request-123456789"}
    first = service.chat.begin(params)
    assert service.chat.begin(params)["id"] == first["id"]
    with pytest.raises(MCPSetupError, match="another Desktop"):
        service.chat.begin({**params, "requestId": "different-request-1234"})
    await service.chat.cancel(req["id"])
    assert (await task)["status"] == "cancelled"


async def test_stopped_tool_cancels_unsigned_setup(service):
    task, req = await request(service)
    service.chat.begin({"chatRequestId": req["id"], "requestId": "request-123456789"})
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not service.manager.store.path.exists()
    assert service.chat.pending()["requests"] == []


async def test_config_change_requires_new_human_review(service):
    task, req = await request(service)
    service.manager.store.path.write_text('{"mcpServers":{"demo":{"command":"changed"}}}')
    with pytest.raises(MCPSetupError, match="changed"):
        service.chat.begin({"chatRequestId": req["id"], "requestId": "request-123456789"})
    service.manager.probe.assert_not_awaited()
    await service.chat.cancel(req["id"])
    await task


async def test_gateway_shutdown_cancels_proposal_and_resumes_waiter(service):
    task, _ = await request(service)
    await service.close()
    assert (await task)["status"] == "cancelled"


async def test_model_tool_uses_trusted_origin_not_model_session_argument(service):
    tool = MCPConnectionRequestTool()
    no_owner = json.loads(await tool.execute(action="request", name="demo", reason="Read notes", config={"command": "demo"}))
    assert "error" in no_owner
    with tool_execution_scope("web:owner"):
        task = asyncio.create_task(tool.execute(action="request", name="demo", reason="Read notes",
                                               config={"command": "demo"}, session_key="web:spoof"))
    await asyncio.sleep(0)
    req = service.chat.pending()["requests"][0]
    assert req["sessionKey"] == "web:owner"
    await service.chat.cancel(req["id"])
    assert json.loads(await task)["status"] == "cancelled"


async def test_chat_cannot_supply_credentials_or_permission_writes(service):
    for config in ({"command": "demo", "env": {"API_KEY": "secret"}}, {"command": "demo", "tools": {"mode": "all"}}):
        with pytest.raises(MCPSetupError, match="privately"):
            await service.chat.request({"name": "demo", "reason": "Read notes", "config": config}, "web:owner")
    assert not service.manager.store.path.exists()


async def test_timeout_resumes_without_running_or_saving(service, monkeypatch):
    monkeypatch.setattr("flowly.mcp.chat_setup.CHAT_SETUP_TIMEOUT", 0.01)
    task, _ = await request(service)
    assert (await asyncio.wait_for(task, 1))["status"] == "expired"
    service.manager.probe.assert_not_awaited()
    assert not service.manager.store.path.exists()


async def test_stopping_chat_after_human_commit_does_not_cancel_accepted_save(service):
    entered, release = asyncio.Event(), asyncio.Event()
    async def apply(_name):
        entered.set()
        await release.wait()
        return {"ok": True, "connected": True}
    service.manager.apply = apply
    task, req = await request(service)
    started = service.chat.begin({"chatRequestId": req["id"], "requestId": "request-123456789"})
    await asyncio.sleep(0)
    service.manager.confirm(started["id"], {"mode": "none"})
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    closing = asyncio.create_task(service.close())
    await asyncio.sleep(0)
    assert not closing.done()
    release.set()
    await asyncio.wait_for(closing, 1)
    assert service.manager.get(started["id"]).phase == "complete"
    assert json.loads(service.manager.store.path.read_text())["mcpServers"]["demo"]["tools"]["mode"] == "none"


def test_setup_tool_routes_as_human_interaction_not_remote_mcp():
    from flowly.agent.tools.registry import ToolRegistry
    from flowly.agent.tools.routing import infer_toolset, resolve_toolset_filters
    registry = ToolRegistry()
    registry.register(MCPConnectionRequestTool())
    assert infer_toolset("mcp_connection") == "interactive"
    assert registry.get_toolsets()["mcp_connection"] == "interactive"
    enabled, disabled = resolve_toolset_filters("cron")
    assert registry.get_definitions(platform="cron", enabled_toolsets=enabled, disabled_toolsets=disabled) == []
