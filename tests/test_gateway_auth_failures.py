"""Failed sign-ins from other machines are capped per address.

A generated token is out of reach of guessing; the cap is for a token a
person chose (``--token``) and against a peer flooding credential checks.
Loopback is never counted (this machine's own callers, or a same-host proxy
that makes every client loopback).
"""

from __future__ import annotations

import aiohttp
import pytest

from flowly.gateway import server as server_module
from flowly.gateway.auth import AuthFailureLimiter, auth_peer_key
from flowly.gateway.server import GatewayServer


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_an_address_is_refused_after_the_limit_and_let_back_as_failures_age_out():
    clock = _Clock()
    limiter = AuthFailureLimiter(limit=3, window_seconds=60, clock=clock)
    peer = "203.0.113.9"
    for _ in range(2):
        assert limiter.record_failure(peer) is False
        clock.now += 1
    assert limiter.retry_after(peer) == 0
    assert limiter.record_failure(peer) is True
    # Refused until the first failure is a window old (1000 + 60).
    assert limiter.retry_after(peer) == pytest.approx(58)
    assert limiter.retry_after("198.51.100.1") == 0
    clock.now = 1060.5
    assert limiter.retry_after(peer) == 0


def test_loopback_is_never_counted_and_ipv6_counts_by_its_64():
    assert auth_peer_key("127.0.0.1") is None
    assert auth_peer_key("::1") is None
    assert auth_peer_key("::ffff:127.0.0.1") is None
    assert auth_peer_key(None) is None
    assert auth_peer_key("::ffff:203.0.113.9") == "203.0.113.9"
    assert auth_peer_key("2001:db8:1:2::5") == auth_peer_key("2001:db8:1:2:ffff::1") == "2001:db8:1:2::/64"
    assert auth_peer_key("2001:db8:1:3::5") != auth_peer_key("2001:db8:1:2::5")
    limiter = AuthFailureLimiter(limit=1)
    assert limiter.record_failure(None) is False
    assert limiter.retry_after(None) == 0


def test_the_limiter_keeps_a_bounded_number_of_addresses():
    limiter = AuthFailureLimiter(limit=5, max_peers=3)
    for n in range(10):
        limiter.record_failure(f"203.0.113.{n}")
    assert len(limiter._failures) == 3


async def _status(server: GatewayServer, headers: dict | None = None) -> int:
    async with aiohttp.ClientSession() as session:
        response = await session.get(f"http://127.0.0.1:{server.port}/api/cron/health", headers=headers or {})
        return response.status


@pytest.mark.asyncio
async def test_the_gateway_refuses_an_address_that_keeps_failing_even_with_the_right_token(monkeypatch):
    # Every test client is loopback; count it as another machine.
    monkeypatch.setattr(server_module, "auth_peer_key", lambda remote: "203.0.113.9")
    server = GatewayServer(host="127.0.0.1", port=0, auth_token="edge-secret",
                           require_loopback_auth=True, advertise_control=False)
    server._auth_failures = AuthFailureLimiter(limit=3)
    await server.start()
    try:
        for _ in range(3):
            assert await _status(server, {"X-Flowly-Token": "guess"}) == 401
        # Refused before its credential is looked at: a guess that is right counts for nothing now.
        assert await _status(server, {"X-Flowly-Token": "edge-secret"}) == 429
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_this_machine_is_never_refused():
    server = GatewayServer(host="127.0.0.1", port=0, auth_token="edge-secret",
                           require_loopback_auth=True, advertise_control=False)
    server._auth_failures = AuthFailureLimiter(limit=2)
    await server.start()
    try:
        for _ in range(5):
            assert await _status(server, {"X-Flowly-Token": "guess"}) == 401
        assert await _status(server, {"X-Flowly-Token": "edge-secret"}) != 401
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_a_failed_websocket_upgrade_counts(monkeypatch):
    monkeypatch.setattr(server_module, "auth_peer_key", lambda remote: "203.0.113.9")

    async def on_chat(*_args, **_kwargs):
        return None

    server = GatewayServer(host="127.0.0.1", port=0, auth_token="edge-secret",
                           require_loopback_auth=True, advertise_control=False, on_chat_message=on_chat)
    server._auth_failures = AuthFailureLimiter(limit=2)
    await server.start()
    try:
        async with aiohttp.ClientSession() as session:
            for _ in range(2):
                response = await session.get(f"http://127.0.0.1:{server.port}/ws?token=guess")
                assert response.status == 401
        assert await _status(server, {"X-Flowly-Token": "edge-secret"}) == 429
    finally:
        await server.stop()
