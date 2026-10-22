"""A Flowly server client watches a cloud screen with only a ticket (PROTOCOL.md C).

``screen.ticket`` (an agent RPC from the Flowly server) wraps the single-use
30 s stream ticket of ``display.observe``. The stream endpoint admits a viewer
that presents only such a ticket, without the gateway token, while every other
route keeps requiring the token. The gateway here enforces auth on loopback,
as on the cloud image behind its socat hop.
"""

from __future__ import annotations

import asyncio
import os

import aiohttp
import pytest

from flowly.bus.queue import MessageBus
from flowly.channels.flowly_server import FlowlyServerChannel
from flowly.config.schema import FlowlyServerChannelConfig
from flowly.gateway import display
from flowly.gateway.display import STREAM_PATH, issue_server_ticket
from tests.test_flowly_server_channel import FakeSocket
from tests.test_screen_display_gateway import RfbSocket, display_gateway, observe  # noqa: F401 - fixture

pytestmark = pytest.mark.skipif(os.name == "nt", reason="private Unix RFB transport is Linux-host only")

APP = "flowly-cloud-test"


@pytest.fixture(autouse=True)
def fly_app(monkeypatch):
    monkeypatch.setenv("FLY_APP_NAME", APP)


async def server_ticket(fixture, profile="default") -> dict:
    return await asyncio.to_thread(issue_server_ticket, fixture.gateway, profile)


def ticket_headers(ticket: dict, **overrides) -> dict:
    headers = {
        "X-Flowly-Screen-Ticket": ticket["ticket"], "X-Flowly-Profile": ticket["profileId"],
        "X-Flowly-Host-Id": ticket["hostId"], "X-Flowly-Bot-Id": ticket["botId"],
        "X-Flowly-Screen-Generation": ticket["generation"],
    }
    headers.update(overrides)
    return headers


async def refused(fixture, headers, path=STREAM_PATH) -> int:
    with pytest.raises(aiohttp.WSServerHandshakeError) as rejected:
        await fixture.client.ws_connect(fixture.server.make_url(path), headers=headers)
    return rejected.value.status


@pytest.mark.parametrize("profile", ["default", "writer"])
async def test_a_server_ticket_alone_opens_that_profiles_stream(display_gateway, profile):
    fixture = display_gateway
    ticket = await server_ticket(fixture, profile)
    status = (await fixture.rpc("display.status", {"profileId": profile}))["result"]
    assert ticket["url"] == f"wss://{APP}.fly.dev{STREAM_PATH}"
    assert ticket["profileId"] == profile and ticket["generation"] == status["generation"]
    assert (ticket["hostId"], ticket["botId"]) == (status["hostId"], status["botId"])
    assert isinstance(ticket["expiresAt"], int) and isinstance(ticket["ticket"], str)
    # Nothing that grants control travels to the server.
    assert "viewerToken" not in ticket
    async with fixture.client.ws_connect(fixture.server.make_url(STREAM_PATH),
                                         headers=ticket_headers(ticket)) as ws:
        await RfbSocket(ws).handshake()


async def test_without_the_token_and_without_a_ticket_the_stream_is_unauthorized(display_gateway):
    fixture = display_gateway
    ticket = await server_ticket(fixture)
    headers = ticket_headers(ticket)
    headers.pop("X-Flowly-Screen-Ticket")
    assert await refused(fixture, headers) == 401
    assert await refused(fixture, ticket_headers(ticket, **{"X-Flowly-Screen-Ticket": "not-a-ticket"})) == 401


async def test_a_replayed_server_ticket_is_refused(display_gateway):
    fixture = display_gateway
    ticket = await server_ticket(fixture)
    async with fixture.client.ws_connect(fixture.server.make_url(STREAM_PATH), headers=ticket_headers(ticket)) as ws:
        await RfbSocket(ws).handshake()
    assert await refused(fixture, ticket_headers(ticket)) == 401


@pytest.mark.parametrize("target", ["profile", "generation"])
async def test_a_ticket_for_another_profile_or_generation_is_refused_and_burnt(display_gateway, target):
    fixture = display_gateway
    ticket = await server_ticket(fixture)
    if target == "profile":
        writer = (await fixture.rpc("display.status", {"profileId": "writer"}))["result"]
        wrong = ticket_headers(ticket, **{
            "X-Flowly-Profile": "writer", "X-Flowly-Host-Id": writer["hostId"],
            "X-Flowly-Bot-Id": writer["botId"], "X-Flowly-Screen-Generation": writer["generation"]})
    else:
        wrong = ticket_headers(ticket, **{"X-Flowly-Screen-Generation": "generation-older"})
    assert await refused(fixture, wrong) == 403
    # The attempt used the ticket up: it no longer opens its own stream either.
    assert await refused(fixture, ticket_headers(ticket)) == 401


async def test_an_expired_server_ticket_is_refused(display_gateway, monkeypatch):
    fixture = display_gateway
    ticket = await server_ticket(fixture)
    registry = fixture.gateway._server_screen_tickets
    monkeypatch.setattr(registry, "_clock", lambda: float("inf"))
    assert await refused(fixture, ticket_headers(ticket)) == 401


async def test_an_owner_ticket_from_display_observe_still_needs_the_token(display_gateway):
    fixture = display_gateway
    _, headers = await observe(fixture)
    headers.pop("Authorization")
    assert await refused(fixture, headers) == 401


async def test_a_server_ticket_opens_no_other_route(display_gateway):
    fixture = display_gateway
    ticket = await server_ticket(fixture)
    headers = ticket_headers(ticket)
    async with fixture.client.post(fixture.server.make_url("/api/auth/ws-ticket"), headers=headers) as response:
        assert response.status == 401
    async with fixture.client.get(fixture.server.make_url("/api/extension/status"), headers=headers) as response:
        assert response.status == 401
    assert await refused(fixture, headers, "/ws") == 401
    # Still unused: the refused routes did not consume it.
    async with fixture.client.ws_connect(fixture.server.make_url(STREAM_PATH), headers=headers) as ws:
        await RfbSocket(ws).handshake()


async def test_repeated_tickets_reuse_one_server_viewer(display_gateway):
    fixture = display_gateway
    first = await server_ticket(fixture)
    second = await server_ticket(fixture)
    assert first["ticket"] != second["ticket"]
    session = display._resolve(fixture.gateway, {"profileId": "default"})
    with session.tickets._connection() as connection:
        viewers = connection.execute("SELECT COUNT(*) FROM viewers").fetchone()[0]
    assert viewers == 1


async def test_a_stopped_screen_or_a_machine_without_a_fly_app_gets_no_ticket(display_gateway, monkeypatch):
    fixture = display_gateway
    monkeypatch.delenv("FLY_APP_NAME")
    with pytest.raises(display.ScreenError) as error:
        await server_ticket(fixture)
    assert error.value.code == "SCREEN_TICKET_UNAVAILABLE"
    monkeypatch.setenv("FLY_APP_NAME", APP)
    runtime = display._resolve(fixture.gateway, {"profileId": "default"}).runtime
    monkeypatch.setattr(runtime, "status", lambda: None)
    with pytest.raises(display.ScreenError) as error:
        await server_ticket(fixture)
    assert error.value.code == "SCREEN_NOT_RUNNING"


def _channel(gateway=None) -> tuple[FlowlyServerChannel, FakeSocket]:
    channel = FlowlyServerChannel(FlowlyServerChannelConfig(
        enabled=True, server_url="wss://cloud.example.test/v1/agent", agent_secret="s", agent_host_id="h"), MessageBus())
    if gateway is not None:
        channel.set_screen_gateway(gateway)
    return channel, FakeSocket()


async def test_screen_ticket_rpc_answers_with_cores_ticket(display_gateway):
    channel, socket = _channel(display_gateway.gateway)
    await channel._handle_relay_message(socket, {"type": "rpc", "id": "s1", "sessionId": "c", "method": "screen.ticket",
                                                 "params": {"profile": "writer"}})
    reply = socket.rpc("s1")
    assert reply["sessionId"] == "c"
    result = reply["result"]
    assert set(result) == {"url", "ticket", "expiresAt", "profileId", "generation", "hostId", "botId"}
    assert result["profileId"] == "writer"
    async with display_gateway.client.ws_connect(display_gateway.server.make_url(STREAM_PATH),
                                                 headers=ticket_headers(result)) as ws:
        await RfbSocket(ws).handshake()


async def test_screen_ticket_rpc_reports_why_there_is_none(display_gateway, monkeypatch):
    channel, socket = _channel()
    await channel._handle_relay_message(socket, {"type": "rpc", "id": "s1", "sessionId": "c", "method": "screen.ticket",
                                                 "params": {}})
    assert socket.rpc("s1")["error"]["code"] == "SCREEN_TICKET_UNAVAILABLE"
    channel, socket = _channel(display_gateway.gateway)
    await channel._handle_relay_message(socket, {"type": "rpc", "id": "s2", "sessionId": "c", "method": "screen.ticket",
                                                 "params": {"profile": "nobody"}})
    assert socket.rpc("s2")["error"]["code"] == "PROFILE_NOT_AVAILABLE"
    await channel._handle_relay_message(socket, {"type": "rpc", "id": "s3", "sessionId": "c", "method": "screen.ticket",
                                                 "params": {"profile": ["default"]}})
    assert socket.rpc("s3")["error"]["code"] == "INVALID_PARAMS"


def test_the_gateway_exempts_exactly_the_stream_route():
    from flowly.gateway.server import GatewayServer

    assert GatewayServer._SCREEN_STREAM_PATH == STREAM_PATH
