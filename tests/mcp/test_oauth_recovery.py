"""Recovery against real local HTTP endpoints, including separate clients/processes."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import sys
import threading
import time
from urllib.parse import parse_qs, urlparse

import httpx2
import pytest
from aiohttp import web
from mcp.shared.auth import AuthorizationCodeResult, OAuthClientInformationFull, OAuthToken

from flowly.mcp.oauth import FlowlyTokenStorage, build_oauth_provider, clear_tokens
from flowly.mcp.oauth_provider import OAuthRecoveryError
from flowly.mcp.oauth_state import recovery_lease


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
async def authority():
    """HTTP auth service; a refresh token rotates once and old tokens then fail."""
    state = {
        "access": "valid-0", "refresh": "refresh-0", "refreshes": 0,
        "seen": [], "delay": 0, "status": 200, "requests_active": 0,
        "peak": 0, "resource_status": 200, "discovery": 0,
        "token_started": asyncio.Event(), "hold_token": None, "token_body": None,
        "resource_headers": {},
    }

    async def resource(request):
        bearer = request.headers.get("Authorization")
        state["seen"].append(bearer)
        if bearer != f"Bearer {state['access']}":
            return web.Response(status=401, headers={"WWW-Authenticate": "Bearer"})
        state["requests_active"] += 1
        state["peak"] = max(state["peak"], state["requests_active"])
        try:
            await asyncio.sleep(state["delay"])
            return web.json_response(
                {"ok": True}, status=state["resource_status"], headers=state["resource_headers"],
            )
        finally:
            state["requests_active"] -= 1

    async def token(request):
        data = await request.post()
        assert "Authorization" not in request.headers
        state["refreshes"] += 1
        state["token_started"].set()
        if state["hold_token"] is not None:
            await state["hold_token"].wait()
        if state["status"] != 200:
            return web.json_response({"error": "SECRET-must-not-appear-in-errors"}, status=state["status"])
        if state["token_body"] is not None:
            return web.json_response(state["token_body"])
        if data.get("grant_type") == "authorization_code":
            assert data["code"] == "test-code"
            assert len(data["code_verifier"]) >= 43
        elif data.get("refresh_token") != state["refresh"]:
            return web.json_response({"error": "invalid_grant"}, status=400)
        state["access"] = f"valid-{state['refreshes']}"
        state["refresh"] = f"refresh-{state['refreshes']}"
        return web.json_response({
            "access_token": state["access"], "refresh_token": state["refresh"],
            "token_type": "Bearer", "expires_in": 3600,
        })

    async def protected(request):
        state["discovery"] += 1
        return web.json_response({
            "resource": state["url"], "authorization_servers": [state["base"]],
        })

    async def metadata(request):
        state["discovery"] += 1
        return web.json_response({
            "issuer": state["base"], "authorization_endpoint": state["base"] + "/authorize",
            "token_endpoint": state["base"] + "/token",
            "registration_endpoint": state["base"] + "/register",
            "code_challenge_methods_supported": ["S256"],
        })

    async def register(request):
        return web.json_response({"client_id": "client", **await request.json()}, status=201)

    async def stream(request):
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)
        await response.write(b"data: ready\n\n")
        try:
            await asyncio.wait_for(state["stream_close"].wait(), 5)
        except TimeoutError:
            pass
        return response

    state["stream_close"] = asyncio.Event()
    app = web.Application()
    app.router.add_route("*", "/mcp", resource)
    app.router.add_get("/stream", stream)
    app.router.add_post("/token", token)
    app.router.add_post("/register", register)
    app.router.add_get("/.well-known/oauth-protected-resource/mcp", protected)
    app.router.add_get("/.well-known/oauth-protected-resource", protected)
    app.router.add_get("/.well-known/oauth-authorization-server", metadata)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    state["base"] = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
    state["url"] = state["base"] + "/mcp"
    try:
        yield state
    finally:
        state["stream_close"].set()
        if state["hold_token"] is not None:
            state["hold_token"].set()
        await runner.cleanup()


async def seed(state, *, access="expired", expires_in=3600):
    storage = FlowlyTokenStorage("remote", state["url"])
    await storage.set_client_info(OAuthClientInformationFull(
        client_id="client", issuer=state["base"],
        redirect_uris=["http://127.0.0.1:8765/callback"],
    ))
    await storage.set_tokens(OAuthToken(
        access_token=access, refresh_token=state["refresh"], token_type="Bearer",
        expires_in=expires_in,
    ))
    return storage


def client(state):
    return httpx2.AsyncClient(auth=build_oauth_provider("remote", state["url"], interactive=False))


async def test_401_concurrent_calls_share_one_refresh_without_serializing_tools(authority):
    await seed(authority)
    authority["delay"] = 0.05
    async with client(authority) as connection:
        responses = await asyncio.gather(*(connection.post(authority["url"]) for _ in range(12)))
    assert all(response.status_code == 200 for response in responses)
    assert authority["refreshes"] == 1
    assert authority["peak"] > 1


async def test_separate_providers_coordinate_rotating_refresh_token(authority):
    await seed(authority)
    async with client(authority) as first, client(authority) as second:
        responses = await asyncio.gather(first.post(authority["url"]), second.post(authority["url"]))
    assert [response.status_code for response in responses] == [200, 200]
    assert authority["refreshes"] == 1


async def test_unchanged_access_token_still_coalesces_401_recovery(authority):
    await seed(authority)
    authority["token_body"] = {
        "access_token": "expired", "token_type": "Bearer", "expires_in": 3600,
    }
    async with client(authority) as connection:
        results = await asyncio.gather(
            *(connection.post(authority["url"]) for _ in range(8)), return_exceptions=True,
        )
    assert all(isinstance(result, OAuthRecoveryError) for result in results)
    assert authority["refreshes"] == 1


async def test_running_provider_observes_external_login_and_logout(authority):
    storage = await seed(authority, access=authority["access"])
    async with client(authority) as connection:
        assert (await connection.post(authority["url"])).status_code == 200
        authority["access"] = "external-login"
        await storage.set_tokens(OAuthToken(access_token="external-login", token_type="Bearer"))
        assert (await connection.post(authority["url"])).status_code == 200
        assert authority["seen"][-1] == "Bearer external-login"
        clear_tokens("remote")
        with pytest.raises(OAuthRecoveryError, match="needs interactive OAuth"):
            await connection.post(authority["url"])
    assert authority["refreshes"] == 0
    assert len(authority["seen"]) == 2


async def test_expiry_is_persisted_and_does_not_restart_with_provider(authority):
    storage = await seed(authority, expires_in=0)
    expiry = storage.snapshot()["expires_at"]
    async with client(authority) as connection:
        assert (await connection.post(authority["url"])).status_code == 200
    assert expiry <= time.time()
    assert authority["seen"] == ["Bearer valid-1"]
    assert authority["refreshes"] == 1
    discovery = authority["discovery"]
    await storage.update({"expires_at": 1})
    async with client(authority) as connection:
        assert (await connection.post(authority["url"])).status_code == 200
    assert authority["refreshes"] == 2
    assert authority["discovery"] == discovery  # AS metadata is durable too.


@pytest.mark.parametrize("status", [404, 403, 500])
async def test_non_auth_resource_errors_do_not_refresh_or_clear_tokens(authority, status):
    storage = await seed(authority, access=authority["access"])
    before = storage.snapshot()
    authority["resource_status"] = status
    async with client(authority) as connection:
        assert (await connection.post(authority["url"])).status_code == status
    assert authority["refreshes"] == 0
    assert storage.snapshot() == before


@pytest.mark.parametrize("status", [400, 401, 429, 503])
async def test_failed_refresh_is_bounded_preserves_store_and_redacts_response(authority, status):
    storage = await seed(authority)
    authority["status"] = status
    async with client(authority) as first, client(authority) as second:
        results = await asyncio.gather(
            first.post(authority["url"]), second.post(authority["url"]), return_exceptions=True,
        )
    assert all(isinstance(result, OAuthRecoveryError) for result in results)
    assert "SECRET" not in str(results)
    assert authority["refreshes"] == 1
    assert (await storage.get_tokens()).refresh_token == "refresh-0"


@pytest.mark.parametrize("logout", [True, False])
async def test_inflight_refresh_cannot_overwrite_new_login_or_resurrect_logout(authority, logout):
    storage = await seed(authority)
    authority["hold_token"] = asyncio.Event()
    async with client(authority) as connection:
        pending = asyncio.create_task(connection.post(authority["url"]))
        await asyncio.wait_for(authority["token_started"].wait(), 2)
        if logout:
            clear_tokens("remote")
        else:
            await storage.set_tokens(OAuthToken(access_token="new-login", token_type="Bearer"))
        authority["hold_token"].set()
        # The local fixture rotates its access token; a concurrent login is
        # intentionally rejected, but its durable credentials must still win.
        with pytest.raises(OAuthRecoveryError):
            await pending
    token = await storage.get_tokens()
    assert token is None if logout else token.access_token == "new-login"


async def test_cancelled_recovery_and_lease_wait_release_locks(authority):
    storage = await seed(authority)
    authority["hold_token"] = asyncio.Event()
    async with client(authority) as first, client(authority) as second:
        pending = asyncio.create_task(first.post(authority["url"]))
        await asyncio.wait_for(authority["token_started"].wait(), 2)
        waiting = asyncio.create_task(second.post(authority["url"]))
        await asyncio.sleep(0.05)
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        async with recovery_lease(storage.recovery_path, timeout=0.2):
            pass


async def test_distinct_resource_binding_never_sends_old_credentials(authority):
    await seed(authority, access=authority["access"])
    provider = build_oauth_provider("remote", authority["base"] + "/other", interactive=False)
    async with httpx2.AsyncClient(auth=provider) as connection:
        with pytest.raises(OAuthRecoveryError):
            await connection.get(authority["base"] + "/other")
        assert (await connection.post(authority["url"])).status_code == 401
    assert authority["seen"] == [None]
    assert authority["refreshes"] == 0


async def test_successful_sse_is_streamed_not_buffered_or_locked(authority):
    url = authority["base"] + "/stream"
    storage = FlowlyTokenStorage("stream", url)
    await storage.set_tokens(OAuthToken(access_token="stream-token", token_type="Bearer"))
    provider = build_oauth_provider("stream", url, interactive=False)
    async with httpx2.AsyncClient(auth=provider) as connection:
        async with asyncio.timeout(1):
            async with connection.stream("GET", url) as response:
                assert await anext(response.aiter_lines()) == "data: ready"
                async with recovery_lease(storage.recovery_path, timeout=0.1):
                    pass


async def test_full_sdk_authorization_uses_pkce_and_persists_discovery(authority):
    provider = build_oauth_provider("remote", authority["url"], interactive=True)
    redirected = {}

    async def redirect(url):
        redirected.update(parse_qs(urlparse(url).query))

    async def callback():
        return AuthorizationCodeResult(code="test-code", state=redirected["state"][0])

    provider.context.redirect_handler = redirect
    provider.context.callback_handler = callback
    async with httpx2.AsyncClient(auth=provider) as connection:
        assert (await connection.post(authority["url"])).status_code == 200
    assert redirected["code_challenge_method"] == ["S256"]
    assert "code_challenge" in redirected
    snapshot = provider.storage.snapshot()
    assert snapshot["oauth_metadata"]["token_endpoint"] == authority["base"] + "/token"
    assert snapshot["client_info"]["issuer"] == authority["base"]


async def test_separate_processes_coordinate_refresh(authority):
    await seed(authority, expires_in=0)
    script = """
import asyncio, sys
import httpx2
from flowly.mcp.oauth import build_oauth_provider
async def main():
    provider = build_oauth_provider('remote', sys.argv[1], interactive=False)
    async with httpx2.AsyncClient(auth=provider) as client:
        result = await client.post(sys.argv[1])
        assert result.status_code == 200
asyncio.run(main())
"""
    processes = [await asyncio.create_subprocess_exec(
        sys.executable, "-c", script, authority["url"], env=os.environ.copy(),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    ) for _ in range(3)]
    try:
        results = await asyncio.wait_for(asyncio.gather(*(p.communicate() for p in processes)), 15)
        assert [p.returncode for p in processes] == [0, 0, 0], results
        assert authority["refreshes"] == 1
    finally:
        for process in processes:
            if process.returncode is None:
                process.kill()
                await process.wait()


async def test_process_death_releases_recovery_lease(authority):
    storage = await seed(authority)
    script = """
import asyncio, sys
from pathlib import Path
from flowly.mcp.oauth_state import recovery_lease
async def main():
    async with recovery_lease(Path(sys.argv[1])):
        print('ready', flush=True)
        await asyncio.Event().wait()
asyncio.run(main())
"""
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-c", script, str(storage.recovery_path),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        assert await asyncio.wait_for(process.stdout.readline(), 5) == b"ready\n"
        with pytest.raises(TimeoutError, match="recovery is busy"):
            async with recovery_lease(storage.recovery_path, timeout=0.05):
                pytest.fail("another process still owns the lease")
        process.kill()
        await process.wait()
        async with recovery_lease(storage.recovery_path, timeout=0.2):
            pass
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


async def test_invalid_refresh_body_is_not_exposed_or_persisted(authority, caplog):
    storage = await seed(authority)
    authority["token_body"] = {"access_token": "SECRET-token-value", "token_type": []}
    async with client(authority) as connection:
        results = await asyncio.gather(
            *(connection.post(authority["url"]) for _ in range(3)), return_exceptions=True,
        )
    assert all(isinstance(result, OAuthRecoveryError) for result in results)
    assert any("invalid token response" in str(result) for result in results)
    assert "SECRET" not in str(results) + caplog.text
    assert authority["refreshes"] == 1
    assert (await storage.get_tokens()).access_token == "expired"


async def test_refresh_preserves_omitted_scope_and_refresh_token(authority):
    storage = await seed(authority)
    token = await storage.get_tokens()
    token.scope = "read"
    await storage.set_tokens(token)
    authority["access"] = "new-access"
    authority["token_body"] = {
        "access_token": "new-access", "token_type": "Bearer", "expires_in": 3600,
    }
    async with client(authority) as connection:
        assert (await connection.post(authority["url"])).status_code == 200
    saved = await storage.get_tokens()
    assert saved.scope == "read"
    assert saved.refresh_token == "refresh-0"
    authority["token_body"] = None
    await storage.update({"expires_at": 1})
    async with client(authority) as connection:
        assert (await connection.post(authority["url"])).status_code == 200
    assert authority["refreshes"] == 2


@pytest.mark.parametrize("cached", [False, True])
async def test_changed_issuer_never_receives_refresh_credentials(authority, cached):
    storage = await seed(authority)
    info = await storage.get_client_info()
    info.issuer = "https://original-issuer.example"
    await storage.set_client_info(info)
    if cached:
        await storage.update({"oauth_metadata": {
            "issuer": authority["base"], "authorization_endpoint": authority["base"] + "/authorize",
            "token_endpoint": authority["base"] + "/token",
        }})
    async with client(authority) as connection:
        with pytest.raises(OAuthRecoveryError, match="needs interactive OAuth"):
            await connection.post(authority["url"])
    assert authority["refreshes"] == 0


@pytest.mark.parametrize("interactive", [False, True])
async def test_scope_step_up_needs_interactive_consent_and_keeps_existing_scope(authority, interactive):
    storage = await seed(authority, access=authority["access"])
    token = await storage.get_tokens()
    token.scope = "read"
    await storage.set_tokens(token)
    authority["resource_status"] = 403
    authority["resource_headers"] = {"WWW-Authenticate": 'Bearer error="insufficient_scope", scope="write"'}
    provider = build_oauth_provider("remote", authority["url"], interactive=interactive)
    redirected = {}

    async def redirect(url):
        redirected.update(parse_qs(urlparse(url).query))

    async def callback():
        authority["resource_status"] = 200
        return AuthorizationCodeResult(code="test-code", state=redirected["state"][0])

    provider.context.redirect_handler = redirect
    provider.context.callback_handler = callback
    async with httpx2.AsyncClient(auth=provider) as connection:
        if interactive:
            assert (await connection.post(authority["url"])).status_code == 200
            assert set(redirected["scope"][0].split()) == {"read", "write"}
            assert set((await storage.get_tokens()).scope.split()) == {"read", "write"}
        else:
            with pytest.raises(OAuthRecoveryError):
                await connection.post(authority["url"])
            assert not redirected
            assert authority["refreshes"] == 0


async def test_legacy_store_guard_distinguishes_logout_from_unchanged_state(authority):
    from flowly.mcp.oauth import OAuthStateChangedError

    storage = await seed(authority)
    data = storage.snapshot()
    data.pop("revision")
    storage._path.write_text(json.dumps(data))
    old = storage.snapshot()
    assert old["revision"].startswith("legacy:")
    with storage.guard(old):
        clear_tokens("remote")
        with pytest.raises(OAuthStateChangedError):
            await storage.set_tokens(OAuthToken(access_token="stale", token_type="Bearer"))
    assert await storage.get_tokens() is None


async def test_concurrent_storage_updates_preserve_both_fields(authority):
    storage = FlowlyTokenStorage("remote", authority["url"])
    await asyncio.gather(
        storage.set_tokens(OAuthToken(access_token="access", token_type="Bearer")),
        storage.set_client_info(OAuthClientInformationFull(client_id="client")),
    )
    assert (await storage.get_tokens()).access_token == "access"
    assert (await storage.get_client_info()).client_id == "client"


async def test_storage_failure_is_not_reported_as_success(authority, monkeypatch):
    from flowly.mcp import oauth

    storage = await seed(authority)
    before = storage.snapshot()

    def fail(*args):
        raise OSError("disk full")

    monkeypatch.setattr(oauth, "atomic_private_write", fail)
    with pytest.raises(OSError):
        await storage.set_tokens(OAuthToken(access_token="unsaved", token_type="Bearer"))
    assert storage.snapshot() == before


@pytest.mark.parametrize("outcome", ["success", "failure", "cancelled", "newer-login", "logout"])
async def test_staged_login_never_rolls_back_newer_credentials(authority, outcome):
    from flowly.mcp.oauth import OAuthStateChangedError, oauth_login

    storage = await seed(authority, access=authority["access"])
    before = storage.snapshot()
    staged = None
    try:
        with oauth_login("remote", authority["url"]) as login:
            staged = build_oauth_provider("remote", authority["url"], interactive=True).storage
            assert staged is not storage
            assert await staged.get_tokens() is None
            await staged.set_tokens(OAuthToken(access_token="fresh-login", token_type="Bearer"))
            assert storage.snapshot() == before
            if outcome == "success":
                login.commit()
            elif outcome == "cancelled":
                raise asyncio.CancelledError
            elif outcome in {"newer-login", "logout"}:
                if outcome == "newer-login":
                    await storage.set_tokens(OAuthToken(access_token="other-process", token_type="Bearer"))
                else:
                    clear_tokens("remote")
                with pytest.raises(OAuthStateChangedError):
                    login.commit()
    except asyncio.CancelledError:
        assert outcome == "cancelled"
    if outcome in {"failure", "cancelled"}:
        assert storage.snapshot() == before
    elif outcome == "logout":
        assert await storage.get_tokens() is None
    else:
        expected = "fresh-login" if outcome == "success" else "other-process"
        assert (await storage.get_tokens()).access_token == expected
    assert not list(storage._path.parent.glob(".login-*"))
    with pytest.raises(OAuthStateChangedError):
        await staged.set_tokens(OAuthToken(access_token="late-result", token_type="Bearer"))
    assert not staged._path.exists()


@pytest.fixture
async def authenticated_mcp(authority, request):
    """Official MCP HTTP server plus an independently running OAuth authority."""
    import uvicorn
    from mcp.server.mcpserver import MCPServer
    from starlette.responses import Response

    service = MCPServer("flowly-oauth-transport-test")
    state = {"expire": False, "expired": set(), "calls": 0}

    @service.tool(structured_output=True)
    async def echo(message: str) -> dict[str, str]:
        state["calls"] += 1
        return {"echo": message}

    transport = getattr(request, "param", "http")
    if transport == "sse":
        inner = service.sse_app(sse_path="/mcp", message_path="/messages/")
    else:
        inner = service.streamable_http_app(
            streamable_http_path="/mcp", json_response=True, stateless_http=False,
        )

    async def app(scope, receive, send):
        if scope["type"] == "http":
            headers = dict(scope["headers"])
            if headers.get(b"authorization") != f"Bearer {authority['access']}".encode():
                response = Response(status_code=401, headers={
                    "WWW-Authenticate": f'Bearer resource_metadata="{authority["base"]}/.well-known/oauth-protected-resource"',
                })
                await response(scope, receive, send)
                return
            session_id = headers.get(b"mcp-session-id")
            if session_id and state["expire"]:
                state["expire"] = False
                state["expired"].add(session_id)
            if session_id in state["expired"]:
                await Response(status_code=404)(scope, receive, send)
                return
        await inner(scope, receive, send)

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
    thread = threading.Thread(target=lambda: server.run(sockets=[listener]), daemon=True)
    thread.start()
    try:
        async with asyncio.timeout(5):
            while not server.started:
                assert thread.is_alive()
                await asyncio.sleep(0.01)
        authority["url"] = f"http://127.0.0.1:{port}/mcp"
        yield state
    finally:
        from flowly.mcp import shutdown_mcp_servers

        await asyncio.to_thread(shutdown_mcp_servers)
        server.should_exit = True
        await asyncio.to_thread(thread.join, 5)
        listener.close()
        assert not thread.is_alive()


@pytest.mark.parametrize("protocol", ["auto", "legacy"])
async def test_public_mcp_client_recovers_credentials_and_expired_session(
    authority, authenticated_mcp, protocol,
):
    from flowly.agent.tools.registry import ToolRegistry
    from flowly.mcp import discover_mcp_tools, get_mcp_server_health

    storage = await seed(authority)
    registry = ToolRegistry()
    names = await asyncio.to_thread(discover_mcp_tools, servers={"remote": {
        "url": authority["url"], "auth": "oauth", "protocol": protocol,
        "timeout": 5, "connect_timeout": 5,
        "lifecycle": {"reconnect_base_delay": 0.01, "reconnect_max_delay": 0.02},
    }}, tool_registry=registry)
    assert names == ["mcp_remote_echo"]
    assert authority["refreshes"] == 1

    async def call(message):
        return json.loads(await registry.execute("mcp_remote_echo", {"message": message}))

    assert (await call("first"))["structuredContent"] == {"echo": "first"}
    authority["access"] = "server-invalidated-access-token"
    assert (await call("recovered"))["structuredContent"] == {"echo": "recovered"}
    assert authority["refreshes"] == 2
    assert authenticated_mcp["calls"] == 2

    if protocol == "legacy":
        before = storage.snapshot()
        authenticated_mcp["expire"] = True
        failed = await call("do-not-replay")
        assert "Session terminated" in failed["error"]
        async with asyncio.timeout(5):
            while True:
                health = get_mcp_server_health()["remote"]
                if health["connected"] and health["reconnectCount"] >= 1:
                    break
                await asyncio.sleep(0.01)
        assert storage.snapshot() == before
        assert authority["refreshes"] == 2
        assert (await call("new-session"))["structuredContent"] == {"echo": "new-session"}
        assert authenticated_mcp["calls"] == 3  # The failed tool was never replayed.


async def test_public_interactive_probe_uses_staged_grant_across_loop_boundary(
    authority, authenticated_mcp, monkeypatch,
):
    from flowly.mcp import oauth
    from flowly.mcp.probe import probe_message_async

    storage = await seed(authority, access=authority["access"])
    before = storage.snapshot()
    original_build = oauth.build_oauth_provider
    redirected = {}

    def build(*args, **kwargs):
        provider = original_build(*args, **kwargs)

        async def redirect(url):
            redirected.update(parse_qs(urlparse(url).query))

        async def callback():
            return AuthorizationCodeResult(code="test-code", state=redirected["state"][0])

        provider.context.redirect_handler = redirect
        provider.context.callback_handler = callback
        return provider

    monkeypatch.setattr(oauth, "build_oauth_provider", build)
    monkeypatch.setenv("OAUTH_TEST_ENDPOINT", authority["url"])
    with oauth.oauth_login("remote", "${OAUTH_TEST_ENDPOINT}") as login:
        ok, message = await probe_message_async("remote", {
            "url": "${OAUTH_TEST_ENDPOINT}", "auth": "oauth",
        }, interactive=True)
        assert ok, message
        assert storage.snapshot() == before
        login.commit()
    assert redirected["code_challenge_method"] == ["S256"]
    assert (await storage.get_tokens()).access_token == "valid-1"
    assert not list(storage._path.parent.glob(".login-*"))


@pytest.mark.parametrize("authenticated_mcp", ["sse"], indirect=True)
async def test_public_legacy_sse_authenticates_separate_post_endpoint(authority, authenticated_mcp):
    from flowly.agent.tools.registry import ToolRegistry
    from flowly.mcp import discover_mcp_tools

    await seed(authority)
    registry = ToolRegistry()
    names = await asyncio.to_thread(discover_mcp_tools, servers={"remote": {
        "url": authority["url"], "auth": "oauth", "transport": "sse",
        "timeout": 5, "connect_timeout": 5,
    }}, tool_registry=registry)
    assert names == ["mcp_remote_echo"]
    authority["access"] = "invalidated"
    result = json.loads(await registry.execute("mcp_remote_echo", {"message": "sse"}))
    assert result["structuredContent"] == {"echo": "sse"}
    assert authority["refreshes"] == 2
