"""A named bot's running screen is reached only through its own gateway, never selected."""

from unittest.mock import AsyncMock

import pytest

import flowly.profile as profiles
from flowly.profile_host import ProfileHost
from flowly.profile_host_contract import ProfileHostError, validate_profile_rpc


def grant(host_id="host", profile="writer", bot_id="bot"):
    return {"providerId": "host-desktop-browser", "hostId": host_id,
            "profileId": profile, "botId": bot_id, "screenId": "browser", "generation": "generation"}


def message(access=None):
    value = {"sessionKey": "desktop:screen-task", "message": "Continue this browser task"}
    return value if access is None else {**value, "browserAccess": access}


@pytest.mark.parametrize("method", ["chat.send", "chat.steer"])
@pytest.mark.parametrize("access", [
    grant(), "host-desktop-browser", {}, {"providerId": "local-browser", "registrationId": "lease"},
    {**grant(), "cdpUrl": "http://localhost:9222"}, {**grant(), "registrationId": "lease"},
])
def test_remote_messages_cannot_select_a_browser_on_the_bot_host(method, access):
    # A bot's running screen is its browser; no message may name one.
    with pytest.raises(ProfileHostError) as error:
        validate_profile_rpc(method, message(access))
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


@pytest.mark.parametrize("target", ["default", "writer"])
async def test_authenticated_profile_proxy_reaches_only_its_own_running_screen(screen_host, tmp_path, target, monkeypatch):
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
    from flowly.desktop_runtime.host_screen_run import bound_screen_run, host_screen_run_scope
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

    async def chat(*args):
        # Production enters this scope in the bound runtime's AgentLoop._process_message.
        async with host_screen_run_scope(bound_gateway, args[0], args[2]) as run:
            page = json.loads(await BrowserTabTool(bound_gateway).execute("get_page_text"))
            seen.extend([bound_screen_run().run_id, page])
            run.succeeded = True
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
            # The child is in-process here; a real one runs with the writer home,
            # where it checks the forwarded agent identity against its own.
            monkeypatch.setattr(profiles, "current_profile_name", lambda: "writer")
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

                selected = await send(grant(host.host_id, target, bot_id), "selected")
                assert selected["error"]["code"] == "REMOTE_HOST_ACCESS_DENIED"
                provider._dispatch.assert_not_awaited()
                accepted = await send(None, "original-run")
                assert accepted["result"]["runId"] == "original-run"
                await asyncio.wait_for(finished.wait(), 10)
                assert seen[0] == "original-run"
                assert seen[1]["text"] == "selected profile page"
                provider._dispatch.assert_awaited_once()
    finally:
        await host.shutdown()
        await outer_server.close()
        await child_server.close()
