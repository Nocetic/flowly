"""gateway.requireAuth: a loopback gateway behind a same-host proxy keeps its token.

A cloud machine's edge relay (socat on the Fly machine) hands every remote
client to the gateway from 127.0.0.1. Loopback trust would then open the
gateway's REST routes to the internet; requireAuth enforces the token anyway.
"""

from __future__ import annotations

import aiohttp
import pytest

from flowly.cli.gateway_cmd import loopback_auth_required
from flowly.config.loader import convert_keys
from flowly.config.schema import Config
from flowly.gateway.server import GatewayServer


def test_require_auth_is_off_by_default_and_read_from_camel_case_config():
    assert Config().gateway.require_auth is False
    config = Config.model_validate(convert_keys({"gateway": {"requireAuth": True, "token": "t"}}))
    assert config.gateway.require_auth is True


@pytest.mark.parametrize("require_auth,local_runtime,expected", [
    (False, False, False), (True, False, True), (False, True, True), (True, True, True),
])
def test_loopback_auth_follows_config_or_local_runtime(require_auth, local_runtime, expected):
    config = Config.model_validate(convert_keys({"gateway": {"requireAuth": require_auth}}))
    assert loopback_auth_required(config, local_runtime) is expected


async def _cron_health(require_loopback_auth: bool, headers: dict | None = None) -> int:
    server = GatewayServer(host="127.0.0.1", port=0, auth_token="edge-secret",
                           require_loopback_auth=require_loopback_auth, advertise_control=False)
    await server.start()
    try:
        async with aiohttp.ClientSession() as session:
            response = await session.get(f"http://127.0.0.1:{server.port}/api/cron/health", headers=headers or {})
            return response.status
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_proxied_loopback_clients_need_the_token_when_required():
    assert await _cron_health(True) == 401
    assert await _cron_health(True, {"X-Flowly-Token": "wrong"}) == 401
    # Past the auth layer (the bare test server has no cron wired behind it).
    assert await _cron_health(True, {"X-Flowly-Token": "edge-secret"}) != 401


@pytest.mark.asyncio
async def test_plain_loopback_gateway_still_trusts_local_clients():
    # The reason requireAuth exists: without it a proxied client is "local".
    assert await _cron_health(False) != 401
