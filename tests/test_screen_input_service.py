import base64
import json
import socket
import struct
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from flowly.desktop_runtime.authority import ScreenAuthority, ScreenError
from flowly.desktop_runtime.input_service import (
    MAX_PACKET,
    InputService,
    InputServiceClient,
    _receive,
    _send,
)
from flowly.desktop_runtime.observers import ObserverRegistry
from tests.test_screen_input_broker import KEY, Peer

pytestmark = pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="private Unix transport")
IDENTITY = dict(generation="generation", viewer="viewer", epoch=2)


@pytest.fixture
def service(tmp_path):
    authority = ScreenAuthority(tmp_path / "authority")
    authority.start("generation")
    authority.acquire("viewer", "generation", 1)
    with tempfile.TemporaryDirectory(prefix="rfb-service-") as directory:
        path = Path(directory)
        peer = Peer(path / "rfb.sock")
        observers = ObserverRegistry(authority, "generation", path)
        server = InputService(authority, "generation", path, observers=observers)
        client = InputServiceClient(server.path)
        yield authority, peer, server, client
        server.close()
        observers.close()
        peer.close()


@pytest.mark.parametrize("method", ["control.release", "control.reclaim"])
def test_public_control_drains_pressed_keys_before_handback_or_reclaim(service, method):
    authority, peer, _, client = service
    opened = client.request("open", **IDENTITY)
    client.request("send", **IDENTITY, handle=opened["handle"], sequence=0, data=base64.b64encode(KEY).decode())
    params = {**IDENTITY, "viewer": "replacement" if method.endswith("reclaim") else "viewer", "operationId": "op"}
    result = client.request(method, **params)
    assert result["control"] == ("human" if method.endswith("reclaim") else "agent")
    assert not result["inputDrainRequired"]
    assert len(peer.events) == 2 and peer.events[1][1] == 0
    assert client.request(method, **params) == result
    assert authority.snapshot() == result


def test_pending_acquire_requires_fresh_operation_after_original_action_finishes(service):
    authority, _, _, client = service
    authority.release("viewer", "generation", 2)
    action = authority.begin_action("origin-run", "action", "generation")
    params = dict(generation="generation", viewer="viewer", epoch=3, operationId="first")
    pending = client.request("control.acquire", **params)
    assert pending["control"] == "human-pending"
    authority.end_action(action)
    assert client.request("control.acquire", **params) == pending
    confirmed = client.request("control.acquire", **{**params, "epoch": pending["epoch"], "operationId": "confirm"})
    assert confirmed["control"] == "human" and confirmed["runId"] == "origin-run"


def test_replaced_attachment_cannot_send_even_with_current_control_epoch(service):
    authority, peer, server, client = service
    connections = []

    def attach(stream):
        connection = socket.socket(socket.AF_UNIX)
        connections.append(connection)
        connection.connect(str(server.observers.path))
        _send(connection, dict(generation="generation", viewer="viewer", stream=stream))
        assert _receive(connection) == {"ok": True}
        return connection

    try:
        first = attach("first")
        opened = client.request("attach", **IDENTITY, stream="first")
        client.request("send", **IDENTITY, handle=opened["handle"], sequence=0, data=base64.b64encode(KEY).decode())
        attach("second")
        assert first.recv(1) == b"R"
        with pytest.raises(ScreenError, match="VIEWER_REVOKED"):
            client.request("send", **IDENTITY, handle=opened["handle"], sequence=1, data=base64.b64encode(KEY).decode())
        with pytest.raises(ScreenError, match="VIEWER_REVOKED"):
            client.request("attach", **IDENTITY, stream="first")
        client.request("control.release", **IDENTITY, operationId="handback")
        assert peer.events == [KEY, struct.pack("!BBHI", 4, 0, 0, 0xFFE1)]
        assert authority.snapshot()["control"] == "agent"
    finally:
        for connection in connections:
            connection.close()


def test_worker_reconnect_keeps_one_broker_and_deduplicates_the_last_input(service):
    authority, peer, server, client = service
    opened = client.request("open", **IDENTITY)
    payload = base64.b64encode(KEY).decode()
    assert client.request("send", **IDENTITY, handle=opened["handle"], sequence=0, data=payload) == {"nextSequence": 1}
    replacement = InputServiceClient(server.path)
    resumed = replacement.request("open", **IDENTITY)
    assert resumed["handle"] == opened["handle"] and resumed["nextSequence"] == 1
    replacement.request("send", **IDENTITY, handle=opened["handle"], sequence=0, data=payload)
    assert peer.events == [KEY]
    with pytest.raises(ScreenError, match="STALE_INPUT_SEQUENCE"):
        replacement.request("send", **IDENTITY, handle=opened["handle"], sequence=0,
                            data=base64.b64encode(struct.pack("!BBHI", 4, 1, 0, ord("x"))).decode())
    result = replacement.request("finish", **IDENTITY, handle=opened["handle"], release=True)
    assert result["control"] == "agent" and not authority.snapshot()["inputDrainRequired"]


def test_a_lost_command_response_is_not_permission_to_repeat_input(service):
    _, peer, server, client = service
    opened = client.request("open", **IDENTITY)
    request = dict(method="send", **IDENTITY, handle=opened["handle"], sequence=0,
                   data=base64.b64encode(KEY).decode())
    encoded = json.dumps(request).encode()
    with socket.socket(socket.AF_UNIX) as connection:
        connection.connect(str(server.path))
        connection.sendall(struct.pack("!I", len(encoded)) + encoded)
        # Abandon the response, simulating a gateway worker exit.
    deadline = time.monotonic() + 2
    while peer.fences < 2 and time.monotonic() < deadline:
        time.sleep(0.005)
    assert peer.fences == 2
    response = InputServiceClient(server.path).request(**request)
    assert response == {"nextSequence": 1} and peer.events == [KEY]


def test_old_finish_retry_cannot_release_a_new_holder(service):
    authority, _, _, client = service
    opened = client.request("open", **IDENTITY)
    released = client.request("finish", **IDENTITY, handle=opened["handle"], release=True)
    human = authority.acquire("next-viewer", "generation", released["epoch"])
    assert client.request("finish", **IDENTITY, handle=opened["handle"], release=True) == released
    assert authority.snapshot() == human
    with pytest.raises(ScreenError, match="VIEWER_MISMATCH"):
        client.request("finish", **IDENTITY, handle=opened["handle"], release=False)


def test_stopping_the_service_never_releases_human_control(service):
    authority, _, server, client = service
    opened = client.request("open", **IDENTITY)
    client.request("send", **IDENTITY, handle=opened["handle"], sequence=0, data=base64.b64encode(KEY).decode())
    server.close()
    assert not server.is_alive
    state = ScreenAuthority(authority.directory).snapshot()
    assert state["control"] == "human" and state["inputDrainRequired"]
    with pytest.raises(ScreenError, match="INPUT_SERVICE_UNAVAILABLE"):
        client.request("open", **IDENTITY)


@pytest.mark.parametrize("change,code", [
    ({"viewer": "other"}, "VIEWER_MISMATCH"), ({"generation": "stale"}, "STALE_GENERATION"),
    ({"epoch": True}, "INVALID_PARAMS"), ({"epoch": 1}, "VIEWER_MISMATCH"),
    ({"path": "/tmp/another-profile/rfb.sock"}, "INVALID_PARAMS"),
    ({"method": "release"}, "INVALID_PARAMS"),
])
def test_profile_target_and_internal_method_cannot_be_substituted(service, change, code):
    _, _, _, client = service
    with pytest.raises(ScreenError, match=code):
        client.request(**{**IDENTITY, "method": "open", **change})


def test_unacknowledged_input_blocks_competing_commands_without_unbounded_queue(service):
    _, peer, _, client = service
    opened = client.request("open", **IDENTITY)
    peer.hold_at = 2
    with ThreadPoolExecutor(max_workers=1) as workers:
        pending = workers.submit(client.request, "send", **IDENTITY, handle=opened["handle"], sequence=0,
                                 data=base64.b64encode(KEY).decode())
        assert peer.reached.wait(2)
        with pytest.raises(ScreenError, match="SCREEN_BUSY"):
            client.request("finish", **IDENTITY, handle=opened["handle"], release=True)
        peer.proceed.set()
        assert pending.result(2) == {"nextSequence": 1}


def test_oversized_packet_is_rejected_before_body_and_socket_is_private(service):
    _, _, server, _ = service
    assert server.path.stat().st_mode & 0o777 == 0o600
    with socket.socket(socket.AF_UNIX) as connection:
        connection.settimeout(1)
        connection.connect(str(server.path))
        connection.sendall(struct.pack("!I", MAX_PACKET + 1))
        assert _receive(connection) == {"ok": False, "code": "INVALID_PARAMS"}


def test_packet_deadline_is_total_not_reset_by_slow_bytes():
    left, right = socket.socketpair()
    stopped = threading.Event()

    def trickle():
        try:
            right.sendall(struct.pack("!I", 100))
            while not stopped.wait(0.01):
                right.sendall(b" ")
        except OSError:
            pass

    thread = threading.Thread(target=trickle)
    thread.start()
    try:
        with pytest.raises(TimeoutError):
            _receive(left, timeout=0.05)
    finally:
        stopped.set()
        left.close()
        right.close()
        thread.join(1)
    assert not thread.is_alive()


def test_service_caps_incomplete_connections_and_recovers_capacity(service):
    _, _, server, client = service
    connections = [socket.socket(socket.AF_UNIX) for _ in range(4)]
    try:
        for connection in connections:
            connection.connect(str(server.path))
        deadline = time.monotonic() + 1
        while len(server._connections) < 4 and time.monotonic() < deadline:
            time.sleep(0.005)
        assert len(server._connections) == 4
        with pytest.raises(ScreenError, match="INPUT_SERVICE_UNAVAILABLE"):
            client.request("open", **IDENTITY)
    finally:
        for connection in connections:
            connection.close()
    deadline = time.monotonic() + 1
    while server._connections and time.monotonic() < deadline:
        time.sleep(0.005)
    opened = client.request("open", **IDENTITY)
    assert opened["nextSequence"] == 0
