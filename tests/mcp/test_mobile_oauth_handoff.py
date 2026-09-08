"""Mobile handoff must preserve the owner callback security boundary."""

import pytest

from flowly.mcp.oauth_handoff import (
    IOS_OAUTH_REDIRECT_URI, ANDROID_OAUTH_REDIRECT_URI, OwnerOAuthHandoff, OAuthHandoffError,
    validate_owner_redirect, validate_desktop_redirect,
)
from tests.mcp.test_oauth_handoff import authorization_url, STATE, REDIRECT


@pytest.mark.parametrize("uri", [IOS_OAUTH_REDIRECT_URI, ANDROID_OAUTH_REDIRECT_URI, REDIRECT])
def test_accepts_only_supported_owner_callback_modes(uri):
    assert validate_owner_redirect(uri) == uri


@pytest.mark.parametrize("suffix", ["/", "?state=x", "#x", "?", "#", "\n"])
@pytest.mark.parametrize("uri", [IOS_OAUTH_REDIRECT_URI, ANDROID_OAUTH_REDIRECT_URI])
def test_mobile_callback_requires_exact_uri(suffix, uri):
    with pytest.raises(OAuthHandoffError):
        validate_owner_redirect(uri + suffix)


@pytest.mark.parametrize("uri", [
    "http://useflowlyapp.com/api/auth/mcp/ios/callback",
    "https://useflowlyapp.com:443/api/auth/mcp/ios/callback",
    "https://useflowlyapp.com.attacker.example/api/auth/mcp/ios/callback",
    "https://attacker@useflowlyapp.com/api/auth/mcp/ios/callback",
    "https://useflowlyapp.com/api/auth/mcp/android/%63allback",
    "https://useflowlyapp.com:443/api/auth/mcp/android/callback",
    "https://useflowlyapp.com.attacker.example/api/auth/mcp/android/callback",
    "https://useflowlyapp.com/api/auth/mcp/ios/%63allback",
])
def test_rejects_unregistered_mobile_redirects(uri):
    with pytest.raises(OAuthHandoffError):
        validate_owner_redirect(uri)


@pytest.mark.parametrize("uri", [IOS_OAUTH_REDIRECT_URI, ANDROID_OAUTH_REDIRECT_URI])
def test_desktop_validator_remains_loopback_only(uri):
    with pytest.raises(OAuthHandoffError):
        validate_desktop_redirect(uri)


@pytest.mark.asyncio
@pytest.mark.parametrize("uri", [IOS_OAUTH_REDIRECT_URI, ANDROID_OAUTH_REDIRECT_URI])
async def test_mobile_state_retry_and_cancel(uri):
    flow = OwnerOAuthHandoff(uri)
    await flow.redirect(authorization_url(redirect=uri))
    with pytest.raises(OAuthHandoffError):
        flow.submit({"code": "code", "state": "wrong"})
    payload = {"code": "code", "state": STATE}
    flow.submit(payload)
    flow.submit(payload)
    assert (await flow.wait())["code"] == "code"
    with pytest.raises(OAuthHandoffError):
        flow.submit({**payload, "code": "replay"})
    flow.close()
    with pytest.raises(OAuthHandoffError):
        flow.submit(payload)
