"""Gateway transport-rebind: a session's live stream follows the latest socket.

A run streams to the socket that started it. If the client leaves and re-enters
mid-stream it comes back on a NEW socket and calls chat.inflight; without
rebinding, forward events (deltas / iteration_step / final) keep going to the
dead socket and the re-entered view freezes at the snapshot. ``bind_session_ws``
+ ``_session_send`` route every live event to the session's CURRENT socket.
"""

from __future__ import annotations

import pytest

from flowly.gateway.server import GatewayServer


class _FakeWS:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.closed = False

    async def send_json(self, data: dict) -> None:
        self.sent.append(data)


def _bare_server() -> GatewayServer:
    srv = object.__new__(GatewayServer)  # bypass the heavy __init__
    srv._session_ws = {}
    return srv


@pytest.mark.asyncio
async def test_forward_events_follow_reentered_socket() -> None:
    srv = _bare_server()
    ws_a, ws_b = _FakeWS(), _FakeWS()

    # chat.send started the run on ws_a.
    srv.bind_session_ws("web:c1", ws_a)
    await srv._session_send("web:c1", ws_a, {"d": 1})
    assert ws_a.sent == [{"d": 1}]
    assert ws_b.sent == []

    # Client left and re-entered on ws_b (chat.inflight rebinds).
    srv.bind_session_ws("web:c1", ws_b)
    await srv._session_send("web:c1", ws_a, {"d": 2})  # fallback ws_a, current ws_b
    assert ws_a.sent == [{"d": 1}]
    assert ws_b.sent == [{"d": 2}]


@pytest.mark.asyncio
async def test_unbound_session_falls_back_to_originating_socket() -> None:
    srv = _bare_server()
    ws = _FakeWS()
    await srv._session_send("web:none", ws, {"d": 3})
    assert ws.sent == [{"d": 3}]


@pytest.mark.asyncio
async def test_closed_current_socket_is_dropped_silently() -> None:
    srv = _bare_server()
    ws_a, ws_b = _FakeWS(), _FakeWS()
    srv.bind_session_ws("web:c1", ws_b)
    ws_b.closed = True
    # current (ws_b) closed → dropped; must not raise, must not hit fallback.
    await srv._session_send("web:c1", ws_a, {"d": 4})
    assert ws_a.sent == []
    assert ws_b.sent == []


def test_bind_ignores_empty_session_key() -> None:
    srv = _bare_server()
    ws = _FakeWS()
    srv.bind_session_ws("", ws)
    assert srv._session_ws == {}
