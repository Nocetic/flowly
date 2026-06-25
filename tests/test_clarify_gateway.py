"""Gateway side of clarify: broadcast_clarify_request fans the event out to
connected clients, and agent.clarify.resolve routes the answer back to the
manager's pending Future."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from flowly.clarify.manager import ClarifyManager, get_clarify_manager
from flowly.clarify.types import ClarifyRequest
from flowly.gateway.server import GatewayServer


@pytest.fixture(autouse=True)
def _isolate_home(monkeypatch, tmp_path):
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path / "home"))


@pytest.fixture(autouse=True)
def _fresh_manager(monkeypatch):
    """Each test gets its own manager singleton."""
    import flowly.clarify.manager as mod
    monkeypatch.setattr(mod, "_manager", ClarifyManager())


def _server() -> GatewayServer:
    return GatewayServer(host="127.0.0.1", port=0, on_chat_message=AsyncMock())


def _fake_ws():
    ws = MagicMock()
    ws.closed = False
    ws.send_json = AsyncMock()
    return ws


@pytest.mark.asyncio
async def test_broadcast_reaches_clients():
    server = _server()
    ws = _fake_ws()
    server._ws_clients["c1"] = ws

    await server.broadcast_clarify_request(
        "id1", "Which?", ["A", "B"], "web:1", time.time() + 60,
    )

    ws.send_json.assert_awaited_once()
    sent = ws.send_json.await_args.args[0]
    assert sent["event"] == "agent.clarify.requested"
    assert sent["data"]["id"] == "id1"
    assert sent["data"]["choices"] == ["A", "B"]
    assert sent["data"]["sessionKey"] == "web:1"


@pytest.mark.asyncio
async def test_resolve_completes_pending_future():
    server = _server()
    mgr = get_clarify_manager()

    now = time.time()
    pending = ClarifyRequest(
        id="id2", question="Which?", choices=["A", "B"],
        session_key="web:1", created_at=now, expires_at=now + 5,
    )

    import asyncio

    async def resolve_via_rpc():
        await asyncio.sleep(0.01)
        ws = _fake_ws()
        await server._ws_rpc_clarify_resolve(ws, "rpc1", {"id": "id2", "answer": "B"})
        ws.send_json.assert_awaited()  # an ok reply went out

    asyncio.create_task(resolve_via_rpc())
    answer = await mgr.request_and_wait(pending)
    assert answer == "B"


@pytest.mark.asyncio
async def test_resolve_unknown_id_errors():
    server = _server()
    ws = _fake_ws()
    await server._ws_rpc_clarify_resolve(ws, "rpc1", {"id": "ghost", "answer": "x"})
    sent = ws.send_json.await_args.args[0]
    assert sent.get("error") is not None


@pytest.mark.asyncio
async def test_resolve_missing_id_errors():
    server = _server()
    ws = _fake_ws()
    await server._ws_rpc_clarify_resolve(ws, "rpc1", {"answer": "x"})
    sent = ws.send_json.await_args.args[0]
    assert sent.get("error") is not None


@pytest.mark.asyncio
async def test_list_returns_pending():
    server = _server()
    mgr = get_clarify_manager()

    import asyncio

    now = time.time()
    pending = ClarifyRequest(
        id="id3", question="Q?", choices=None,
        session_key="web:2", created_at=now, expires_at=now + 5,
    )

    async def check_list_then_resolve():
        await asyncio.sleep(0.01)
        ws = _fake_ws()
        await server._ws_rpc_clarify_list(ws, "rpc1", {})
        reply = ws.send_json.await_args.args[0]
        items = reply["result"]["clarifies"]
        assert any(i["id"] == "id3" for i in items)
        mgr.resolve("id3", "done")

    asyncio.create_task(check_list_then_resolve())
    await mgr.request_and_wait(pending)
