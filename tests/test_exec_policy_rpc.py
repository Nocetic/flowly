"""Gateway RPCs that let a client (TUI policy editor) read and change the
standing exec approval policy, which lives in the approvals store. Writes go
to the store file; the running executor picks them up via refresh_if_changed.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from flowly.exec.approvals import ExecApprovalStore
from flowly.gateway.server import GatewayServer


def _server() -> tuple[GatewayServer, list[dict], list[dict]]:
    server = GatewayServer(
        host="127.0.0.1",
        port=0,
        on_voice_message=AsyncMock(),
        on_cron_run=AsyncMock(),
        on_cron_health=AsyncMock(),
        on_cron_reload=AsyncMock(),
        on_chat_message=AsyncMock(),
        sessions=MagicMock(),
        subagent_registry=None,
        artifact_store=None,
        on_compact=AsyncMock(),
        on_clear=AsyncMock(),
    )
    replies: list[dict] = []
    errors: list[dict] = []

    async def fake_reply(ws, rpc_id, result):
        replies.append(result)

    async def fake_error(ws, rpc_id, code, message):
        errors.append({"code": code, "message": message})

    server._ws_rpc_reply = fake_reply  # type: ignore[method-assign]
    server._ws_rpc_error = fake_error  # type: ignore[method-assign]
    return server, replies, errors


@pytest.fixture(autouse=True)
def _home(monkeypatch, tmp_path):
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path / "home"))


@pytest.mark.asyncio
async def test_policy_get_returns_defaults():
    server, replies, _ = _server()
    await server._ws_rpc_exec_policy_get(None, "1", {})
    assert replies[0]["security"] == "full"
    assert replies[0]["ask"] == "off"
    assert replies[0]["allowlist"] == []


@pytest.mark.asyncio
async def test_policy_set_persists_and_returns_updated():
    server, replies, _ = _server()
    await server._ws_rpc_exec_policy_set(None, "1", {"security": "allowlist", "ask": "always"})
    assert replies[0]["security"] == "allowlist"
    assert replies[0]["ask"] == "always"
    # Persisted to the store file (what the executor reads).
    assert ExecApprovalStore().load().security == "allowlist"


@pytest.mark.asyncio
async def test_policy_set_rejects_invalid_security():
    server, replies, errors = _server()
    await server._ws_rpc_exec_policy_set(None, "1", {"security": "bogus"})
    assert not replies
    assert errors[0]["code"] == "INVALID_REQUEST"


@pytest.mark.asyncio
async def test_policy_set_rejects_empty():
    server, replies, errors = _server()
    await server._ws_rpc_exec_policy_set(None, "1", {})
    assert not replies
    assert errors[0]["code"] == "INVALID_REQUEST"


@pytest.mark.asyncio
async def test_allowlist_remove():
    seed = ExecApprovalStore()
    seed.load()
    seed.add_to_allowlist(pattern="/usr/bin/git", command="git *")
    seed.save()

    server, replies, _ = _server()
    await server._ws_rpc_exec_policy_allowlist_remove(None, "1", {"pattern": "/usr/bin/git"})
    assert replies[0]["removed"] is True
    assert replies[0]["allowlist"] == []
    assert ExecApprovalStore().load().allowlist == []


@pytest.mark.asyncio
async def test_allowlist_remove_missing_pattern_is_false():
    server, replies, _ = _server()
    await server._ws_rpc_exec_policy_allowlist_remove(None, "1", {"pattern": "/nope"})
    assert replies[0]["removed"] is False
