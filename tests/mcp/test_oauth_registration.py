"""Registration contract through the production SDK and a strict local authority."""

import base64
import hashlib
from urllib.parse import parse_qs, urlsplit

import httpx2
import pytest
from aiohttp import web
from mcp.shared.auth import AuthorizationCodeResult

from flowly.mcp.oauth import build_oauth_provider


@pytest.fixture
async def authority(tmp_path, monkeypatch):
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path))
    state = {"registrations": [], "tokens": [], "authorization": {}, "reply": "echo"}

    async def resource(request):
        if request.headers.get("Authorization") == "Bearer test-access":
            return web.json_response({"ok": True})
        return web.Response(status=401, headers={"WWW-Authenticate": "Bearer"})

    async def protected(request):
        return web.json_response({
            "resource": state["url"], "authorization_servers": [state["base"]],
            "scopes_supported": ["read"],
        })

    async def metadata(request):
        return web.json_response({
            "issuer": state["base"], "authorization_endpoint": state["base"] + "/authorize",
            "token_endpoint": state["base"] + "/token",
            "registration_endpoint": state["base"] + "/register",
            "token_endpoint_auth_methods_supported": ["none", "client_secret_basic", "client_secret_post"],
            "code_challenge_methods_supported": ["S256"],
        })

    async def register(request):
        data = await request.json()
        state["registrations"].append(data)
        # Omission is not an explicit public registration (RFC 7591 section 2).
        requested = data.get("token_endpoint_auth_method", "client_secret_basic")
        method = state["reply"] if state["reply"].startswith("client_secret_") else requested
        state["method"] = method
        result = {**data, "client_id": "test-client"}
        if state["reply"] != "omit":
            result["token_endpoint_auth_method"] = method
        else:
            result.pop("token_endpoint_auth_method", None)
        if method != "none" or state["reply"] == "none_with_secret":
            result["client_secret"] = "test-secret"
        return web.json_response(result, status=201)

    async def token(request):
        data = dict(await request.post())
        state["tokens"].append(data)
        authorization = request.headers.get("Authorization")
        if state["method"] == "client_secret_basic":
            expected = base64.b64encode(b"test-client:test-secret").decode()
            assert authorization == f"Basic {expected}"
            assert "client_secret" not in data
        elif state["method"] == "client_secret_post":
            assert authorization is None
            assert data["client_secret"] == "test-secret"
        else:
            assert authorization is None
            assert "client_secret" not in data
        assert data["client_id"] == "test-client"
        assert data["resource"] == state["url"]
        if data["grant_type"] == "authorization_code":
            assert data["code"] == "test-code+reserved/value="
            assert data["redirect_uri"] == state["authorization"]["redirect_uri"][0]
            challenge = base64.urlsafe_b64encode(hashlib.sha256(data["code_verifier"].encode()).digest())
            assert challenge.rstrip(b"=").decode() == state["authorization"]["code_challenge"][0]
        else:
            assert data["grant_type"] == "refresh_token"
            assert data["refresh_token"] == "test-refresh"
        return web.json_response({
            "access_token": "test-access", "refresh_token": "test-refresh",
            "token_type": "Bearer", "expires_in": 3600,
        })

    app = web.Application()
    app.router.add_post("/mcp", resource)
    app.router.add_get("/.well-known/oauth-protected-resource/mcp", protected)
    app.router.add_get("/.well-known/oauth-authorization-server", metadata)
    app.router.add_post("/register", register)
    app.router.add_post("/token", token)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    state["base"] = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
    state["url"] = state["base"] + "/mcp"
    try:
        yield state
    finally:
        await runner.cleanup()


@pytest.mark.parametrize("reply", ["echo", "omit", "none_with_secret", "client_secret_basic", "client_secret_post"])
async def test_public_registration_honors_negotiated_auth_for_login_and_restart_refresh(authority, reply):
    authority["reply"] = reply
    provider = build_oauth_provider("registration", authority["url"], interactive=True, scope="read")

    async def redirect(url):
        authority["authorization"].update(parse_qs(urlsplit(url).query))

    async def callback():
        return AuthorizationCodeResult(
            code="test-code+reserved/value=", state=authority["authorization"]["state"][0],
        )

    provider.context.redirect_handler = redirect
    provider.context.callback_handler = callback
    headers = {"mcp-protocol-version": "2025-06-18"}
    async with httpx2.AsyncClient(auth=provider, headers=headers) as connection:
        assert (await connection.post(authority["url"])).status_code == 200

    registration, = authority["registrations"]
    assert registration["token_endpoint_auth_method"] == "none"
    assert registration["application_type"] == "native"
    assert registration["scope"] == "read"
    assert "client_secret" not in registration
    assert registration["redirect_uris"] == authority["authorization"]["redirect_uri"]
    assert authority["authorization"]["code_challenge_method"] == ["S256"]

    await provider.storage.update({"expires_at": 1})
    restarted = build_oauth_provider("registration", authority["url"], interactive=False)
    async with httpx2.AsyncClient(auth=restarted, headers=headers) as connection:
        assert (await connection.post(authority["url"])).status_code == 200
    assert len(authority["registrations"]) == 1
    assert [data["grant_type"] for data in authority["tokens"]] == ["authorization_code", "refresh_token"]
    assert (await restarted.storage.get_tokens()).scope == "read"
