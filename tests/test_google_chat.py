import asyncio
import json
from unittest.mock import Mock

import pytest

from flowly.agent.tool_context import tool_execution_scope
from flowly.agent.tools.google_connection import GoogleConnectionTool
from flowly.integrations.gmail_connection import GmailConnection, GmailConnectionError
from flowly.integrations.google_chat import GoogleChatRequests


@pytest.fixture
def manager(monkeypatch, tmp_path):
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path))
    return GoogleChatRequests()


async def propose(manager, session="web:one"):
    task = asyncio.create_task(manager.request(session, "Organize my mail", ["gmail", "gmail_manage"]))
    await asyncio.sleep(0)
    return task, manager.pending(session)["requests"][0]


async def test_proposals_are_inert_private_to_conversation_and_decline_unblocks_model(manager, monkeypatch):
    begin = Mock()
    monkeypatch.setattr(GmailConnection, "begin", begin)
    task, request = await propose(manager)
    assert manager.pending("web:other") == {"requests": []}
    begin.assert_not_called()
    with pytest.raises(GmailConnectionError, match="SETUP_NOT_FOUND"):
        await manager.cancel(request["id"], "web:other")
    await manager.cancel(request["id"], "web:one")
    assert (await task)["status"] == "cancelled"
    assert manager.pending("web:one") == {"requests": []}


async def test_human_start_uses_reviewed_services_and_reports_actual_granted_access(manager, monkeypatch):
    begin = Mock(return_value={"requestId": "a" * 32})
    monkeypatch.setattr(GmailConnection, "begin", begin)
    monkeypatch.setattr(GmailConnection, "setup_status", lambda *_: {"status": "connected", "connected": True, "services": ["gmail"], "access_token": "never-return"})
    task, request = await propose(manager)
    await manager.begin(request["id"], "web:one", services=["gmail"])
    result = await asyncio.wait_for(task, 1)
    begin.assert_called_once_with(services=["gmail"])
    assert result["connected"] is True and result["services"] == ["gmail"]
    assert "never-return" not in json.dumps(result)


async def test_stopping_turn_removes_card_without_revoking_owner_started_oauth(manager, monkeypatch):
    cancel = Mock()
    monkeypatch.setattr(GmailConnection, "cancel", cancel)
    task, request = await propose(manager)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not manager.pending("web:one")["requests"]
    cancel.assert_not_called()


async def test_expiry_and_duplicate_requests_do_not_start_oauth(manager, monkeypatch):
    task, request = await propose(manager)
    with pytest.raises(GmailConnectionError, match="SETUP_IN_PROGRESS"):
        await manager.request("web:one", "Another request", ["gmail"])
    manager.requests[request["id"]].created_at -= 1000
    with pytest.raises(GmailConnectionError, match="EXPIRED"):
        await manager.begin(request["id"], "web:one")
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def test_tool_uses_runtime_session_instead_of_model_supplied_session(manager, monkeypatch):
    monkeypatch.setattr("flowly.channels.feature_rpc._mcp_connection_service", object())
    monkeypatch.setattr("flowly.agent.tools.google_connection.google_chat_requests", lambda: manager)
    monkeypatch.setattr(GmailConnection, "status", lambda *_: {"connected": False})
    with tool_execution_scope("web:real"):
        task = asyncio.create_task(GoogleConnectionTool().execute("request", reason="Read mail", services=["gmail"], session_key="web:forged"))
    for _ in range(50):
        if manager.pending("web:real")["requests"]:
            break
        await asyncio.sleep(.01)
    assert not manager.pending("web:forged")["requests"]
    request = manager.pending("web:real")["requests"][0]
    await manager.cancel(request["id"], "web:real")
    assert json.loads(await task)["status"] == "cancelled"


async def test_tool_rejects_unowned_conversations(manager):
    result = json.loads(await GoogleConnectionTool().execute("request", reason="Read mail"))
    assert "live conversation" in result["error"]
    assert not manager.requests
