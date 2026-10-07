"""Real authenticated gateway and private Unix transport; synthetic RFB peer."""

import asyncio
import base64
import json
import os
import struct
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiohttp
import pytest
from aiohttp.test_utils import TestServer

import flowly.profile as profiles
from flowly.desktop_runtime.authority import ScreenAuthority
from flowly.desktop_runtime.input_service import InputService, InputServiceClient
from flowly.desktop_runtime.launcher import ScreenRuntime
from flowly.desktop_runtime.observers import ObserverRegistry
from flowly.gateway.display import STREAM_PATH, _resolve
from flowly.gateway.server import GatewayServer

pytestmark = pytest.mark.skipif(os.name == "nt", reason="private Unix RFB transport is Linux-host only")


@pytest.fixture
async def display_gateway(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(profiles, "_DEFAULT_HOME", home)
    monkeypatch.setattr(profiles, "_PROFILES_ROOT", home / "profiles")
    monkeypatch.setenv("FLOWLY_HOME", str(home))
    monkeypatch.setattr(ScreenRuntime, "capabilities", staticmethod(lambda: {
        "supported": True, "reason": None, "missing": [],
    }))
    profiles.create_profile("writer", local_runtime=True)
    gateway = GatewayServer(host="127.0.0.1", port=0, auth_token="display-test-owner",
                            require_loopback_auth=True, enable_profile_host=True)
    gateway.on_chat_message = AsyncMock()
    server = TestServer(gateway._create_app())
    await server.start_server()
    backends = []
    registries = []
    input_services = {}
    tasks = set()
    received = []
    received_event = asyncio.Event()
    with tempfile.TemporaryDirectory(prefix="display-gateway-", dir="/tmp") as short:
        for profile in ("default", "writer"):
            service = _resolve(gateway, {"profileId": profile})
            sockets = Path(short) / profile
            sockets.mkdir()
            authority = ScreenAuthority(tmp_path / f"authority-{profile}")
            generation = f"generation-{profile}"
            authority.start(generation)
            registries.append(ObserverRegistry(authority, generation, sockets, viewers=service.viewers))
            input_services[profile] = InputService(authority, generation, sockets, observers=registries[-1],
                                                  viewers=service.viewers)
            service._runtime = SimpleNamespace(
                socket_directory=sockets, authority=authority,
                assert_current_directory=lambda: None,
                rfb_socket_for=lambda state, directory=sockets: directory / "rfb.sock",
                status=lambda gen=generation: {
                    "phase": "running", "generation": gen,
                    "socket": "/must-not-use-manifest-path", "cdpPort": 9999,
                    "observerServiceVersion": 3, "inputServiceVersion": 3,
                },
            )

            async def peer(reader, writer, name=profile):
                task = asyncio.current_task()
                tasks.add(task)
                try:
                    writer.write(b"RFB 003.008\n")
                    await writer.drain()
                    assert await reader.readexactly(12) == b"RFB 003.008\n"
                    writer.write(b"\x01\x01")
                    await writer.drain()
                    assert await reader.readexactly(1) == b"\x01"
                    writer.write(bytes(4))
                    await writer.drain()
                    assert await reader.readexactly(1) == b"\x01"
                    writer.write(bytes(24))
                    await writer.drain()
                    while data := await reader.read(65536):
                        received.append((name, data))
                        received_event.set()
                except (asyncio.IncompleteReadError, ConnectionError):
                    pass
                finally:
                    writer.close()
                    await writer.wait_closed()
                    tasks.discard(task)

            backends.append(await asyncio.start_unix_server(peer, sockets / "rfb.sock"))

        async with aiohttp.ClientSession() as client:
            async with client.post(server.make_url("/api/auth/ws-ticket"),
                                   headers={"Authorization": "Bearer display-test-owner"}) as response:
                ticket = (await response.json())["ticket"]
            socket = await client.ws_connect(server.make_url("/ws?ticket=" + ticket))

            async def rpc(method, params=None):
                await socket.send_json({"type": "rpc", "id": "request", "method": method, "params": params or {}})
                return await socket.receive_json(timeout=5)

            yield SimpleNamespace(server=server, client=client, gateway=gateway, rpc=rpc,
                                  socket=socket, received=received, received_event=received_event, input_services=input_services)
            await socket.close()
        await server.close()
        for registry in registries:
            await asyncio.to_thread(registry.close)
        for service in input_services.values():
            await asyncio.to_thread(service.close)
        for backend in backends:
            backend.close()
            await backend.wait_closed()
        if tasks:
            await asyncio.gather(*tuple(tasks), return_exceptions=True)


async def observe(fixture, profile="default"):
    status = (await fixture.rpc("display.status", {"profileId": profile}))["result"]
    request = {"profileId": profile, "expectedHostId": status["hostId"],
               "expectedBotId": status["botId"], "generation": status["generation"]}
    observed = (await fixture.rpc("display.observe", request))["result"]
    return observed, {
        "Authorization": "Bearer display-test-owner", "X-Flowly-Profile": profile,
        "X-Flowly-Host-Id": status["hostId"], "X-Flowly-Bot-Id": status["botId"],
        "X-Flowly-Screen-Generation": status["generation"], "X-Flowly-Screen-Ticket": observed["ticket"],
    }


def control_params(view, epoch, operation):
    return {"profileId": view["profileId"], "expectedHostId": view["hostId"],
            "expectedBotId": view["botId"], "generation": view["generation"],
            "viewerToken": view["viewerToken"], "epoch": epoch, "operationId": operation}


async def test_public_takeover_requires_capability_and_retries_do_not_release_new_control(display_gateway):
    fixture = display_gateway
    view, _ = await observe(fixture)
    params = control_params(view, 1, "acquire-a")
    rejected = await fixture.rpc("display.control.acquire", {**params, "viewerToken": view["viewerId"]})
    assert rejected["error"]["code"] == "VIEWER_EXPIRED"
    acquired = (await fixture.rpc("display.control.acquire", params))["result"]
    assert acquired["control"]["control"] == "human"
    assert (await fixture.rpc("display.control.acquire", params))["result"] == acquired
    release = control_params(view, acquired["control"]["epoch"], "release-a")
    released = (await fixture.rpc("display.control.release", release))["result"]
    assert released["control"]["control"] == "agent"
    newer = (await fixture.rpc("display.control.acquire", control_params(view, released["control"]["epoch"],
                                                                       "acquire-b")))["result"]
    assert (await fixture.rpc("display.control.release", release))["result"] == released
    assert (await fixture.rpc("display.status"))["result"]["control"] == newer["control"]
    conflict = await fixture.rpc("display.control.release", {**release, "epoch": newer["control"]["epoch"]})
    assert conflict["error"]["code"] == "OPERATION_CONFLICT"


async def test_public_holder_reconnect_and_explicit_reclaim_after_lost_app_capability(display_gateway):
    fixture = display_gateway
    view, _ = await observe(fixture)
    acquired = (await fixture.rpc("display.control.acquire", control_params(view, 1, "acquire")))["result"]
    target = {key: value for key, value in control_params(view, 1, "unused").items()
              if key not in {"epoch", "operationId"}}
    resumed = (await fixture.rpc("display.observe", target))["result"]
    assert resumed["viewerId"] == view["viewerId"] and resumed["ticket"] != view["ticket"]
    assert "viewerToken" not in resumed
    # Starting another app session creates only a capability, never pixels or input.
    replacement = (await fixture.rpc("display.viewer.create", {
        key: value for key, value in target.items() if key != "viewerToken"
    }))["result"]
    busy = await fixture.rpc("display.control.acquire", control_params(replacement, acquired["control"]["epoch"], "busy"))
    assert busy["error"]["code"] == "SCREEN_BUSY"
    reclaimed = (await fixture.rpc("display.control.reclaim", control_params(
        replacement, acquired["control"]["epoch"], "reclaim")))["result"]
    assert reclaimed["control"]["control"] == "human"
    assert (await fixture.rpc("display.observe", target))["error"]["code"] == "HUMAN_HAS_CONTROL"
    stale = await fixture.rpc("display.control.release", control_params(view, reclaimed["control"]["epoch"], "old"))
    assert stale["error"]["code"] == "VIEWER_MISMATCH"


async def test_public_control_capability_cannot_cross_profiles_or_older_runtime(display_gateway):
    fixture = display_gateway
    default, _ = await observe(fixture)
    writer, _ = await observe(fixture, "writer")
    params = {**control_params(writer, 1, "wrong"), "viewerToken": default["viewerToken"]}
    assert (await fixture.rpc("display.control.acquire", params))["error"]["code"] == "VIEWER_EXPIRED"
    runtime = _resolve(fixture.gateway, {}).runtime
    runtime.status = lambda: {"phase": "running", "generation": default["generation"], "inputServiceVersion": 1}
    rejected = await fixture.rpc("display.control.acquire", control_params(default, 1, "old-runtime"))
    assert rejected["error"]["code"] == "SCREEN_UPGRADE_REQUIRED"


@pytest.mark.parametrize("framed", [False, True])
async def test_unframed_or_stale_epoch_input_cannot_bypass_human_authority(display_gateway, framed):
    fixture = display_gateway
    view, headers = await observe(fixture)
    if framed:
        headers["X-Flowly-Screen-Input"] = "epoch-u64-v1"
    async with fixture.client.ws_connect(fixture.server.make_url(STREAM_PATH), headers=headers) as ws:
        rfb = RfbSocket(ws, epoch=0 if framed else None)
        await rfb.handshake()
        human = (await fixture.rpc("display.control.acquire", control_params(view, 1, "acquire")))["result"]["control"]
        assert human["epoch"] == 2 and human["control"] == "human"
        key = struct.pack("!BBHI", 4, 1, 0, ord("x"))
        update = struct.pack("!BBHHHH", 3, 1, 0, 0, 64, 48)
        await rfb.send(key + update, epoch=1 if framed else None)
        await asyncio.wait_for(fixture.received_event.wait(), 5)
        assert fixture.received == [("default", update)]
        assert fixture.input_services["default"]._broker is None


async def test_a_partial_key_cannot_cross_a_takeover_epoch(display_gateway):
    fixture = display_gateway
    view, headers = await observe(fixture)
    headers["X-Flowly-Screen-Input"] = "epoch-u64-v1"
    async with fixture.client.ws_connect(fixture.server.make_url(STREAM_PATH), headers=headers) as ws:
        rfb = RfbSocket(ws, epoch=0)
        await rfb.handshake()
        key = struct.pack("!BBHI", 4, 1, 0, ord("x"))
        await rfb.send(key[:3])
        await fixture.rpc("display.control.acquire", control_params(view, 1, "acquire"))
        await rfb.send(key[3:], epoch=2)
        assert (await ws.receive(timeout=5)).type in {aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED}
        assert fixture.input_services["default"]._broker is None


@pytest.mark.parametrize("release_control", [False, True])
async def test_slow_input_delivery_does_not_starve_websocket_heartbeat(display_gateway, monkeypatch, release_control):
    fixture = display_gateway
    view, _ = await observe(fixture)
    human = (await fixture.rpc("display.control.acquire", control_params(view, 1, "acquire")))["result"]["control"]
    # Acquire a fresh ticket for the same holder after the control transition.
    request = {key: value for key, value in control_params(view, human["epoch"], "unused").items()
               if key not in {"epoch", "operationId"}}
    ticket = (await fixture.rpc("display.observe", request))["result"]
    headers = {"Authorization": "Bearer display-test-owner", "X-Flowly-Profile": view["profileId"],
               "X-Flowly-Host-Id": view["hostId"], "X-Flowly-Bot-Id": view["botId"],
               "X-Flowly-Screen-Generation": view["generation"],
               "X-Flowly-Screen-Ticket": ticket["ticket"], "X-Flowly-Screen-Input": "epoch-u64-v1"}
    entered, release = threading.Event(), threading.Event()
    deliveries = []
    original = InputServiceClient.request

    def stalled(client, method, **params):
        if method == "attach":
            return {"handle": "test-handle", "nextSequence": 0}
        if method == "send":
            deliveries.append(base64.b64decode(params["data"]))
            entered.set()
            release.wait(5)
            return {"nextSequence": params["sequence"] + 1}
        return original(client, method, **params)

    monkeypatch.setattr(InputServiceClient, "request", stalled)
    async with fixture.client.ws_connect(fixture.server.make_url(STREAM_PATH), headers=headers, autoping=False) as ws:
        rfb = RfbSocket(ws, epoch=human["epoch"])
        await rfb.handshake()
        try:
            await rfb.send(struct.pack("!BBHH", 5, 0, 20, 20))
            assert await asyncio.to_thread(entered.wait, 2)
            # Build a backlog while the prior delivery waits for its ACK.
            moves = [struct.pack("!BBHH", 5, 0, x, 20) for x in range(100)]
            key = struct.pack("!BBHI", 4, 1, 0, ord("a"))
            update = struct.pack("!BBHHHH", 3, 1, 0, 0, 64, 48)
            for move in moves:
                await rfb.send(move)
            await rfb.send(key + update)
            await ws.ping(b"still-responsive")
            pong = await ws.receive(timeout=1)
            assert pong.type == aiohttp.WSMsgType.PONG and pong.data == b"still-responsive"
            if release_control:
                result = await fixture.rpc("display.control.release", control_params(view, human["epoch"], "release"))
                assert result["result"]["control"]["control"] == "agent"
            release.set()
            await asyncio.wait_for(fixture.received_event.wait(), 2)
            assert fixture.received == [("default", update)]
            if release_control:
                # Permission is rechecked at consumption, not queue admission.
                assert len(deliveries) == 1
            else:
                async with asyncio.timeout(2):
                    while len(deliveries) < 2:
                        await asyncio.sleep(0.01)
                assert deliveries[1:] == [moves[0] + moves[-1] + key]
        finally:
            release.set()


class RfbSocket:
    def __init__(self, ws, epoch=None):
        self.ws = ws
        self.epoch = epoch
        self.buffer = bytearray()

    async def send(self, data, *, epoch=None):
        selected = self.epoch if epoch is None else epoch
        await self.ws.send_bytes((selected.to_bytes(8, "big") if selected is not None else b"") + data)

    async def read(self, length):
        while len(self.buffer) < length:
            message = await self.ws.receive(timeout=5)
            assert message.type == aiohttp.WSMsgType.BINARY
            self.buffer.extend(message.data)
        value = bytes(self.buffer[:length])
        del self.buffer[:length]
        return value

    async def handshake(self):
        assert await self.read(12) == b"RFB 003.008\n"
        await self.send(b"RFB 003.008\n")
        assert await self.read(2) == b"\x01\x01"
        await self.send(b"\x01")
        assert await self.read(4) == bytes(4)
        # Unshared requests must be converted to shared by the product gate.
        await self.send(b"\x00")
        init = await self.read(24)
        await self.read(int.from_bytes(init[20:24], "big"))
        return init


async def test_status_is_public_projection_without_private_endpoints(display_gateway):
    result = (await display_gateway.rpc("display.status"))["result"]
    assert result["phase"] == "running"
    assert result["generation"] == "generation-default"
    assert result["control"]["control"] == "agent"
    assert not {"socket", "cdpPort", "directory", "viewer"} & result.keys()
    assert "must-not-use" not in json.dumps(result)


async def test_status_phase_has_own_durable_revision_without_control_change(display_gateway):
    fixture = display_gateway
    first = (await fixture.rpc("display.status"))["result"]
    assert first["statusRevision"] == 1
    assert (await fixture.rpc("display.status"))["result"] == first
    session = _resolve(fixture.gateway, {})
    running = session.runtime.status()
    session.runtime.status = lambda: {**running, "phase": "failed", "error": "RUNTIME_FAILED"}
    failed = (await fixture.rpc("display.status"))["result"]
    assert failed["control"] == first["control"]
    assert failed["statusRevision"] == 2
    session._status_sequence = None  # A newly constructed gateway resumes the same sequence.
    assert (await fixture.rpc("display.status"))["result"] == failed
    session.runtime.status = lambda: running
    recovered = (await fixture.rpc("display.status"))["result"]
    assert recovered["control"] == first["control"]
    assert recovered["statusRevision"] == 3


@pytest.mark.parametrize("profile", ["default", "writer"])
async def test_authenticated_binary_stream_uses_only_selected_profile_and_drops_input(display_gateway, profile):
    observed, headers = await observe(display_gateway, profile)
    assert "?" not in observed["streamPath"]
    async with display_gateway.client.ws_connect(display_gateway.server.make_url(STREAM_PATH), headers=headers) as ws:
        await RfbSocket(ws).handshake()
        key = struct.pack("!BBHI", 4, 1, 0, ord("X"))
        pointer = struct.pack("!BBHH", 5, 1, 20, 30)
        update = struct.pack("!BBHHHH", 3, 1, 0, 0, 64, 48)
        await ws.send_bytes(key[:3])
        await ws.send_bytes(key[3:] + pointer + update)
        await asyncio.wait_for(display_gateway.received_event.wait(), 5)
        assert display_gateway.received == [(profile, update)]
    with pytest.raises(aiohttp.WSServerHandshakeError) as replay:
        await display_gateway.client.ws_connect(display_gateway.server.make_url(STREAM_PATH), headers=headers)
    assert replay.value.status == 403


async def test_ticket_is_not_sufficient_without_gateway_authentication(display_gateway):
    _, headers = await observe(display_gateway)
    headers.pop("Authorization")
    with pytest.raises(aiohttp.WSServerHandshakeError) as rejected:
        await display_gateway.client.ws_connect(display_gateway.server.make_url(STREAM_PATH), headers=headers)
    assert rejected.value.status == 401


@pytest.mark.parametrize("changes", [
    {"owner": "spoofed"}, {"viewerId": "spoofed"}, {"path": "/tmp/other"},
    {"expectedHostId": "other"}, {"expectedBotId": "recreated"},
])
async def test_observe_rejects_caller_identity_and_target_spoofing(display_gateway, changes):
    status = (await display_gateway.rpc("display.status"))["result"]
    result = await display_gateway.rpc("display.observe", {
        "profileId": "default", "expectedHostId": status["hostId"],
        "expectedBotId": status["botId"], "generation": status["generation"], **changes,
    })
    assert result["error"]["code"] in {"INVALID_PARAMS", "PROFILE_IDENTITY_CHANGED"}


async def test_observe_requires_pinned_target_and_secure_transport(display_gateway, monkeypatch):
    result = await display_gateway.rpc("display.observe", {"generation": "generation-default"})
    assert result["error"]["code"] == "PROFILE_IDENTITY_CHANGED"
    monkeypatch.setattr("flowly.gateway.display._protected_socket", lambda request: False)
    result = await display_gateway.rpc("display.status")
    assert result["error"]["code"] == "SECURE_TRANSPORT_REQUIRED"


async def test_human_control_revokes_viewing_without_releasing_ownership(display_gateway):
    _, headers = await observe(display_gateway)
    ws = await display_gateway.client.ws_connect(display_gateway.server.make_url(STREAM_PATH), headers=headers)
    await RfbSocket(ws).handshake()
    service = _resolve(display_gateway.gateway, {})
    human = service.runtime.authority.acquire("human", "generation-default", 1)
    closed = await ws.receive(timeout=5)
    assert closed.type in {aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED}
    assert service.runtime.authority.snapshot() == human
    response = await display_gateway.rpc("display.observe", {
        "profileId": "default", "expectedHostId": service.identity["hostId"],
        "expectedBotId": service.identity["botId"], "generation": "generation-default",
    })
    assert response["error"]["code"] == "HUMAN_HAS_CONTROL"
    await ws.close()


async def test_deleted_profile_identity_cannot_continue_streaming(display_gateway, monkeypatch):
    _, headers = await observe(display_gateway, "writer")
    ws = await display_gateway.client.ws_connect(display_gateway.server.make_url(STREAM_PATH), headers=headers)
    await RfbSocket(ws).handshake()
    path, _ = profiles.profile_identity("writer")
    metadata = profiles._profile_metadata(path)
    metadata["botId"] = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    profiles._atomic_write_json(path / "profile.json", metadata)
    assert (await ws.receive(timeout=5)).type in {aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED}
    await ws.close()


async def test_active_stream_limit_and_disconnect_cleanup(display_gateway):
    streams = []
    try:
        for _ in range(4):
            _, headers = await observe(display_gateway)
            ws = await display_gateway.client.ws_connect(display_gateway.server.make_url(STREAM_PATH), headers=headers)
            await RfbSocket(ws).handshake()
            streams.append(ws)
        _, headers = await observe(display_gateway)
        with pytest.raises(aiohttp.WSServerHandshakeError) as busy:
            await display_gateway.client.ws_connect(display_gateway.server.make_url(STREAM_PATH), headers=headers)
        assert busy.value.status == 429
    finally:
        for ws in streams:
            await ws.close()
    async with asyncio.timeout(5):
        while _resolve(display_gateway.gateway, {}).attaching:
            await asyncio.sleep(0.01)
    assert not _resolve(display_gateway.gateway, {}).streams


async def test_unsupported_host_is_explicit_and_does_not_start_runtime(display_gateway, monkeypatch):
    monkeypatch.setattr(ScreenRuntime, "capabilities", staticmethod(lambda: {
        "supported": False, "reason": "UNSUPPORTED_HOST", "missing": [],
    }))
    status = (await display_gateway.rpc("display.status"))["result"]
    assert status["phase"] == "unavailable"
    assert status["reason"] == "UNSUPPORTED_HOST"
    assert status["transports"] == []


async def test_supervisor_takeover_ack_follows_gateway_capture_cleanup(display_gateway):
    observed, headers = await observe(display_gateway)
    holder = await display_gateway.client.ws_connect(display_gateway.server.make_url(STREAM_PATH), headers=headers)
    await RfbSocket(holder).handshake()
    _, other_headers = await observe(display_gateway)
    other = await display_gateway.client.ws_connect(display_gateway.server.make_url(STREAM_PATH), headers=other_headers)
    await RfbSocket(other).handshake()
    session = _resolve(display_gateway.gateway, {})
    client = InputServiceClient(display_gateway.input_services["default"].path)
    state = await asyncio.to_thread(client.request, "acquire", generation=observed["generation"],
                                    viewer=observed["viewerId"], epoch=session.runtime.authority.snapshot()["epoch"])
    assert state["control"] == "human"
    assert (await other.receive(timeout=3)).type in {aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED}
    assert session.attaching == 1
    # The holder's view remains connected, while its input is still host-filtered.
    display_gateway.received.clear()
    display_gateway.received_event.clear()
    await holder.send_bytes(struct.pack("!BBHHHH", 3, 0, 0, 0, 10, 10))
    await asyncio.wait_for(display_gateway.received_event.wait(), 3)
    assert display_gateway.received
    await holder.close()
    await other.close()


async def test_public_terminal_close_revokes_capture_and_retry_cannot_release_new_holder(display_gateway):
    fixture = display_gateway
    view, headers = await observe(fixture)
    acquired = (await fixture.rpc("display.control.acquire", control_params(view, 1, "acquire")))["result"]["control"]
    close = control_params(view, acquired["epoch"], "terminal-close")
    async with fixture.client.ws_connect(fixture.server.make_url(STREAM_PATH), headers=headers) as screen:
        await RfbSocket(screen).handshake()
        # Consume close concurrently: server must finish this capture before ACK.
        close_frame = asyncio.create_task(screen.receive(timeout=5))
        closed = (await fixture.rpc("display.viewer.close", close))["result"]
        assert closed["control"]["control"] == "agent"
        assert (await close_frame).type in {aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED}
    resume = {key: value for key, value in close.items() if key not in {"epoch", "operationId"}}
    assert (await fixture.rpc("display.observe", resume))["error"]["code"] == "VIEWER_REVOKED"
    assert (await fixture.rpc("display.control.acquire", control_params(view, closed["control"]["epoch"], "late")))["error"]["code"] == "VIEWER_REVOKED"
    other, _ = await observe(fixture)
    human = (await fixture.rpc("display.control.acquire", control_params(other, closed["control"]["epoch"], "other")))["result"]["control"]
    # A new session object simulates gateway reconstruction against the same DB.
    session = _resolve(fixture.gateway, {"profileId": "default"})
    session._viewers = None
    assert (await fixture.rpc("display.viewer.close", close))["result"] == closed
    assert (await fixture.rpc("display.status"))["result"]["control"] == human
    assert (await fixture.rpc("display.viewer.close", {**close, "operationId": "different"}))["error"]["code"] == "OPERATION_CONFLICT"
