"""A person's message reaches every other open view of its chat at once.

Two views of one chat (Desktop and a phone, or a chat beside a voice call) each
hear the reply as it streams (``_session_send``). The person's own words were
told only to the socket that sent them: the other view showed the reply under
no question until it read the history again. The gateway now tells the other
views the message as the run's ``user`` state, before any of the reply, in the
shape an agent-authored prompt already has; the sending view, which shows it
already, is never told it twice.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from flowly.gateway.server import GatewayServer


class Socket:
    closed = False

    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_json(self, frame: dict) -> None:
        self.sent.append(frame)

    async def send(self, frame: str) -> None:
        await self.send_json(json.loads(frame))


def user_events(socket: Socket) -> list[dict]:
    return [frame["data"] for frame in socket.sent
            if frame.get("event") == "chat" and frame.get("data", {}).get("state") == "user"]


def _server(seen: list[str]) -> GatewayServer:
    async def run(*args):
        seen.append(args[2])
        return "Monday is free.", {}

    return GatewayServer(on_chat_message=run)


async def _send(server: GatewayServer, socket: Socket, params: dict) -> None:
    await server._ws_rpc_chat_send(socket, "client", "rpc-1", params)
    await asyncio.gather(*list(server._active_tasks.values()))


@pytest.mark.asyncio
async def test_another_view_of_the_chat_hears_the_persons_message_before_the_reply() -> None:
    seen: list[str] = []
    server = _server(seen)
    phone, desktop = Socket(), Socket()
    server.bind_session_ws("web:c1", desktop)  # Desktop has the chat open (chat.history)
    await _send(server, phone, {"sessionKey": "web:c1", "idempotencyKey": "m1", "message": "plan my week"})

    assert user_events(desktop) == [{
        "state": "user", "runId": "m1", "sessionKey": "web:c1",
        "message": {"role": "user", "content": [{"type": "text", "text": "plan my week"}]},
    }]
    first_user = next(i for i, f in enumerate(desktop.sent) if f.get("data", {}).get("state") == "user")
    assert all(i > first_user for i, f in enumerate(desktop.sent) if f.get("event") == "agent")
    # The phone shows its own message already: never told it again.
    assert user_events(phone) == []
    assert seen  # the turn ran


@pytest.mark.asyncio
async def test_a_turn_the_person_did_not_type_is_not_told_as_their_words() -> None:
    server = _server([])
    sender, viewer = Socket(), Socket()
    server.bind_session_ws("web:c1", viewer)
    await _send(server, sender, {"sessionKey": "web:c1", "idempotencyKey": "r1",
                                 "message": "Daily summary", "turnOrigin": "routine"})
    assert user_events(viewer) == []


@pytest.mark.asyncio
async def test_a_resent_message_is_told_once() -> None:
    server = _server([])
    phone, desktop = Socket(), Socket()
    server.bind_session_ws("web:c1", desktop)
    params = {"sessionKey": "web:c1", "idempotencyKey": "m1", "message": "plan my week"}
    await _send(server, phone, params)
    await _send(server, phone, params)
    assert len(user_events(desktop)) == 1


@pytest.mark.asyncio
async def test_the_profile_host_is_never_told_a_direct_view_s_message() -> None:
    """The primary's in-process socket to the Flowly server's side keeps its
    own log of what it delivered: a message it did not carry is not its to keep."""
    server = _server([])
    phone = Socket()
    host = Socket()
    server._profile_host_socket = host
    server.bind_session_ws("web:c1", host)
    await _send(server, phone, {"sessionKey": "web:c1", "idempotencyKey": "m1", "message": "plan my week"})
    assert user_events(host) == []
