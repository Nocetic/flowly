"""GatewayClient methods that drive the TUI policy editor over RPC."""

from __future__ import annotations

from typing import Any

import pytest

from flowly.tui.client import GatewayClient


def _client_capturing(reply: dict[str, Any]):
    client = GatewayClient.__new__(GatewayClient)
    sent: dict[str, Any] = {}

    async def fake_rpc(method: str, params: dict[str, Any]) -> str:
        sent["method"] = method
        sent["params"] = params
        return "rid-1"

    async def fake_await_reply(rid: str, timeout: float = 5.0) -> dict[str, Any]:
        sent["rid"] = rid
        return reply

    client._rpc = fake_rpc  # type: ignore[method-assign]
    client._await_reply = fake_await_reply  # type: ignore[method-assign]
    return client, sent


@pytest.mark.asyncio
async def test_policy_get():
    reply = {"security": "allowlist", "ask": "on-miss", "allowlist": []}
    client, sent = _client_capturing(reply)
    out = await client.exec_policy_get()
    assert sent["method"] == "exec.policy.get"
    assert sent["params"] == {}
    assert out == reply


@pytest.mark.asyncio
async def test_policy_set_only_sends_provided_fields():
    client, sent = _client_capturing({"security": "deny", "ask": "off", "allowlist": []})
    await client.exec_policy_set(security="deny")
    assert sent["method"] == "exec.policy.set"
    assert sent["params"] == {"security": "deny"}  # ask omitted


@pytest.mark.asyncio
async def test_policy_set_sends_both_fields():
    client, sent = _client_capturing({"security": "allowlist", "ask": "always", "allowlist": []})
    await client.exec_policy_set(security="allowlist", ask="always")
    assert sent["params"] == {"security": "allowlist", "ask": "always"}


@pytest.mark.asyncio
async def test_allowlist_remove():
    client, sent = _client_capturing({"security": "full", "ask": "off", "allowlist": [], "removed": True})
    out = await client.exec_policy_allowlist_remove("/usr/bin/git")
    assert sent["method"] == "exec.policy.allowlist.remove"
    assert sent["params"] == {"pattern": "/usr/bin/git"}
    assert out["removed"] is True
