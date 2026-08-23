from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from flowly.agent.tools.message_profile import MessageProfileTool
from flowly.gateway.server import (
    _PROFILE_RUN_BINDING,
    GatewayServer,
    _ProfileRunBinding,
)
from flowly.session.manager import SessionManager


@pytest.mark.asyncio
async def test_conversation_model_pin_round_trips_and_can_be_cleared(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path / ".flowly"))
    server = object.__new__(GatewayServer)
    server.sessions = SessionManager(workspace=tmp_path)
    server._ws_rpc_reply = AsyncMock()
    server._ws_rpc_error = AsyncMock()
    ws = object()

    await server._ws_rpc_sessions_model_set(
        ws, "set", {"sessionKey": "desktop:one", "model": "openai/gpt-5.6"}
    )
    assert server.sessions.get_or_create("desktop:one").metadata["model_override"] == "openai/gpt-5.6"
    server._ws_rpc_reply.assert_awaited_with(
        ws,
        "set",
        {"sessionKey": "desktop:one", "model": "openai/gpt-5.6", "inherited": False},
    )

    await server._ws_rpc_sessions_model_get(ws, "get", {"sessionKey": "desktop:one"})
    server._ws_rpc_reply.assert_awaited_with(
        ws,
        "get",
        {"sessionKey": "desktop:one", "model": "openai/gpt-5.6", "inherited": False},
    )

    await server._ws_rpc_sessions_model_set(
        ws, "clear", {"sessionKey": "desktop:one", "model": None}
    )
    assert "model_override" not in server.sessions.get_or_create("desktop:one").metadata


@pytest.mark.asyncio
async def test_profile_reverse_rpc_accepts_only_owning_socket() -> None:
    server = object.__new__(GatewayServer)
    owner = SimpleNamespace(closed=False)
    server._ws_clients = {"desktop-owner": owner}
    server._profile_pending = {}
    server._profile_pending_clients = {}
    server._ws_send = AsyncMock()
    binding = _ProfileRunBinding(
        client_id="desktop-owner",
        session_key="desktop:source",
        current_profile="alpha",
        available_profiles=("alpha", "beta"),
        correlation_id="correlation-1",
        hop=0,
    )
    token = _PROFILE_RUN_BINDING.set(binding)
    try:
        task = asyncio.create_task(
            server.send_profile_message_request("request-1", "beta", "Review this")
        )
        await asyncio.sleep(0)
        server._handle_profile_message_result(
            {"id": "request-1", "result": {"ok": True, "response": "spoofed"}},
            "another-client",
        )
        assert not task.done()
        server._handle_profile_message_result(
            {"id": "request-1", "result": {"ok": True, "response": "verified"}},
            "desktop-owner",
        )
        assert await task == {"ok": True, "response": "verified"}
        server._ws_send.assert_awaited_once()
        sent = server._ws_send.await_args.args[1]
        assert sent["params"] == {
            "sourceProfile": "alpha",
            "sourceSessionKey": "desktop:source",
            "targetProfile": "beta",
            "message": "Review this",
            "correlationId": "correlation-1",
            "hop": 1,
        }
    finally:
        _PROFILE_RUN_BINDING.reset(token)


@pytest.mark.asyncio
async def test_message_profile_tool_returns_structured_broker_result() -> None:
    gateway = SimpleNamespace(
        send_profile_message_request=AsyncMock(
            return_value={"ok": True, "targetProfile": "beta", "response": "Checked"}
        )
    )
    result = await MessageProfileTool(gateway).execute("beta", "Check this")
    assert '"targetProfile":"beta"' in result
    assert '"response":"Checked"' in result
    gateway.send_profile_message_request.assert_awaited_once()
