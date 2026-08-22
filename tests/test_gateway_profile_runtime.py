from __future__ import annotations

import aiohttp
import pytest

from flowly.gateway.server import GatewayServer


@pytest.mark.asyncio
async def test_ephemeral_loopback_runtime_requires_its_token() -> None:
    server = GatewayServer(
        host="127.0.0.1",
        port=0,
        auth_token="profile-secret",
        require_loopback_auth=True,
        advertise_control=False,
    )
    await server.start()
    try:
        assert 1 <= server.port <= 65535
        origin = f"http://127.0.0.1:{server.port}"
        async with aiohttp.ClientSession() as session:
            health = await session.get(f"{origin}/health")
            assert health.status == 200
            assert (await health.json())["auth_required"] is True

            denied = await session.post(f"{origin}/api/auth/ws-ticket")
            assert denied.status == 401

            ticket = await session.post(
                f"{origin}/api/auth/ws-ticket",
                headers={"X-Flowly-Token": "profile-secret"},
            )
            assert ticket.status == 200
            payload = await ticket.json()
            assert isinstance(payload.get("ticket"), str)
            assert payload["ticket"]
    finally:
        await server.stop()
