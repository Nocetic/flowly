"""Gateway HTTP API for the Board: GET /api/board, POST /api/board/action.

Isolation: a BoardStore backed by ``tmp_path`` is injected straight into the
GatewayServer, and FLOWLY_HOME is redirected to a tmp dir as a belt-and-
suspenders guard so nothing can reach the real ``~/.flowly``.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from flowly.board.store import BoardStore, STATUS_DONE, STATUS_IN_PROGRESS
from flowly.gateway.server import GatewayServer


@pytest.fixture(autouse=True)
def _isolate_home(monkeypatch, tmp_path):
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path / "home"))


@pytest.fixture
def store(tmp_path):
    s = BoardStore(tmp_path / "board.db")
    yield s
    s.close()


def _server(store, subagent_manager=None) -> GatewayServer:
    server = GatewayServer(
        host="127.0.0.1",
        port=0,
        on_cron_run=AsyncMock(),
        on_cron_health=AsyncMock(),
        on_cron_reload=AsyncMock(),
        on_chat_message=AsyncMock(),
        sessions=MagicMock(),
        board_store=store,
    )
    if subagent_manager is not None:
        server._subagent_manager = subagent_manager
    return server


class _Req:
    """Minimal stand-in for an aiohttp request: only json() is used."""

    def __init__(self, payload=None):
        self._payload = payload or {}

    async def json(self):
        return self._payload


def _body(resp):
    return json.loads(resp.text)


@pytest.mark.asyncio
async def test_route_registered_only_with_store(store):
    with_store = _server(store)._create_app()
    paths = {r.resource.canonical for r in with_store.router.routes()}
    assert "/api/board" in paths
    assert "/api/board/action" in paths

    without = GatewayServer(host="127.0.0.1", port=0, on_chat_message=AsyncMock())._create_app()
    paths2 = {r.resource.canonical for r in without.router.routes()}
    assert "/api/board" not in paths2


@pytest.mark.asyncio
async def test_snapshot_empty(store):
    server = _server(store)
    resp = await server._handle_board_snapshot(_Req())
    data = _body(resp)
    assert data["total"] == 0
    assert [c["status"] for c in data["columns"]] == [
        "todo", "in_progress", "waiting", "done"
    ]


@pytest.mark.asyncio
async def test_action_add_then_snapshot(store):
    server = _server(store)
    resp = await server._handle_board_action(
        _Req({"action": "add", "title": "from desktop", "originChannel": "desktop"})
    )
    added = _body(resp)
    assert added["ok"] is True
    assert added["card"]["title"] == "from desktop"

    snap = _body(await server._handle_board_snapshot(_Req()))
    assert snap["total"] == 1
    assert snap["columns"][0]["cards"][0]["title"] == "from desktop"


@pytest.mark.asyncio
async def test_action_move(store):
    card = store.add_card("task")
    server = _server(store)
    resp = await server._handle_board_action(
        _Req({"action": "move", "cardId": card.id, "status": STATUS_IN_PROGRESS})
    )
    data = _body(resp)
    assert data["ok"] is True
    assert data["card"]["status"] == STATUS_IN_PROGRESS


@pytest.mark.asyncio
async def test_action_move_bad_status(store):
    card = store.add_card("task")
    server = _server(store)
    resp = await server._handle_board_action(
        _Req({"action": "move", "cardId": card.id, "status": "bogus"})
    )
    assert resp.status == 400
    assert _body(resp)["ok"] is False


@pytest.mark.asyncio
async def test_action_note_and_delete(store):
    card = store.add_card("task")
    server = _server(store)
    await server._handle_board_action(
        _Req({"action": "note", "cardId": card.id, "text": "hi"})
    )
    got = _body(await server._handle_board_action(_Req({"action": "delete", "cardId": card.id})))
    assert got["ok"] is True
    assert store.get_card(card.id) is None


@pytest.mark.asyncio
async def test_action_cancel_calls_subagent_manager(store):
    card = store.add_card("task")
    store.set_status(card.id, STATUS_IN_PROGRESS)
    store.set_run_id(card.id, "run-42")
    mgr = MagicMock()
    mgr.cancel = AsyncMock()
    server = _server(store, subagent_manager=mgr)

    resp = await server._handle_board_action(_Req({"action": "cancel", "cardId": card.id}))
    data = _body(resp)
    assert data["ok"] is True
    assert data["card"]["status"] == "cancelled"
    mgr.cancel.assert_awaited_once_with("run-42")


@pytest.mark.asyncio
async def test_action_run_invokes_orchestrator(store):
    import asyncio

    card = store.add_card("do it")

    class _Orch:
        def __init__(self):
            self.ran = []

        async def run_card(self, card_id, *, deliver=True):
            self.ran.append(card_id)

    orch = _Orch()
    server = _server(store)
    server.board_orchestrator = orch

    resp = await server._handle_board_action(_Req({"action": "run", "cardId": card.id}))
    data = _body(resp)
    assert data["ok"] is True and data["status"] == "started"
    await asyncio.sleep(0)  # let the backgrounded run_card execute
    assert orch.ran == [card.id]


@pytest.mark.asyncio
async def test_action_run_without_orchestrator(store):
    server = _server(store)  # no orchestrator
    card = store.add_card("x")
    resp = await server._handle_board_action(_Req({"action": "run", "cardId": card.id}))
    assert resp.status == 400


@pytest.mark.asyncio
async def test_action_cancel_prefers_orchestrator(store):
    card = store.add_card("task")
    store.set_status(card.id, STATUS_IN_PROGRESS)

    class _Orch:
        def __init__(self):
            self.cancelled = []

        async def cancel_card(self, card_id):
            self.cancelled.append(card_id)
            from flowly.board.store import STATUS_CANCELLED
            store.set_status(card_id, STATUS_CANCELLED, error="cancelled")
            return True

    orch = _Orch()
    server = _server(store)
    server.board_orchestrator = orch

    resp = await server._handle_board_action(_Req({"action": "cancel", "cardId": card.id}))
    data = _body(resp)
    assert data["ok"] is True
    assert data["card"]["status"] == "cancelled"
    assert orch.cancelled == [card.id]


@pytest.mark.asyncio
async def test_action_unknown(store):
    server = _server(store)
    resp = await server._handle_board_action(_Req({"action": "frobnicate"}))
    assert resp.status == 400


@pytest.mark.asyncio
async def test_action_missing_card_id(store):
    server = _server(store)
    resp = await server._handle_board_action(_Req({"action": "move", "status": "done"}))
    assert resp.status == 400


@pytest.mark.asyncio
async def test_apply_board_action_add_then_delete(store):
    server = _server(store)
    res, status = await server._apply_board_action({"action": "add", "title": "x"})
    assert status == 200 and res["ok"] is True
    cid = res["card"]["id"]
    res2, status2 = await server._apply_board_action({"action": "delete", "cardId": cid})
    assert status2 == 200 and res2["ok"] is True
    assert store.get_card(cid) is None


@pytest.mark.asyncio
async def test_apply_board_action_clear_done(store):
    a = store.add_card("a")
    b = store.add_card("b")
    store.set_status(a.id, STATUS_DONE)
    store.set_status(b.id, STATUS_DONE)
    store.add_card("keep")  # todo
    server = _server(store)
    res, status = await server._apply_board_action({"action": "clear_done"})
    assert status == 200 and res["ok"] is True and res["removed"] == 2
    assert store.get_card(a.id) is None
    assert len(store.list_cards()) == 1


@pytest.mark.asyncio
async def test_apply_board_action_unknown(store):
    server = _server(store)
    res, status = await server._apply_board_action({"action": "frobnicate"})
    assert status == 400 and res["ok"] is False


@pytest.mark.asyncio
async def test_ws_rpc_board_action_ok_and_error(store):
    server = _server(store)
    replies: list = []
    errors: list = []

    async def fake_reply(ws, rid, result):
        replies.append(result)

    async def fake_error(ws, rid, code, msg):
        errors.append(msg)

    server._ws_rpc_reply = fake_reply  # type: ignore[assignment]
    server._ws_rpc_error = fake_error  # type: ignore[assignment]

    await server._ws_rpc_board_action(None, "1", {"action": "add", "title": "y"})
    assert replies and replies[0]["ok"] is True

    await server._ws_rpc_board_action(None, "2", {"action": "frob"})
    assert errors  # unknown action surfaces as an rpc error


@pytest.mark.asyncio
async def test_ws_rpc_board_snapshot(store):
    store.add_card("a")
    store.add_card("b")
    server = _server(store)
    replies: list = []

    async def fake_reply(ws, rpc_id, result):
        replies.append(result)

    server._ws_rpc_reply = fake_reply  # type: ignore[assignment]
    await server._ws_rpc_board_snapshot(None, "1", {})
    assert replies[0]["snapshot"]["total"] == 2


@pytest.mark.asyncio
async def test_ws_rpc_board_snapshot_no_store():
    server = GatewayServer(host="127.0.0.1", port=0, on_chat_message=AsyncMock())
    replies: list = []

    async def fake_reply(ws, rpc_id, result):
        replies.append(result)

    server._ws_rpc_reply = fake_reply  # type: ignore[assignment]
    await server._ws_rpc_board_snapshot(None, "1", {})
    assert replies[0]["snapshot"] is None


@pytest.mark.asyncio
async def test_push_session_message_broadcasts_chat_final(store):
    """Proactive board delivery reaches WS clients as a normal chat 'final'
    event — so the TUI/desktop render it like any assistant reply."""
    server = _server(store)

    sent: list = []

    class _FakeWS:
        closed = False

        async def send_json(self, data):
            sent.append(data)

    server._ws_clients = {"c1": _FakeWS(), "c2": _FakeWS()}
    await server.push_session_message("cli:user", "your news summary is ready")

    assert len(sent) == 2  # broadcast to both clients
    ev = sent[0]
    assert ev["type"] == "event" and ev["event"] == "chat"
    assert ev["data"]["state"] == "final"
    assert ev["data"]["proactive"] is True
    assert ev["data"]["sessionKey"] == "cli:user"
    assert ev["data"]["message"]["content"][0]["text"] == "your news summary is ready"


@pytest.mark.asyncio
async def test_push_session_message_empty_is_noop(store):
    server = _server(store)
    sent: list = []

    class _FakeWS:
        closed = False

        async def send_json(self, data):
            sent.append(data)

    server._ws_clients = {"c1": _FakeWS()}
    await server.push_session_message("cli:user", "")
    assert sent == []


@pytest.mark.asyncio
async def test_real_http_roundtrip(store):
    """Exercise the actual aiohttp app over real HTTP: routing + JSON wire."""
    from aiohttp.test_utils import TestClient, TestServer

    store.add_card("seed", origin_channel="telegram", origin_chat_id="9")
    server = _server(store)
    app = server._create_app()

    async with TestClient(TestServer(app)) as client:
        r = await client.get("/api/board")
        assert r.status == 200
        data = await r.json()
        assert data["total"] == 1
        assert data["columns"][0]["cards"][0]["originChannel"] == "telegram"

        r2 = await client.post(
            "/api/board/action", json={"action": "add", "title": "via http"}
        )
        assert r2.status == 200
        assert (await r2.json())["ok"] is True

        r3 = await client.get("/api/board")
        assert (await r3.json())["total"] == 2
