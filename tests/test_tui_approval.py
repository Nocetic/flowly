from __future__ import annotations

import asyncio
from typing import Any

import pytest

from flowly.tui.client import ApprovalRequest, GatewayClient


@pytest.mark.asyncio
async def test_gateway_client_accepts_approval_event_id_aliases() -> None:
    client = GatewayClient.__new__(GatewayClient)
    client._inbox = asyncio.Queue()

    payload = {
        "type": "event",
        "event": "exec.approval.requested",
        "data": {
            "id": "approval-1",
            "command": "echo hello",
            "riskReasons": ["writes outside workspace"],
            "sessionKey": "tui:default",
            "expiresAt": 123.0,
            "cwd": "/work",
            "resolvedPath": "/work/file.txt",
        },
    }

    await client._dispatch(payload)

    ev = await client._inbox.get()
    assert isinstance(ev, ApprovalRequest)
    assert ev.request_id == "approval-1"
    assert ev.command == "echo hello"
    assert ev.reasons == ["writes outside workspace"]
    assert ev.session_key == "tui:default"
    assert ev.expires_at == 123.0
    assert ev.cwd == "/work"
    assert ev.resolved_path == "/work/file.txt"


@pytest.mark.asyncio
async def test_gateway_client_sends_both_approval_id_fields() -> None:
    client = GatewayClient.__new__(GatewayClient)
    captured: dict[str, Any] = {}

    async def fake_rpc(method: str, params: dict[str, Any]) -> str:
        captured["method"] = method
        captured["params"] = params
        return "rpc-1"

    client._rpc = fake_rpc

    await client.approval_resolve("approval-1", "allow-always", remember=True)

    assert captured == {
        "method": "exec.approval.resolve",
        "params": {
            "id": "approval-1",
            "requestId": "approval-1",
            "decision": "allow-always",
            "remember": True,
        },
    }
