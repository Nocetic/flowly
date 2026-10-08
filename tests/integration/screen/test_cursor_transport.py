"""Reproduce separate input/capture cursor rendering on real TigerVNC.

This is a diagnostic characterization, not a bypass of the product input gate.
The direct pointer write exists only on a disposable test runtime's Unix socket.
"""

import os
import socket
import struct

import pytest

from flowly.desktop_runtime.input_broker import ScreenInputBroker
from tests.integration.screen.test_xvnc_browser import recv_exact, running_screen

pytestmark = pytest.mark.skipif(
    os.environ.get("FLOWLY_SCREEN_LINUX_TEST") != "1",
    reason="requires isolated Linux screen runner",
)


def handshake(connection):
    assert recv_exact(connection, 12) == b"RFB 003.008\n"
    connection.sendall(b"RFB 003.008\n")
    assert recv_exact(connection, 2) == b"\x01\x01"
    connection.sendall(b"\x01")
    assert recv_exact(connection, 4) == bytes(4)
    connection.sendall(b"\x01")
    init = recv_exact(connection, 24)
    recv_exact(connection, int.from_bytes(init[20:24], "big"))
    connection.sendall(struct.pack("!BBHii", 2, 0, 2, -239, 0))


def capture(connection):
    connection.sendall(struct.pack("!BBHHHH", 3, 0, 600, 500, 32, 32))
    header = recv_exact(connection, 4)
    assert header[0] == 0
    shapes, pixels = [], bytearray()
    for _ in range(int.from_bytes(header[2:4], "big")):
        _, _, width, height, encoding = struct.unpack("!HHHHi", recv_exact(connection, 12))
        if encoding == -239:
            shapes.append((width, height))
            recv_exact(connection, width * height * 4 + ((width + 7) // 8) * height)
        else:
            assert encoding == 0
            pixels.extend(recv_exact(connection, width * height * 4))
    return shapes, {bytes(pixels[i : i + 3]) for i in range(0, len(pixels), 4)}


def test_separate_input_bakes_cursor_into_capture_but_shared_input_does_not(tmp_path):
    from playwright.sync_api import sync_playwright

    with running_screen(tmp_path / "cursor") as screen, sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp(screen["endpoint"])
        page = browser.contexts[0].pages[0]
        page.set_content("<body style='margin:0;background:rgb(35,123,180);height:100vh'></body>")
        page.bring_to_front()
        page.evaluate(
            "addEventListener('mousemove', e => window.lastPointer = [e.screenX,e.screenY])"
        )
        authority = screen["runtime"].authority
        generation = screen["state"]["generation"]
        human = authority.acquire("cursor-fixture", generation, authority.snapshot()["epoch"])
        broker = ScreenInputBroker(
            authority, "cursor-fixture", generation, human["epoch"], screen["socket"]
        )
        try:
            with socket.socket(socket.AF_UNIX) as viewer:
                viewer.settimeout(5)
                viewer.connect(str(screen["socket"]))
                handshake(viewer)
                broker.send(struct.pack("!BBHH", 5, 0, 600, 500))
                page.wait_for_function("window.lastPointer?.[0] === 600")
                # Wait for the browser's blank page to be painted, then ask for
                # a complete small patch rather than assuming a fixed sleep.
                page.evaluate(
                    "() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))"
                )
                separate_shapes, separate_colors = capture(viewer)
                assert (0, 0) in separate_shapes
                assert len(separate_colors) > 1, "separate capture includes server-rendered cursor"
                viewer.sendall(struct.pack("!BBHH", 5, 0, 601, 500))
                page.wait_for_function("window.lastPointer?.[0] === 601")
                shared_shapes, shared_colors = capture(viewer)
                assert any(width and height for width, height in shared_shapes)
                assert len(shared_colors) == 1, (
                    "same connection receives clean background and local cursor shape"
                )
        finally:
            broker.finish(release=True)


def test_shared_capture_keeps_local_cursor_and_drains_after_viewer_exit(tmp_path):
    from playwright.sync_api import sync_playwright

    from flowly.desktop_runtime.shared_rfb import SharedRfbPeer

    with running_screen(tmp_path / "shared-cursor") as screen, sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp(screen["endpoint"])
        page = browser.contexts[0].pages[0]
        page.set_content("<body style='margin:0;background:rgb(35,123,180);height:100vh'></body>")
        page.bring_to_front()
        page.evaluate("""() => {
            addEventListener('mousemove', e => window.lastPointer=[e.screenX,e.screenY]);
            addEventListener('keydown', e => window.shift=e.shiftKey);
            addEventListener('keyup', e => window.shift=e.shiftKey);
            addEventListener('mousedown', () => window.held=true);
            addEventListener('mouseup', () => window.held=false);
        }""")
        authority = screen["runtime"].authority
        generation = screen["state"]["generation"]
        human = authority.acquire("cursor-fixture", generation, authority.snapshot()["epoch"])
        frontend, viewer = socket.socketpair()
        backend = socket.socket(socket.AF_UNIX)
        backend.connect(str(screen["socket"]))
        shared = SharedRfbPeer(frontend, backend)
        broker = None
        try:
            viewer.settimeout(5)
            handshake(viewer)
            broker = ScreenInputBroker(
                authority,
                "cursor-fixture",
                generation,
                human["epoch"],
                screen["socket"],
                shared_connection=shared.claim(),
            )
            broker.send(struct.pack("!BBHH", 5, 0, 600, 500))
            page.wait_for_function("window.lastPointer?.[0]===600")
            page.evaluate(
                "() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))"
            )
            shapes, colors = capture(viewer)
            assert any(width and height for width, height in shapes)
            assert len(colors) == 1, "capture must not contain the lagging server-rendered cursor"
            broker.send(
                struct.pack("!BBHI", 4, 1, 0, 0xFFE1) + struct.pack("!BBHH", 5, 1, 600, 500)
            )
            page.wait_for_function("window.shift && window.held")
            # Simulate a gateway/viewer crash. The supervisor can still prove
            # release of held input through the SAME connection without the UI.
            viewer.close()
            result = broker.finish(release=True)
            assert result["control"] == "agent" and not result["inputDrainRequired"]
            page.wait_for_function("!window.shift && !window.held")
        finally:
            viewer.close()
            if broker is not None:
                broker.abandon()
            shared.join()
