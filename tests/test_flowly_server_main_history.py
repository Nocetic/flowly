"""A home that keeps no chats on the server reads them here (chat-storage-choice.md).

The server forwards the main agent's ``chat.history`` to this machine only
for such a home; it is answered as the gateway answers its own clients, from
the gateway's own session manager, a page at a time.
"""

from __future__ import annotations

import json

import pytest

from flowly.bus.queue import MessageBus
from flowly.channels.flowly_server import FlowlyServerChannel
from flowly.config.schema import FlowlyServerChannelConfig
from flowly.gateway.server import GatewayServer
from flowly.session.manager import SessionManager
from tests.test_flowly_server_channel import FakeSocket


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "workspace").mkdir(parents=True)
    monkeypatch.setenv("FLOWLY_HOME", str(home))
    return home


def _channel(gateway):
    config = FlowlyServerChannelConfig(enabled=True, server_url="wss://cloud.example.test/v1/agent",
                                       agent_secret="s3cret", agent_host_id="host-1")
    channel = FlowlyServerChannel(config, MessageBus())
    socket = FakeSocket()
    channel._ws = socket
    channel._running = True
    if gateway is not None:
        channel.set_screen_gateway(gateway)
    return channel, socket


def _strip_identities(home) -> None:
    """Sessions saved before rows had archive identities: no ids, no full archive beside them."""
    for full in home.rglob("*.full.jsonl"):
        full.unlink()
    for path in home.rglob("*.jsonl"):
        rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
        for row in rows:
            row.pop("_event_id", None)
            row.pop("_event_seq", None)
        path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")


async def _history(channel, socket, rpc_id: str, **params):
    await channel._handle_relay_message(socket, {"type": "rpc", "id": rpc_id, "sessionId": "c-1",
                                                 "method": "chat.history", "params": {"sessionKey": "web:c1", **params}})
    return socket.rpc(rpc_id)


async def test_the_main_agents_conversation_is_read_as_the_gateway_reads_it(home):
    sessions = SessionManager(home / "workspace")
    chat = sessions.get_or_create("web:c1")
    chat.add_message("user", "plan my week")
    chat.add_message("assistant", "Monday is free.")
    sessions.save(chat)
    gateway = GatewayServer(sessions=sessions)
    channel, socket = _channel(gateway)

    answer = await _history(channel, socket, "h1")
    assert answer["sessionId"] == "c-1", "answered to the app's own session"
    rows = answer["result"]["messages"]
    assert [(r["role"], r["content"][0]["text"] if isinstance(r["content"], list) else r["content"]) for r in rows] == [
        ("user", "plan my week"), ("assistant", "Monday is free.")]
    assert all(isinstance(r.get("timestampMs"), int) for r in rows), "exact times, as named agents' are"
    # Exactly what the gateway's own clients get.
    assert answer["result"]["messages"] == [
        {**r, "timestampMs": answer["result"]["messages"][i]["timestampMs"]}
        for i, r in enumerate(gateway.chat_history_result({"sessionKey": "web:c1"})["messages"])]


async def test_an_old_conversation_pages_through_on_the_same_cursors(home):
    # Rows with no identity get one when a session is loaded: the gateway's own
    # manager keeps them, so the next page's cursor holds (a fresh manager per
    # call refused it, 2026-11-10).
    sessions = SessionManager(home / "workspace")
    chat = sessions.get_or_create("web:c1")
    for i in range(6):
        chat.add_message("user", f"question {i}")
        chat.add_message("assistant", f"answer {i}")
    sessions.save(chat)
    _strip_identities(home)
    channel, socket = _channel(GatewayServer(sessions=SessionManager(home / "workspace")))

    seen, before, n = [], None, 0
    while True:
        n += 1
        answer = await _history(channel, socket, f"p{n}", limit=4, **({"before": before} if before else {}))
        assert "result" in answer, answer
        seen = answer["result"]["messages"] + seen
        if not answer["result"]["hasOlder"]:
            break
        before = answer["result"]["before"]
    assert len(seen) == 12 and len({r["id"] for r in seen}) == 12


async def test_a_machine_without_its_gateway_says_so_and_a_bad_cursor_is_refused(home):
    channel, socket = _channel(None)
    answer = await _history(channel, socket, "u1")
    assert answer["error"]["code"] == "UNAVAILABLE" and answer["error"]["retryable"] is True

    sessions = SessionManager(home / "workspace")
    chat = sessions.get_or_create("web:c1")
    chat.add_message("user", "hi")
    sessions.save(chat)
    channel, socket = _channel(GatewayServer(sessions=sessions))
    answer = await _history(channel, socket, "b1", limit=4, before="not-a-cursor")
    assert answer["error"]["code"] == "INVALID_HISTORY_CURSOR"


async def test_a_named_agents_history_still_goes_to_that_agent(home):
    channel, socket = _channel(GatewayServer(sessions=SessionManager(home / "workspace")))
    called = []

    async def profile_chat(ws, msg, profile):
        called.append(profile)

    channel._dispatch_profile_chat = profile_chat  # type: ignore[method-assign]
    await channel._handle_relay_message(socket, {"type": "rpc", "id": "n1", "method": "chat.history",
                                                 "params": {"sessionKey": "agent:home", "profile": "tsuki"}})
    assert called == ["tsuki"]


def test_core_tells_the_server_it_reads_the_main_agents_conversations_itself():
    from flowly.channels.flowly_server import CHAT_HISTORY_FEATURE

    channel, _ = _channel(None)
    _, kwargs, _ = channel._relay_connection()
    features = kwargs["additional_headers"]["X-Flowly-Agent-Features"].split(",")
    assert CHAT_HISTORY_FEATURE == "chat-history-v1" and CHAT_HISTORY_FEATURE in features
