import os
import time
from pathlib import Path

import pytest

from flowly.auth import xai_oauth
from flowly.config.schema import Config
from flowly.integrations.active_provider import resolve_active_provider


@pytest.fixture(autouse=True)
def isolated_flowly_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path))
    monkeypatch.setattr(xai_oauth, "_try_keyring", lambda: None)


def test_client_id_is_hardcoded_and_not_overridable(monkeypatch: pytest.MonkeyPatch):
    # The shared grok-cli client is baked in; env/config must not change it.
    expected = "b1a00492-073a-47ea-816f-4c329264a828"
    assert xai_oauth.XAI_OAUTH_CLIENT_ID == expected
    assert xai_oauth.resolve_client_id() == expected
    assert xai_oauth.require_client_id() == expected

    monkeypatch.setenv("FLOWLY_XAI_OAUTH_CLIENT_ID", "someone-elses-id")
    cfg = Config()
    cfg.providers.xai_oauth.client_id = "config-override"
    # Neither the env var nor a config value can override the baked-in id.
    assert xai_oauth.resolve_client_id(cfg) == expected
    assert xai_oauth.require_client_id(cfg) == expected


def test_xai_oauth_base_url_is_pinned_to_xai_hosts():
    assert xai_oauth.validate_xai_oauth_base_url("https://api.x.ai/v1") == "https://api.x.ai/v1"
    assert xai_oauth.validate_xai_oauth_base_url("https://sub.x.ai/v1") == "https://sub.x.ai/v1"

    with pytest.raises(xai_oauth.XAIAuthError):
        xai_oauth.validate_xai_oauth_base_url("http://api.x.ai/v1")
    with pytest.raises(xai_oauth.XAIAuthError):
        xai_oauth.validate_xai_oauth_base_url("https://evil.example/v1")


def test_pkce_challenge_is_s256_base64url():
    verifier = "abc123"
    challenge = xai_oauth.pkce_challenge(verifier)

    assert "=" not in challenge
    assert challenge == "bKE9UspwyIPg8LsQHkJaiehiTeUdstI5JZOvaoQRgJA"


def test_token_storage_roundtrip_uses_profile_credentials_dir():
    payload = xai_oauth.XAITokenPayload(
        access_token="access-token-123",
        refresh_token="refresh-token-123",
        expires_at=int(time.time()) + 3600,
        email="user@example.com",
    )

    backend = xai_oauth.save_token_payload(payload)
    loaded = xai_oauth.load_token_payload()

    assert backend.startswith("file:")
    assert loaded is not None
    assert loaded.access_token == "access-token-123"
    assert loaded.refresh_token == "refresh-token-123"
    assert loaded.email == "user@example.com"
    assert (Path(os.environ["FLOWLY_HOME"]) / "credentials" / "xai_oauth.json").exists()


def test_active_provider_resolves_xai_oauth_without_refresh():
    payload = xai_oauth.XAITokenPayload(
        access_token="oauth-access",
        expires_at=int(time.time()) + 3600,
        email="grok@example.com",
    )
    xai_oauth.save_token_payload(payload)
    cfg = Config()
    cfg.providers.active = "xai_oauth"
    cfg.providers.xai_oauth.client_id = "flowly-client"

    active = resolve_active_provider(cfg)

    assert active is not None
    assert active.key == "xai_oauth"
    assert active.api_key == "oauth-access"
    assert active.api_base == "https://api.x.ai/v1"
    assert "grok@example.com" in active.source


def test_authorize_url_contains_pkce_and_flowly_referrer():
    url = xai_oauth.build_authorize_url(
        client_id="client-id",
        code_challenge="challenge",
        state="state",
        nonce="nonce",
        authorization_endpoint="https://auth.x.ai/oauth2/auth",
    )

    assert "client_id=client-id" in url
    assert "code_challenge=challenge" in url
    assert "code_challenge_method=S256" in url
    assert "redirect_uri=http%3A%2F%2F127.0.0.1%3A56121%2Fcallback" in url
    assert "referrer=flowly" in url


def test_callback_handler_sends_cors_and_private_network_headers():
    # xAI's consent page fetches the loopback cross-origin to confirm the
    # redirect; without these headers Chrome blocks it and xAI shows the
    # "couldn't reach your app — paste the code" fallback.
    import http.client
    import threading
    from http.server import ThreadingHTTPServer

    state = xai_oauth._CallbackState(expected_state="STATE123")
    srv = ThreadingHTTPServer(("127.0.0.1", 0), xai_oauth._make_callback_handler(state))
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        # Preflight from an allowed xAI origin.
        conn = http.client.HTTPConnection("127.0.0.1", port)
        conn.request("OPTIONS", "/callback", headers={"Origin": "https://auth.x.ai"})
        resp = conn.getresponse(); resp.read()
        assert resp.status == 204
        assert resp.getheader("Access-Control-Allow-Origin") == "https://auth.x.ai"
        assert resp.getheader("Access-Control-Allow-Private-Network") == "true"

        # Actual callback delivers the code and echoes CORS headers.
        conn = http.client.HTTPConnection("127.0.0.1", port)
        conn.request("GET", "/callback?code=ABC&state=STATE123",
                     headers={"Origin": "https://auth.x.ai"})
        resp = conn.getresponse(); resp.read()
        assert resp.status == 200
        assert resp.getheader("Access-Control-Allow-Origin") == "https://auth.x.ai"
        assert state.code == "ABC" and state.state_ok

        # Unlisted origins get no CORS grant.
        conn = http.client.HTTPConnection("127.0.0.1", port)
        conn.request("OPTIONS", "/callback", headers={"Origin": "https://evil.example"})
        resp = conn.getresponse(); resp.read()
        assert resp.getheader("Access-Control-Allow-Origin") is None
    finally:
        srv.shutdown()


def test_exchange_code_classifies_403_as_entitlement(monkeypatch: pytest.MonkeyPatch):
    class FakeResponse:
        status_code = 403
        text = "forbidden"

        def json(self):
            return {}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def post(self, *args, **kwargs):
            return FakeResponse()

    monkeypatch.setattr(xai_oauth.httpx, "Client", FakeClient)

    with pytest.raises(xai_oauth.XAIEntitlementError):
        xai_oauth.exchange_code_for_tokens(
            code="code",
            client_id="client",
            code_verifier="verifier",
            code_challenge_value="challenge",
            token_endpoint="https://auth.x.ai/oauth2/token",
        )
