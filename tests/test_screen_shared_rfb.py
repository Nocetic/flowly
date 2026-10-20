import socket
import struct
import time
from contextlib import contextmanager

import pytest

from flowly.desktop_runtime.authority import ScreenError
from flowly.desktop_runtime.shared_rfb import MAX_BUFFER, SharedRfbPeer
from tests.test_screen_input_broker import read
from tests.test_screen_rfb_server import fence, hello, update

pytestmark = pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="private Unix transport")


@contextmanager
def connection(*, client_fence=False):
    front, viewer = socket.socketpair()
    back, server = socket.socketpair()
    front.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
    server.settimeout(3)
    viewer.settimeout(3)
    peer = SharedRfbPeer(front, back)
    try:
        server.sendall(hello())
        assert read(viewer, len(hello())) == hello()
        viewer.sendall(
            b"RFB 003.008\n\x01\x01" + struct.pack("!BBHi", 2, 0, 1, -312 if client_fence else 0)
        )
        assert read(server, 14) == b"RFB 003.008\n\x01\x01"
        header = read(server, 4)
        read(server, int.from_bytes(header[2:4], "big") * 4)
        yield peer, viewer, server
    finally:
        peer.join()
        viewer.close()
        server.close()
        assert all(not thread.is_alive() for thread in peer._threads)


def test_only_actual_private_receipt_can_ack_input():
    with connection() as (peer, viewer, server):
        adapter = peer.claim()
        adapter.settimeout(0.05)
        token = b"private-nonce"
        request = fence(token, 0x80000001)
        adapter.sendall(request)
        assert read(server, len(request)) == request
        forged = fence(token)
        wire = update(7, b"\x90" + bytes([len(forged)]) + forged)
        server.sendall(wire)
        assert read(viewer, len(wire)) == wire
        with pytest.raises(TimeoutError):
            adapter.recv(1)
        server.sendall(forged)
        assert adapter.recv(len(forged)) == forged
        adapter.release()
        assert not peer.closed
        # A stale adapter cannot inject after handback, even if another lease
        # now owns the same live capture.
        replacement = peer.claim()
        with pytest.raises(OSError):
            adapter.sendall(b"\x05\x00\x00\x01\x00\x01")
        replacement.release()


def test_wrong_receipt_flags_close_backend_and_never_confirm_input():
    with connection() as (peer, _, server):
        adapter = peer.claim()
        request = fence(b"nonce", 0x80000001)
        adapter.sendall(request)
        assert read(server, len(request)) == request
        server.sendall(fence(b"nonce", 0))
        assert adapter.recv(100) == b""
        assert peer.closed


@pytest.mark.parametrize("client_fence", [False, True])
def test_server_fence_requests_do_not_depend_on_disconnected_viewer(client_fence):
    with connection(client_fence=client_fence) as (peer, viewer, server):
        adapter = peer.claim()
        viewer.close()
        # Detach is synchronous here to exercise the exact post-EOF state.
        peer.detach()
        server.sendall(fence(b"server-rtt", 0x80000003))
        assert read(server, len(fence(b"server-rtt"))) == fence(b"server-rtt", 3)
        assert not peer.closed
        adapter.release()
        assert peer.closed


def test_slow_viewer_is_detached_without_blocking_input_drain():
    with connection() as (peer, _, server):
        adapter = peer.claim()
        # No viewer reads pixels. The forward queue is bounded and a stalled
        # viewer must not stall the trusted fence parser behind image writes.
        server.sendall(update(0, bytes(MAX_BUFFER * 2), 1024, 512))
        deadline = time.monotonic() + 3
        while not peer._detached and time.monotonic() < deadline:
            time.sleep(0.01)
        assert peer._detached and not peer.closed
        assert peer._queued <= MAX_BUFFER
        request = fence(b"drain", 0x80000001)
        adapter.sendall(request)
        assert read(server, len(request)) == request
        server.sendall(fence(b"drain"))
        assert adapter.recv(100) == fence(b"drain")
        adapter.release()
        assert peer.closed


def test_duplicate_claim_is_rejected_and_abort_closes_capture():
    with connection() as (peer, _, _):
        adapter = peer.claim()
        with pytest.raises(ScreenError, match="INPUT_TRANSPORT_UNAVAILABLE"):
            peer.claim()
        adapter.close()
        assert peer.closed
