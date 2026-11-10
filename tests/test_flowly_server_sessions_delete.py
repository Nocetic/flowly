"""An app chat deleted through the Flowly server (server PROTOCOL A11).

The apps of a home that keeps no chats on the server delete its chats on the
machine, as a direct gateway's do. The server channel did not know
``sessions.delete``, so every delete from a phone failed (2026-11-10).
"""

from __future__ import annotations

import pytest

from flowly.gateway.server import GatewayServer
from flowly.session.manager import SessionManager
from flowly.session.reactions import read_reactions, set_reaction
from tests.test_flowly_server_main_history import _channel
from tests.test_flowly_server_channel import FakeSocket  # noqa: F401


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path))
    (tmp_path / "workspace").mkdir()
    return tmp_path


@pytest.mark.asyncio
async def test_an_app_chat_is_deleted_through_the_server_and_nothing_else_is(home):
    sessions = SessionManager(home / "workspace")
    for key in ("web:c1", "telegram:42"):
        chat = sessions.get_or_create(key)
        chat.add_message("user", "plan my week")
        chat.add_message("assistant", "Monday is free.")
        sessions.save(chat)
    gateway = GatewayServer(sessions=sessions)
    reply = gateway.chat_history_result({"sessionKey": "web:c1"})["messages"][1]["reactId"]
    set_reaction(sessions, "web:c1", reply, "user", "❤️")
    channel, socket = _channel(gateway)

    await channel._handle_relay_message(socket, {"type": "rpc", "id": "r1", "sessionId": "c-1", "method": "sessions.delete",
                                                 "params": {"sessionKey": "web:c1"}})
    assert socket.rpc("r1")["result"] == {"deleted": True, "sessionKey": "web:c1"}
    assert "web:c1" not in [s["key"] for s in sessions.list_sessions()]
    assert read_reactions(sessions, "web:c1") == {}, "its reactions go with it"

    # Another channel's conversation is never deleted from afar.
    await channel._handle_relay_message(socket, {"type": "rpc", "id": "r2", "sessionId": "c-1", "method": "sessions.delete",
                                                 "params": {"sessionKey": "telegram:42"}})
    assert socket.rpc("r2")["error"]["code"] == "INVALID_REQUEST"
    assert "telegram:42" in [s["key"] for s in sessions.list_sessions()]
