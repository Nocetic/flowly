"""The gateway's own encryption: its key, one port for TLS and plain HTTP, the pin.

A gateway reached from other machines encrypts with a key of its own that the
apps pin (flowly/gateway/tls.py); this machine's own callers keep plain HTTP
on the same port (flowly/gateway/listener.py). These run a real gateway and
real handshakes: the wire, not the helpers.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import socket
import ssl
import stat

import aiohttp
import pytest
from aiohttp import web
from cryptography import x509
from cryptography.hazmat.primitives import serialization

from flowly.gateway import listener as listener_module
from flowly.gateway.listener import SniffingSite, is_loopback_peer
from flowly.gateway.remote_qr import connect_uri
from flowly.gateway.tls import ensure_identity, rotate, short_fingerprint, spki_pin


def pin_of_der(der: bytes) -> str:
    """What an app computes from the certificate the gateway shows it."""
    spki = x509.load_der_x509_certificate(der).public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    return "sha256/" + base64.b64encode(hashlib.sha256(spki).digest()).decode()


def no_ca() -> ssl.SSLContext:
    """A client that trusts no authority: it accepts the gateway by its pin alone."""
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


async def pin_shown(port: int) -> str:
    """The key a TLS handshake with the gateway shows."""
    _reader, writer = await asyncio.open_connection("127.0.0.1", port, ssl=no_ca())
    try:
        return pin_of_der(writer.get_extra_info("ssl_object").getpeercert(binary_form=True))
    finally:
        writer.close()


async def get_tls(port: int, path: str = "/health") -> tuple[int, dict, str]:
    pin = await pin_shown(port)
    async with aiohttp.ClientSession() as session:
        async with session.get(f"https://127.0.0.1:{port}{path}", ssl=no_ca()) as response:
            return response.status, await response.json(), pin


async def get_plain(port: int, path: str = "/health") -> int:
    async with aiohttp.ClientSession() as session:
        async with session.get(f"http://127.0.0.1:{port}{path}") as response:
            return response.status


# ── The key ──────────────────────────────────────────────────────────


def test_its_key_is_made_once_owner_only_and_keeps_its_pin(tmp_path):
    directory = tmp_path / "tls"
    first = ensure_identity(["203.0.113.7"], directory)
    assert first.pin.startswith("sha256/") and len(base64.b64decode(first.pin[7:])) == 32
    for path in (first.key_path, first.cert_path):
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(directory).st_mode) == 0o700
    assert ensure_identity([], directory).pin == first.pin
    assert spki_pin(first.cert_path.read_bytes()) == first.pin
    assert first.fingerprint == short_fingerprint(first.pin)
    assert len(first.fingerprint.split(" ")) == 4


def test_a_lost_certificate_is_made_again_on_the_same_key_so_apps_stay_paired(tmp_path):
    directory = tmp_path / "tls"
    first = ensure_identity([], directory)
    first.cert_path.unlink()
    again = ensure_identity([], directory)
    assert again.pin == first.pin


def test_a_new_key_is_a_new_pin(tmp_path):
    directory = tmp_path / "tls"
    first = ensure_identity([], directory)
    assert rotate([], directory).pin != first.pin


def test_the_pairing_code_carries_the_pin_and_leaves_old_apps_on_plain(tmp_path):
    pin = ensure_identity([], tmp_path / "tls").pin
    uri = connect_uri("203.0.113.7", 18790, "secret", pin=pin)
    assert uri.startswith("flowly://remote?") and "tls=0" in uri
    assert "pin=sha256%2F" in uri
    assert "pin=" not in connect_uri("203.0.113.7", 18790, "secret")


def test_a_loopback_peer_is_told_from_another_machine():
    assert is_loopback_peer(("127.0.0.1", 5000))
    assert is_loopback_peer(("::1", 5000, 0, 0))
    assert is_loopback_peer(("::ffff:127.0.0.1", 5000, 0, 0))
    assert not is_loopback_peer(("203.0.113.7", 5000))
    assert not is_loopback_peer(("::ffff:203.0.113.7", 5000, 0, 0))
    assert not is_loopback_peer(())


# ── A running gateway ────────────────────────────────────────────────


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path / "home"))
    return tmp_path / "home"


@pytest.mark.asyncio
async def test_a_gateway_with_its_key_answers_tls_with_that_key_and_plain_http_to_this_machine(home):
    from flowly.gateway.server import GatewayServer

    identity = ensure_identity(["127.0.0.1"], home / "gateway" / "tls")
    server = GatewayServer(host="127.0.0.1", port=0, tls_identity=identity)
    await server.start()
    try:
        status, payload, pin = await get_tls(server.port)
        assert status == 200 and payload["tls"] is True
        assert pin == identity.pin  # the key the apps pin is the key it answers with
        # This machine's own callers (profile hosts, MCP tools, cron, the TUI) keep plain HTTP.
        assert await get_plain(server.port) == 200
        assert server._tls_info() == {"tls": True, "pin": identity.pin, "fingerprint": identity.fingerprint,
                                      "plainRemote": True}
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_without_its_key_the_gateway_is_plain_http_as_before(home):
    from flowly.gateway.server import GatewayServer

    server = GatewayServer(host="127.0.0.1", port=0)
    await server.start()
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"http://127.0.0.1:{server.port}/health") as response:
                assert (await response.json())["tls"] is False
        with pytest.raises((aiohttp.ClientError, ssl.SSLError)):
            await get_tls(server.port)
        assert server._tls_info() == {"tls": False}
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_another_machine_without_encryption_is_refused_once_plain_remote_is_off(home, monkeypatch):
    from flowly.gateway.server import GatewayServer

    # Every peer looks like another machine.
    monkeypatch.setattr(listener_module, "is_loopback_peer", lambda address: False)
    identity = ensure_identity([], home / "gateway" / "tls")
    server = GatewayServer(host="127.0.0.1", port=0, tls_identity=identity, allow_plain_remote=False)
    await server.start()
    try:
        with pytest.raises(aiohttp.ClientError):
            await get_plain(server.port)
        status, _payload, _pin = await get_tls(server.port)
        assert status == 200
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_another_machine_without_encryption_still_connects_while_plain_remote_is_on(home, monkeypatch):
    from flowly.gateway.server import GatewayServer

    monkeypatch.setattr(listener_module, "is_loopback_peer", lambda address: False)
    identity = ensure_identity([], home / "gateway" / "tls")
    server = GatewayServer(host="127.0.0.1", port=0, tls_identity=identity, allow_plain_remote=True)
    await server.start()
    try:
        assert await get_plain(server.port) == 200
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_the_websocket_runs_over_tls_too(home):
    from flowly.gateway.server import GatewayServer

    identity = ensure_identity([], home / "gateway" / "tls")
    server = GatewayServer(host="127.0.0.1", port=0, tls_identity=identity, on_chat_message=lambda *a: None)
    await server.start()
    try:
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(f"wss://127.0.0.1:{server.port}/ws?clientId=t1", ssl=no_ca()) as ws:
                await ws.send_json({"type": "rpc", "id": "1", "method": "gateway.tls", "params": {}})
                while True:
                    frame = await asyncio.wait_for(ws.receive_json(), 5)
                    if frame.get("type") == "rpc" and frame.get("id") == "1":
                        break
        assert frame["result"]["pin"] == identity.pin
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_a_connection_that_says_nothing_is_closed(tmp_path):
    app = web.Application()
    runner = web.AppRunner(app)
    await runner.setup()
    identity = ensure_identity([], tmp_path / "tls")
    site = SniffingSite(runner, "127.0.0.1", 0, ssl_context=identity.server_context(),
                        allow_plain_remote=True, first_byte_timeout=0.2)
    await site.start()
    try:
        port = site._server.sockets[0].getsockname()[1]
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        assert await asyncio.wait_for(reader.read(1), 3) == b""  # closed by the gateway
        writer.close()
    finally:
        await runner.cleanup()
