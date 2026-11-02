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


# --- entitlement_detail: surface xAI's 403 body message -----------------

def test_entitlement_detail_parses_code_and_error():
    body = (
        '{"code": "personal-team-blocked:spending-limit", '
        '"error": "You have run out of credits or need a Grok subscription. '
        'Add credits at https://grok.com/?_s=usage or upgrade at https://grok.com/supergrok."}'
    )
    detail = xai_oauth.entitlement_detail(body)
    assert detail is not None
    assert "run out of credits" in detail
    assert "personal-team-blocked:spending-limit" in detail


def test_entitlement_detail_plain_error_only():
    assert xai_oauth.entitlement_detail('{"error": "nope"}') == "nope"


def test_entitlement_detail_nested_error_object():
    body = '{"error": {"code": "x", "message": "inner message"}}'
    detail = xai_oauth.entitlement_detail(body)
    assert detail is not None
    assert "inner message" in detail


def test_entitlement_detail_garbage_returns_none():
    assert xai_oauth.entitlement_detail("<html>Cloudflare</html>") is None
    assert xai_oauth.entitlement_detail("") is None
    assert xai_oauth.entitlement_detail(None) is None
    assert xai_oauth.entitlement_detail('["list"]') is None


def test_exchange_code_403_includes_body_detail(monkeypatch: pytest.MonkeyPatch):
    class FakeResponse:
        status_code = 403
        text = '{"code": "personal-team-blocked:spending-limit", "error": "You have run out of credits."}'

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

    with pytest.raises(xai_oauth.XAIEntitlementError) as excinfo:
        xai_oauth.exchange_code_for_tokens(
            code="code",
            client_id="client",
            code_verifier="verifier",
            code_challenge_value="challenge",
            token_endpoint="https://auth.x.ai/oauth2/token",
        )
    assert "run out of credits" in str(excinfo.value)


# ── Device sign-in (any device: a phone signs the agent in) ─────────────────


class _FakeXAI:
    """xAI's device endpoints as RFC 8628 has them: a code, then the token
    endpoint's answers in order (`pending`, `slow_down`, …, then tokens)."""

    def __init__(self, answers, device=None):
        self.answers = list(answers)
        self.device = device or {
            "device_code": "dev-1", "user_code": "WDJB-MJHT", "interval": 1, "expires_in": 600,
            "verification_uri": "https://accounts.x.ai/oauth2/device",
            "verification_uri_complete": "https://accounts.x.ai/oauth2/device?user_code=WDJB-MJHT",
        }
        self.posts: list[tuple[str, dict]] = []

    def client(self, *_, **__):
        fake = self

        class _Client:
            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def post(self, url, data=None, **_):
                import httpx

                fake.posts.append((url, dict(data or {})))
                request = httpx.Request("POST", url)
                if url.endswith("/device/code"):
                    return httpx.Response(200, json=fake.device, request=request)
                status, body = fake.answers.pop(0)
                return httpx.Response(status, json=body, request=request)

        return _Client()


@pytest.fixture
def xai_device(monkeypatch: pytest.MonkeyPatch):
    def install(answers, device=None):
        fake = _FakeXAI(answers, device)
        monkeypatch.setattr(xai_oauth, "discover_oauth_metadata", lambda: {
            "authorization_endpoint": "https://auth.x.ai/oauth2/authorize",
            "token_endpoint": "https://auth.x.ai/oauth2/token",
            "device_authorization_endpoint": "https://auth.x.ai/oauth2/device/code",
            "issuer": "https://auth.x.ai",
        })
        monkeypatch.setattr(xai_oauth.httpx, "Client", fake.client)
        monkeypatch.setattr(xai_oauth.time, "sleep", lambda _s: None)
        return fake
    return install


_TOKENS = {"access_token": "grok-access", "refresh_token": "grok-refresh", "expires_in": 3600, "token_type": "Bearer"}


def test_device_sign_in_waits_for_approval_and_keeps_the_tokens(xai_device):
    fake = xai_device([
        (400, {"error": "authorization_pending"}),
        (400, {"error": "slow_down"}),
        (200, _TOKENS),
    ])
    shown: list[tuple[str, str]] = []
    payload = xai_oauth.login_with_device_code(client_id=xai_oauth.XAI_OAUTH_CLIENT_ID,
                                               on_user_code=lambda code, url: shown.append((code, url)))
    assert shown == [("WDJB-MJHT", "https://accounts.x.ai/oauth2/device?user_code=WDJB-MJHT")]
    assert payload.access_token == "grok-access" and payload.refresh_token == "grok-refresh"
    assert xai_oauth.load_token_payload().access_token == "grok-access"
    url, asked = fake.posts[0]
    assert url == "https://auth.x.ai/oauth2/device/code"
    assert asked["client_id"] == xai_oauth.XAI_OAUTH_CLIENT_ID and asked["scope"] == xai_oauth.XAI_OAUTH_SCOPE
    polls = [data for url, data in fake.posts if url.endswith("/token")]
    assert len(polls) == 3
    assert all(p == {"grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                     "client_id": xai_oauth.XAI_OAUTH_CLIENT_ID, "device_code": "dev-1"} for p in polls)


@pytest.mark.parametrize("page", [
    "http://accounts.x.ai/oauth2/device",
    "https://accounts.x.ai.evil.example/oauth2/device",
    "https://evil.example/oauth2/device",
    "https://user@accounts.x.ai/oauth2/device",
    "https://accounts.x.ai:444/oauth2/device",
    "https://accounts.x.ai/oauth2/device#token",
])
def test_device_sign_in_never_sends_the_apps_to_another_page(xai_device, page):
    xai_device([], device={"device_code": "d", "user_code": "C", "verification_uri": page})
    with pytest.raises(xai_oauth.XAIAuthError, match="unsupported sign-in page"):
        xai_oauth.login_with_device_code(client_id="c", on_user_code=lambda *_: pytest.fail("a code was shown"))


def test_device_sign_in_says_how_it_ended(xai_device):
    xai_device([(400, {"error": "access_denied"})])
    with pytest.raises(xai_oauth.XAIAuthError, match="not approved"):
        xai_oauth.login_with_device_code(client_id="c")
    xai_device([(400, {"error": "expired_token"})])
    with pytest.raises(xai_oauth.XAIAuthError, match="timed out"):
        xai_oauth.login_with_device_code(client_id="c")
    xai_device([(403, {"error": "forbidden", "error_description": "no API access"})])
    with pytest.raises(xai_oauth.XAIEntitlementError):
        xai_oauth.login_with_device_code(client_id="c")
    xai_device([(400, {"error": "authorization_pending"})] * 3)
    calls = iter([False, False, True])
    with pytest.raises(xai_oauth.XAIAuthError, match="cancelled"):
        xai_oauth.login_with_device_code(client_id="c", cancelled=lambda: next(calls))
    assert xai_oauth.load_token_payload() is None
