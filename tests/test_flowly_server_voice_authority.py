"""Voice account identity over the Flowly server's agent socket (server PROTOCOL H1).

The server offers the relay's ``voice-relay-authority-v1`` handshake only to
a Core that asks for it. After the handshake Core accepts a client's frames
only in signed ``relay.browser`` envelopes, as with the relay, but the
server's own calls (inbox ``chat.send`` under a ``srv-`` session, power and
provider calls without one) keep arriving plain and must still run.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from flowly.bus.queue import MessageBus
from flowly.channels.flowly_server import VOICE_AUTHORITY_FEATURE, FlowlyServerChannel
from flowly.config.schema import FlowlyServerChannelConfig
from flowly.live_voice.relay_transport import RelayBrowserVerifier
from flowly.power.monitor import JUST_WOKE_SECONDS, PowerMonitor
from tests.relay_voice_helpers import TEST_RELAY_KEY, relay_frame
from tests.test_flowly_server_channel import Clock, FakeSocket

HOST_ID = "host-1"


def _config(url: str = "wss://cloud.example.test/v1/agent") -> FlowlyServerChannelConfig:
    return FlowlyServerChannelConfig(enabled=True, server_url=url, agent_secret="s3cret", agent_host_id=HOST_ID)


def _enabled_channel():
    """A server channel after the handshake, with a power monitor, on a fake socket."""
    clock = Clock()
    channel = FlowlyServerChannel(_config(), MessageBus())
    monitor = PowerMonitor(flush=lambda: None, wall=clock.wall, monotonic=clock.mono)
    clock.now += JUST_WOKE_SECONDS + 1
    channel.set_power_monitor(monitor)
    socket = FakeSocket()
    channel._ws = socket
    channel._running = True
    channel._relay_authority = RelayBrowserVerifier(
        {"version": 1, "serverId": HOST_ID, "linkId": "test-link", "key": TEST_RELAY_KEY}, server_id=HOST_ID)
    channel._relay_authority_enabled = True
    channel._test_relay_sequence = 0
    return channel, socket, monitor, clock


def test_core_asks_the_server_for_voice_identity():
    channel = FlowlyServerChannel(_config(), MessageBus())
    _, kwargs, _ = channel._relay_connection()
    assert kwargs["additional_headers"]["X-Flowly-Agent-Features"] == VOICE_AUTHORITY_FEATURE == "voice-authority-v1"


def test_after_the_handshake_the_servers_own_calls_stay_accepted_and_a_clients_plain_rpc_does_not():
    channel, socket, *_ = _enabled_channel()
    for own in ({"type": "rpc", "id": "p1", "method": "power.status", "params": {}},
                {"type": "rpc", "id": "k1", "method": "provider.list", "params": {}},
                {"type": "rpc", "id": "m1", "sessionId": "srv-default-conv1", "method": "chat.send", "params": {}}):
        assert channel._decode_relay_message(socket, own) == own
    # A client's frame without its envelope: the relay's rule, refused.
    assert channel._decode_relay_message(socket, {"type": "rpc", "id": "c1", "sessionId": "c-123",
                                                  "method": "voice.list", "params": {}}) is None
    # Lifecycle frames come wrapped too.
    assert channel._decode_relay_message(socket, {"type": "browser-connected", "sessionId": "c-123"}) is None


def test_before_the_handshake_every_plain_frame_is_still_accepted():
    channel = FlowlyServerChannel(_config(), MessageBus())
    socket = FakeSocket()
    channel._ws = socket
    frame = {"type": "rpc", "id": "c1", "sessionId": "c-123", "method": "voice.list", "params": {}}
    assert channel._decode_relay_message(socket, frame) == frame


async def test_power_status_from_the_server_is_answered_after_the_handshake():
    channel, socket, *_ = _enabled_channel()
    await channel._handle_relay_message(socket, {"type": "rpc", "id": "p1", "method": "power.status", "params": {}})
    assert socket.rpc("p1")["result"]["busy"] == []


async def test_a_wrapped_client_screen_ticket_is_answered_and_counts_as_activity():
    channel, socket, monitor, clock = _enabled_channel()
    await channel._handle_relay_message(socket, relay_frame(channel, {
        "id": "s1", "sessionId": "c-123", "method": "screen.ticket", "params": {"profile": "default"}}, uid="account-1"))
    reply = socket.rpc("s1")
    assert reply["sessionId"] == "c-123"
    assert reply["error"]["code"] == "SCREEN_TICKET_UNAVAILABLE"
    assert monitor.status()["lastActivityAt"] == int(clock.now * 1000)


async def test_a_wrapped_power_call_from_a_client_is_still_refused():
    channel, socket, *_ = _enabled_channel()
    await channel._handle_relay_message(socket, relay_frame(channel, {
        "id": "p9", "sessionId": "c-123", "method": "power.sleep", "params": {}}, uid="account-1"))
    assert socket.rpc("p9")["error"]["code"] == "FORBIDDEN"


async def test_the_servers_own_call_cannot_carry_an_account_certificate(monkeypatch):
    channel, socket, *_ = _enabled_channel()

    async def must_not_dispatch(*_args):
        raise AssertionError("an unattested account call was dispatched")

    monkeypatch.setattr(channel, "_dispatch_rpc", must_not_dispatch)
    await channel._handle_relay_message(socket, {"type": "rpc", "id": "v1", "sessionId": "srv-default-x",
                                                 "method": "voice.list", "params": {"voiceAccess": "a.b.c"}})
    assert socket.rpc("v1")["error"]["code"] == "VOICE_AUTH_UNAVAILABLE"


@pytest.fixture
def certificates(monkeypatch):
    import flowly.profile as profiles
    from flowly.channels import feature_rpc
    from flowly.live_voice.access import VoiceAccessVerifier
    from tests.test_voice_access import HOST
    from tests.test_voice_access import fixture as certificate_fixture

    token, _, keys, _, _ = certificate_fixture()
    monkeypatch.setattr(feature_rpc, "_voice_access_verifier", VoiceAccessVerifier(fetch_keys=keys))
    monkeypatch.setattr(profiles, "get_or_create_profile_host_id", lambda: HOST)
    return token


async def test_real_server_connection_negotiates_and_runs_account_and_server_calls(certificates, monkeypatch):
    """A local stand-in for the Flowly server, over a real WebSocket: the handshake,
    an account RPC with its certificate, the server's own power call, and a
    client's unwrapped frame (dropped)."""
    import websockets

    from flowly.live_voice.authority import RequestOwner, current_request_owner

    token = certificates
    channel = FlowlyServerChannel(_config(), MessageBus())
    owners = []

    async def dispatch(reply, request):
        owners.append((request["method"], current_request_owner()))
        await reply.send(json.dumps({"type": "rpc", "id": request["id"], "sessionId": request.get("sessionId", ""),
                                     "result": {"ok": True}}))

    monkeypatch.setattr(channel, "_dispatch_rpc", dispatch)
    headers: dict = {}
    done = asyncio.get_running_loop().create_future()

    async def server(socket):
        try:
            headers.update(socket.request.headers)
            await socket.send(json.dumps({"type": "ready", "protocol": 1, "cronSessionId": None,
                                          "capabilities": ["voice-relay-authority-v1"],
                                          "relayAuthority": {"version": 1, "key": TEST_RELAY_KEY,
                                                             "serverId": HOST_ID, "linkId": "test-link"}}))

            async def next_frame(kind):
                while True:
                    frame = json.loads(await asyncio.wait_for(socket.recv(), 2))
                    if frame.get("type") == kind:
                        return frame

            assert await next_frame("relay.authority.enable") == {
                "type": "relay.authority.enable", "version": 1, "linkId": "test-link"}
            await socket.send(json.dumps({"type": "relay.authority.enabled", "version": 1, "linkId": "test-link"}))
            channel._test_relay_sequence = 0
            # A client's unwrapped frame after the handshake: never answered.
            await socket.send(json.dumps({"type": "rpc", "id": "plain", "sessionId": "c-1", "method": "voice.list",
                                          "params": {}}))
            await socket.send(json.dumps(relay_frame(channel, {
                "id": "acct", "sessionId": "c-1", "method": "voice.list",
                "params": {"voiceAccess": token({"sub": "account-1"})}}, uid="account-1")))
            account = await next_frame("rpc")
            assert account["id"] == "acct"
            assert account["result"] == {"ok": True}
            assert account["voiceDelivery"]["userId"] == "account-1"
            assert account["voiceDelivery"]["sessionId"] == "c-1"
            await socket.send(json.dumps({"type": "rpc", "id": "srv-p1", "method": "power.status", "params": {}}))
            power = await next_frame("rpc")
            assert power["id"] == "srv-p1" and "busy" in power["result"]
            done.set_result(True)
        except BaseException as error:  # noqa: BLE001 - surfaced through the future
            if not done.done():
                done.set_exception(error)

    clock = Clock()
    channel.set_power_monitor(PowerMonitor(flush=lambda: None, wall=clock.wall, monotonic=clock.mono))
    async with websockets.serve(server, "127.0.0.1", 0) as running:
        port = running.sockets[0].getsockname()[1]
        channel.server_config = _config(f"ws://127.0.0.1:{port}/v1/agent")
        assert channel._ready_to_connect()
        channel._running = True
        task = asyncio.create_task(channel._connect_and_run())
        try:
            await asyncio.wait_for(done, 4)
        finally:
            channel._running = False
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert headers.get("x-flowly-agent-features") == "voice-authority-v1"
    assert owners == [("voice.list", RequestOwner("account-1"))]
    channel.chat_commands.close()
