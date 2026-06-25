"""Tests for the write-plane: gateway control endpoint + serve client (Faz 3c).

Spins up a real aiohttp app with the control routes (a stand-in for the
gateway), advertises it via gateway-api.json, and drives the serve-side
writeplane HTTP client against it — send, approvals list/resolve, auth,
and graceful degradation when the gateway is absent.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from flowly.mcp.server import control, writeplane


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path))
    return tmp_path


class _FakeApprovalManager:
    def __init__(self):
        self._pending = {
            "abc123": SimpleNamespace(
                id="abc123",
                request=SimpleNamespace(command="rm -rf /tmp/x"),
                session_key="telegram:1",
                created_at=1.0,
                expires_at=9999999999.0,
                risk_reasons=["destructive"],
            )
        }
        self.resolved: list[tuple[str, str]] = []

    def list_pending(self):
        return list(self._pending.values())

    def resolve(self, approval_id, decision):
        if approval_id in self._pending:
            self.resolved.append((approval_id, decision))
            del self._pending[approval_id]
            return True
        return False


async def _start_control_app(token, on_send, monkeypatch):
    """Start a real aiohttp app with control routes on an ephemeral port."""
    from aiohttp import web

    fake_mgr = _FakeApprovalManager()
    monkeypatch.setattr(
        "flowly.exec.approval_manager.get_approval_manager", lambda: fake_mgr,
    )

    app = web.Application()
    control.register_control_routes(app, token=token, on_send=on_send)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    # Discover the bound port.
    port = list(runner.addresses)[0][1] if hasattr(runner, "addresses") else None
    if port is None:
        for sock in runner.server.sockets:  # type: ignore[attr-defined]
            port = sock.getsockname()[1]
            break
    return runner, port, fake_mgr


@pytest.mark.asyncio
async def test_send_message_roundtrip(isolated_home, monkeypatch):
    sent: list[tuple[str, str]] = []

    async def _on_send(target, message):
        sent.append((target, message))
        return True

    token = control.generate_token()
    runner, port, _ = await _start_control_app(token, _on_send, monkeypatch)
    try:
        control.write_api_file("127.0.0.1", port, token)
        # writeplane uses blocking urllib → run in a thread.
        res = await asyncio.to_thread(
            writeplane._request, "POST", "/messages/send",
            {"target": "telegram:123", "message": "hi"},
        )
        assert res.get("sent") is True
        assert sent == [("telegram:123", "hi")]
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_approvals_list_and_resolve(isolated_home, monkeypatch):
    async def _on_send(target, message):
        return True

    token = control.generate_token()
    runner, port, mgr = await _start_control_app(token, _on_send, monkeypatch)
    try:
        control.write_api_file("127.0.0.1", port, token)

        listed = await asyncio.to_thread(writeplane._request, "GET", "/approvals")
        assert listed["count"] == 1
        assert listed["approvals"][0]["id"] == "abc123"
        assert listed["approvals"][0]["command"] == "rm -rf /tmp/x"

        resolved = await asyncio.to_thread(
            writeplane._request, "POST", "/approvals/resolve",
            {"id": "abc123", "decision": "deny"},
        )
        assert resolved["resolved"] is True
        assert mgr.resolved == [("abc123", "deny")]
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_bad_token_rejected(isolated_home, monkeypatch):
    async def _on_send(target, message):
        return True

    token = control.generate_token()
    runner, port, _ = await _start_control_app(token, _on_send, monkeypatch)
    try:
        # Advertise a WRONG token.
        control.write_api_file("127.0.0.1", port, "wrong-token")
        res = await asyncio.to_thread(writeplane._request, "GET", "/approvals")
        assert "error" in res  # 401 → error envelope
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_invalid_decision_rejected(isolated_home, monkeypatch):
    async def _on_send(target, message):
        return True

    token = control.generate_token()
    runner, port, _ = await _start_control_app(token, _on_send, monkeypatch)
    try:
        control.write_api_file("127.0.0.1", port, token)
        res = await asyncio.to_thread(
            writeplane._request, "POST", "/approvals/resolve",
            {"id": "abc123", "decision": "maybe"},
        )
        assert "error" in res
    finally:
        await runner.cleanup()


def test_graceful_degradation_no_gateway(isolated_home):
    # No gateway-api.json → clear "not running" error, no exception.
    control.remove_api_file()
    res = writeplane._request("GET", "/approvals")
    assert "error" in res
    assert "gateway" in res["error"].lower()


def test_api_file_roundtrip_and_perms(isolated_home):
    import stat
    control.write_api_file("127.0.0.1", 18790, "tok123")
    info = control.read_api_file()
    assert info["port"] == 18790
    assert info["token"] == "tok123"
    path = isolated_home / "gateway-api.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    control.remove_api_file()
    assert control.read_api_file() is None


def test_register_write_tools_adds_three():
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError:
        pytest.skip("mcp SDK not installed")
    import json
    mcp = FastMCP("t")
    writeplane.register_write_tools(mcp, lambda o: json.dumps(o))
    # FastMCP stores tools; confirm our three are present.
    names = set()
    mgr = getattr(mcp, "_tool_manager", None)
    if mgr and hasattr(mgr, "list_tools"):
        names = {t.name for t in mgr.list_tools()}
    assert {"messages_send", "approvals_list", "approvals_resolve"} <= names
