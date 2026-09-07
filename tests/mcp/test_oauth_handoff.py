"""Desktop OAuth routing never opens a browser on a remote agent's host."""
from urllib.parse import urlencode

import asyncio
import pytest

from flowly.mcp.oauth_handoff import (
    DesktopOAuthHandoff, OAuthHandoffError, desktop_oauth_handoff,
    handoff_for, validate_desktop_redirect,
)

REDIRECT = "http://127.0.0.1:54321/mcp/oauth/callback/" + "a" * 32
STATE = "s" * 43


def authorization_url(redirect=REDIRECT, **overrides):
    params = {"state": STATE, "redirect_uri": redirect,
              "code_challenge": "c" * 43, "code_challenge_method": "S256"}
    params.update(overrides)
    return "https://identity.example/authorize?" + urlencode(params)


@pytest.mark.parametrize("uri", [
    "https://attacker.example/callback", "file:///callback", "flowly://callback",
    REDIRECT.replace("127.0.0.1", "localhost"), REDIRECT.replace("127.0.0.1", "0.0.0.0"),
    REDIRECT.replace("127.0.0.1", "user@127.0.0.1"), REDIRECT + "?next=https://attacker.example",
    REDIRECT + "#fragment", REDIRECT.replace(":54321", ":80"),
    REDIRECT.replace(":54321", ":999999"), REDIRECT.replace("/mcp/", "/../mcp/"),
])
def test_rejects_unowned_redirects(uri):
    with pytest.raises(OAuthHandoffError):
        validate_desktop_redirect(uri)


@pytest.mark.asyncio
async def test_routes_callback_and_allows_only_identical_rpc_retry():
    flow = DesktopOAuthHandoff(REDIRECT)
    await flow.redirect(authorization_url())
    payload = {"code": "auth-code", "state": STATE, "iss": "https://identity.example"}
    flow.submit(payload)
    flow.submit(payload)
    assert (await flow.wait())["code"] == "auth-code"
    with pytest.raises(OAuthHandoffError, match="already consumed"):
        flow.submit({**payload, "code": "different"})
    assert "auth-code" not in str(flow.snapshot())
    flow.close()
    assert flow.snapshot()["authorizationUrl"] is None


@pytest.mark.asyncio
async def test_bad_state_does_not_consume_legitimate_callback():
    flow = DesktopOAuthHandoff(REDIRECT)
    await flow.redirect(authorization_url())
    with pytest.raises(OAuthHandoffError, match="does not match"):
        flow.submit({"code": "bad", "state": "wrong"})
    flow.submit({"code": "good", "state": STATE})
    assert (await flow.wait())["code"] == "good"


@pytest.mark.asyncio
@pytest.mark.parametrize("url", [
    "javascript:alert(1)", "file:///etc/passwd", "http://remote.example/authorize",
    authorization_url(code_challenge_method="plain"), authorization_url(state="short"),
    authorization_url(redirect="https://attacker.example/callback"),
    authorization_url() + "&state=second",
    authorization_url().replace("https://identity.example", "https://user:pass@identity.example"),
])
async def test_rejects_unsafe_authorization_addresses(url):
    flow = DesktopOAuthHandoff(REDIRECT)
    with pytest.raises(OAuthHandoffError):
        await flow.redirect(url)


@pytest.mark.asyncio
async def test_cancel_and_expiry_release_waiters():
    flow = DesktopOAuthHandoff(REDIRECT)
    waiting = asyncio.create_task(flow.wait())
    flow.close()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    expired = DesktopOAuthHandoff(REDIRECT, timeout=0.001)
    with pytest.raises(OAuthHandoffError, match="timed out"):
        await expired.wait()
    with pytest.raises(OAuthHandoffError, match="expired"):
        expired.submit({"code": "late", "state": STATE})


@pytest.mark.asyncio
async def test_runtime_context_is_scoped_and_restored():
    flow = DesktopOAuthHandoff(REDIRECT)
    assert handoff_for("demo", "https://mcp.example") is None
    with desktop_oauth_handoff("demo", "https://mcp.example", flow):
        assert handoff_for("demo", "https://mcp.example") is flow
        # The dedicated MCP loop receives a copy of this context, not UI globals.
        assert await asyncio.to_thread(handoff_for, "demo", "https://mcp.example") is flow
        with pytest.raises(OAuthHandoffError, match="different"):
            handoff_for("other", "https://mcp.example")
    assert handoff_for("demo", "https://mcp.example") is None


@pytest.mark.asyncio
async def test_provider_uses_desktop_instead_of_host_browser(tmp_path, monkeypatch):
    from flowly.mcp.oauth import build_oauth_provider

    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path))
    def forbidden(*args, **kwargs):
        pytest.fail("The remote agent must not launch a browser")
    monkeypatch.setattr("webbrowser.open", forbidden)
    flow = DesktopOAuthHandoff(REDIRECT)
    with desktop_oauth_handoff("demo", "https://mcp.example", flow):
        provider = build_oauth_provider("demo", "https://mcp.example", interactive=True)
        assert str(provider.context.client_metadata.redirect_uris[0]) == REDIRECT
        await provider.context.redirect_handler(authorization_url())
        flow.submit({"code": "demo-code", "state": STATE})
        result = await provider.context.callback_handler()
        assert result.code == "demo-code"
        assert result.state == STATE


@pytest.mark.asyncio
async def test_error_callback_validates_issuer_before_acting(tmp_path, monkeypatch):
    from flowly.mcp.oauth import build_oauth_provider
    from mcp.shared.auth import OAuthMetadata

    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path))
    flow = DesktopOAuthHandoff(REDIRECT)
    with desktop_oauth_handoff("demo", "https://mcp.example", flow):
        provider = build_oauth_provider("demo", "https://mcp.example", interactive=True)
        provider.context.oauth_metadata = OAuthMetadata(
            issuer="https://identity.example", authorization_endpoint="https://identity.example/authorize",
            token_endpoint="https://identity.example/token", response_types_supported=["code"],
            authorization_response_iss_parameter_supported=True,
        )
        await provider.context.redirect_handler(authorization_url())
        flow.submit({"error": "untrusted-provider-message", "state": STATE, "iss": "https://attacker.example"})
        with pytest.raises(Exception) as exc:
            await provider.context.callback_handler()
        assert "untrusted-provider-message" not in str(exc.value)
        assert "issuer" in str(exc.value).lower()
