"""Profile proxy grants stay pinned through validation and runtime startup."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import flowly.profile as profiles
from flowly.profile_host import ProfileHost
from flowly.profile_host_contract import ProfileHostError, validate_profile_rpc


def grant(host_id="host", profile="writer"):
    return {"providerId": "host-desktop-browser", "hostId": host_id,
            "profileId": profile, "screenId": "browser", "generation": "generation"}


def message(access):
    return {"sessionKey": "desktop:screen-task", "message": "Continue this browser task", "browserAccess": access}


@pytest.mark.parametrize("access", [
    "host-desktop-browser", {}, {"providerId": "local-browser", "registrationId": "lease"},
    {**grant(), "cdpUrl": "http://localhost:9222"},
    {**grant(), "registrationId": "lease"}, {**grant(), "screenId": "physical"},
    {**grant(), "generation": None}, {**grant(), "generation": "x" * 257},
    {**grant(), "hostId": "host\x7f"}, {**grant(), "profileId": " writer"},
])
def test_profile_contract_rejects_arbitrary_browser_access(access):
    with pytest.raises(ProfileHostError) as error:
        validate_profile_rpc("chat.send", message(access))
    assert error.value.code == "REMOTE_HOST_ACCESS_DENIED"


def test_profile_contract_copies_screen_grant_and_rejects_steering_rebind():
    original = message(grant())
    _, sanitized = validate_profile_rpc("chat.send", original)
    sanitized["browserAccess"]["generation"] = "changed"
    assert original["browserAccess"]["generation"] == "generation"
    with pytest.raises(ProfileHostError) as error:
        validate_profile_rpc("chat.steer", original)
    assert error.value.code == "REMOTE_HOST_ACCESS_DENIED"


@pytest.fixture
def screen_host(tmp_path, monkeypatch):
    default = tmp_path / ".flowly"
    monkeypatch.setattr(profiles, "_DEFAULT_HOME", default)
    monkeypatch.setattr(profiles, "_PROFILES_ROOT", default / "profiles")
    monkeypatch.setenv("FLOWLY_HOME", str(default))
    default.mkdir()
    profiles.create_profile("writer", local_runtime=True)
    host = ProfileHost()
    host._target_rpc = AsyncMock(return_value={"runId": "run"})
    return host, profiles.ensure_profile_bot_id("writer").bot_id


async def test_screen_profile_requires_pinned_identities_before_starting_runtime(screen_host):
    host, _ = screen_host
    with pytest.raises(ProfileHostError) as error:
        await host.rpc("writer", "chat.send", message(grant(host.host_id)))
    assert error.value.code == "INVALID_PARAMS"
    host._target_rpc.assert_not_awaited()


@pytest.mark.parametrize("mismatch", ["host", "profile", "bot", "expected-host"])
async def test_screen_profile_rejects_target_drift_before_starting_runtime(screen_host, mismatch):
    host, bot_id = screen_host
    access = grant(host.host_id)
    expected_host = host.host_id
    if mismatch == "host":
        access["hostId"] = "other"
    elif mismatch == "profile":
        access["profileId"] = "default"
    elif mismatch == "bot":
        bot_id = "deleted-and-recreated"
    else:
        expected_host = "other"
    with pytest.raises(ProfileHostError) as error:
        await host.rpc("writer", "chat.send", message(access),
                       expected_host_id=expected_host, expected_bot_id=bot_id)
    assert error.value.code == "PROFILE_IDENTITY_CHANGED"
    host._target_rpc.assert_not_awaited()


async def test_screen_profile_forwards_exact_grant_with_startup_identity_guard(screen_host):
    host, bot_id = screen_host
    access = grant(host.host_id)
    assert await host.rpc("writer", "chat.send", message(access),
                          expected_host_id=host.host_id, expected_bot_id=bot_id) == {"runId": "run"}
    forwarded = host._target_rpc.await_args
    assert forwarded.args[:2] == ("writer", "chat.send")
    assert forwarded.args[2]["browserAccess"] == access
    assert forwarded.kwargs == {"expected_bot_id": bot_id}


async def test_screen_profile_rechecks_recreated_identity_after_runtime_start(screen_host, monkeypatch):
    host, bot_id = screen_host
    # Exercise the real second identity check across the runtime-start await.
    del host._target_rpc
    runtime = SimpleNamespace(active_runs=set())

    async def start(_):
        monkeypatch.setattr("flowly.profile_host._public_profile", lambda name: {"botId": "replacement"})
        return runtime

    host._ensure_runtime = start
    host._rpc = AsyncMock()
    with pytest.raises(ProfileHostError) as error:
        await host.rpc("writer", "chat.send", message(grant(host.host_id)),
                       expected_host_id=host.host_id, expected_bot_id=bot_id)
    assert error.value.code == "PROFILE_IDENTITY_CHANGED"
    host._rpc.assert_not_awaited()


@pytest.mark.parametrize("target", ["default", "writer"])
async def test_authenticated_profile_proxy_reaches_only_bound_gateway_screen(screen_host, tmp_path, target, monkeypatch):
    """Real authenticated sockets and RPC dispatch; only browser I/O is faked.

    Linux integration separately proves this provider operates real Chromium.
    Runtime spawn is outside this test: attach a live gateway using the same
    transport/reader as an already-running managed profile.
    """
    import asyncio
    import json
    from unittest.mock import Mock

    import aiohttp
    from aiohttp.test_utils import TestServer

    from flowly.agent.tools.browser_tab import BrowserTabTool
    from flowly.desktop_runtime.authority import ScreenAuthority
    from flowly.desktop_runtime.browser_provider import HostBrowserProvider
    from flowly.desktop_runtime.context import SCREEN_RUN_BINDING
    from flowly.gateway.server import GatewayServer
    from flowly.profile_host import _Runtime

    owner = {"pid": 42, "start": "1", "boot": "test"}
    monkeypatch.setattr("flowly.desktop_runtime.handoff.process_identity", lambda _: owner)
    monkeypatch.setattr("flowly.desktop_runtime.handoff.is_alive", lambda _: True)
    gateway = GatewayServer(host="127.0.0.1", port=0, auth_token="owner-test-token",
                            require_loopback_auth=True, enable_profile_host=True)
    gateway.on_chat_message = AsyncMock(side_effect=AssertionError("must not run on primary"))
    host = gateway.profile_host
    bot_id = profiles.ensure_profile_bot_id(target).bot_id
    child = GatewayServer(host="127.0.0.1", port=0, auth_token="child-test-token", require_loopback_auth=True)
    bound_gateway = child if target == "writer" else gateway
    authority = ScreenAuthority(tmp_path / "screen")
    authority.start("generation")
    provider = HostBrowserProvider(Mock(directory=profiles.ensure_profile_bot_id(target).path / "desktop", authority=authority,
        status=lambda: {"phase": "running", "generation": "generation"}))
    provider._connect = AsyncMock()
    provider._dispatch = AsyncMock(return_value={"success": True, "text": "selected profile page"})
    bound_gateway._host_screen_browser = provider
    bound_gateway._host_screen_identity = {"hostId": host.host_id, "profileId": target, "screenId": "browser"}
    seen = []
    finished = asyncio.Event()

    async def chat(*_):
        seen.append(SCREEN_RUN_BINDING.get().run_id)
        seen.append(json.loads(await BrowserTabTool(bound_gateway).execute("get_page_text")))
        finished.set()
        return "done", {}

    bound_gateway.on_chat_message = chat
    outer_server, child_server = TestServer(gateway._create_app()), TestServer(child._create_app())
    await outer_server.start_server()
    await child_server.start_server()
    try:
        if target == "writer":
            session, ws = await host._open_runtime_transport(
                port=child_server.port, token="child-test-token",
                error_code="TEST_CONNECTION_FAILED", error_message="Test child unavailable")
            runtime = _Runtime(profile="writer", process=None, session=session, ws=ws,
                               instance_id="test-instance", owned=False)
            host._runtimes["writer"] = runtime
            runtime.reader_task = asyncio.create_task(host._read_runtime(runtime))
        async with aiohttp.ClientSession() as session:
            async with session.post(outer_server.make_url("/api/auth/ws-ticket"),
                                    headers={"Authorization": "Bearer owner-test-token"}) as response:
                ticket = (await response.json())["ticket"]
            async with session.ws_connect(outer_server.make_url("/ws?ticket=" + ticket)) as desktop:
                async def send(access, request_id):
                    await desktop.send_json({"type": "rpc", "id": request_id, "method": "profiles.rpc", "params": {
                        "name": target, "method": "chat.send", "expectedHostId": host.host_id,
                        "expectedBotId": bot_id,
                        "params": {**message(access), "idempotencyKey": request_id},
                    }})
                    async with asyncio.timeout(10):
                        while True:
                            frame = await desktop.receive_json()
                            if frame.get("type") == "rpc" and frame.get("id") == request_id:
                                return frame

                access = grant(host.host_id, target)
                stale = await send({**access, "generation": "old-generation"}, "stale")
                assert stale["error"]["code"] == "STALE_GENERATION"
                provider._dispatch.assert_not_awaited()
                accepted = await send(access, "original-run")
                assert accepted["result"]["runId"] == "original-run"
                await asyncio.wait_for(finished.wait(), 10)
                assert seen[0] == "original-run"
                assert seen[1]["text"] == "selected profile page"
                provider._dispatch.assert_awaited_once()
    finally:
        await host.shutdown()
        await outer_server.close()
        await child_server.close()
