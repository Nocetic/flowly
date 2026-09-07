"""Token failures expose standard error codes, never provider-controlled text."""

import asyncio
import json
import traceback

import httpx2
import pytest
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from flowly.mcp.oauth import build_oauth_provider
from flowly.mcp.oauth_provider import OAuthRecoveryError


@pytest.fixture
async def provider(tmp_path, monkeypatch):
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path))
    instance = build_oauth_provider("diagnostic", "https://oauth.example.test/mcp", interactive=False)
    token = OAuthToken(access_token="existing-test-token", refresh_token="existing-test-refresh")
    await instance.storage.set_tokens(token)
    instance.context.current_tokens = token
    return instance


@pytest.mark.parametrize("code", [
    "invalid_request", "invalid_client", "invalid_grant", "unauthorized_client",
    "unsupported_grant_type", "invalid_scope",
])
async def test_only_standard_code_survives_and_credentials_remain_unchanged(provider, code, caplog):
    secret = "provider-private-value-12345"
    response = httpx2.Response(400, json={
        "error": code, "error_description": secret, "error_uri": "https://example.test/" + secret,
        "access_token": secret, "refresh_token": secret, "code": secret, "client_secret": secret,
    })
    before = provider.storage.snapshot()
    with pytest.raises(OAuthRecoveryError) as raised:
        await provider._handle_token_response(response)
    assert str(raised.value) == f"OAuth token exchange failed (HTTP 400): {code}"
    assert secret not in "".join(traceback.format_exception(raised.value)) + caplog.text
    assert provider.storage.snapshot() == before
    assert provider.context.current_tokens.access_token == "existing-test-token"


@pytest.mark.parametrize("field", [
    "client_id", "client_secret", "code", "code_verifier", "redirect_uri", "grant_type", "resource", "scope",
])
async def test_description_exposes_only_recognized_field_names(provider, field, caplog):
    secret = "private-value-98765"
    response = httpx2.Response(400, json={
        "error": "invalid_request", "error_description": f"Invalid {field}: {secret}",
    })
    with pytest.raises(OAuthRecoveryError) as raised:
        await provider._handle_token_response(response)
    assert str(raised.value) == (
        f"OAuth token exchange failed (HTTP 400): invalid_request (provider fields: {field})"
    )
    assert secret not in "".join(traceback.format_exception(raised.value)) + caplog.text


@pytest.mark.parametrize("description", [
    None, {}, ["client_secret"], 123, "my_client_secret_value", "private-code", "scope-private-value",
])
async def test_malformed_or_partial_field_description_is_not_rendered(provider, description):
    response = httpx2.Response(400, json={"error": "invalid_request", "error_description": description})
    with pytest.raises(OAuthRecoveryError) as raised:
        await provider._handle_token_response(response)
    assert str(raised.value) == "OAuth token exchange failed (HTTP 400): invalid_request"


async def test_multiple_provider_fields_are_deduplicated_in_fixed_order(provider):
    response = httpx2.Response(400, json={
        "error": "invalid_request",
        "error_description": "Check redirect_uri and CLIENT_SECRET; client_secret=private-value",
    })
    with pytest.raises(OAuthRecoveryError) as raised:
        await provider._handle_token_response(response)
    assert str(raised.value) == (
        "OAuth token exchange failed (HTTP 400): invalid_request (provider fields: client_secret, redirect_uri)"
    )


@pytest.mark.parametrize("method,label", [
    (None, "unspecified"), ("none", "none"), ("client_secret_basic", "client_secret_basic"),
    ("client_secret_post", "client_secret_post"), ("provider-private-value", "other"),
])
@pytest.mark.parametrize("has_secret", [False, True])
async def test_negotiated_auth_diagnostic_never_exposes_client_credentials(provider, method, label, has_secret, caplog):
    from flowly.mcp.security import sanitize_error

    info = OAuthClientInformationFull(
        client_id="private-client-id", client_secret="private-client-secret" if has_secret else None,
        token_endpoint_auth_method=method,
    )
    provider.context.client_info = info
    response = httpx2.Response(400, json={"error": "invalid_request"})
    with pytest.raises(OAuthRecoveryError) as raised:
        await provider._handle_token_response(response)
    presence = "yes" if has_secret else "no"
    assert str(raised.value) == (
        f"OAuth token exchange failed (HTTP 400): invalid_request [client_auth={label}; secret_present={presence}]"
    )
    rendered = "".join(traceback.format_exception(raised.value)) + caplog.text
    assert not any(value in rendered for value in ["private-client-id", "private-client-secret", "provider-private-value"])
    assert sanitize_error(str(raised.value)) == str(raised.value)
    assert provider.context.client_info is info


@pytest.mark.parametrize("body", [
    b'{"error":"provider-private-value"}',
    b'{"error":"invalid_client provider-private-value"}',
    b'{"error":"invalid_client\\nprovider-private-value"}',
    b'{"error":{"code":"invalid_client"}}',
    b'{"error":["invalid_client"]}',
    b'{"error":null}', b'{"error":400}', b'{}', b'[]', b'null',
    b'{"error_description":"invalid_client provider-private-value"}',
    b'<html>provider-private-value</html>', b'\xffprovider-private-value',
    b'{"error":"invalid_client"}provider-private-value',
])
async def test_unknown_or_malformed_body_keeps_safe_http_fallback(provider, body, caplog):
    with pytest.raises(OAuthRecoveryError) as raised:
        await provider._handle_token_response(httpx2.Response(400, content=body))
    assert str(raised.value) == "OAuth token exchange failed (HTTP 400)"
    assert "provider-private-value" not in "".join(traceback.format_exception(raised.value)) + caplog.text


class DiagnosticStream(httpx2.AsyncByteStream):
    def __init__(self, chunks=(), *, error=None, wait=False):
        self.chunks = chunks
        self.error = error
        self.wait = wait
        self.started = asyncio.Event()
        self.reads = 0

    async def __aiter__(self):
        self.started.set()
        for chunk in self.chunks:
            self.reads += 1
            yield chunk
        if self.error:
            raise self.error
        if self.wait:
            await asyncio.Event().wait()


async def test_streamed_error_is_parsed_without_exposing_description(provider):
    body = json.dumps({"error": "invalid_grant", "error_description": "private-code"}).encode()
    stream = DiagnosticStream([body[:12], body[12:]])
    with pytest.raises(OAuthRecoveryError, match=r"\(HTTP 401\): invalid_grant$"):
        await provider._handle_token_response(httpx2.Response(401, stream=stream))
    assert stream.reads == 2


async def test_oversized_error_body_is_not_parsed_or_fully_drained(provider):
    stream = DiagnosticStream([b'{"error":"invalid_client","extra":"', b'x' * 100_000, b'"}'])
    with pytest.raises(OAuthRecoveryError) as raised:
        await provider._handle_token_response(httpx2.Response(400, stream=stream))
    assert str(raised.value) == "OAuth token exchange failed (HTTP 400)"
    assert stream.reads == 2


@pytest.mark.parametrize("error", [
    httpx2.ReadError("private-transport-value"),
    httpx2.DecodingError("private-transport-value"),
    httpx2.StreamConsumed(),
])
async def test_read_failure_cannot_replace_http_error_or_leak_exception(provider, error):
    response = httpx2.Response(400, stream=DiagnosticStream(error=error))
    with pytest.raises(OAuthRecoveryError) as raised:
        await provider._handle_token_response(response)
    assert str(raised.value) == "OAuth token exchange failed (HTTP 400)"
    assert "private-transport-value" not in "".join(traceback.format_exception(raised.value))


async def test_slow_error_body_has_a_short_diagnostic_deadline(provider, monkeypatch):
    monkeypatch.setattr("flowly.mcp.oauth_provider._OAUTH_ERROR_READ_TIMEOUT_SECONDS", 0.01)
    stream = DiagnosticStream(wait=True)
    async with asyncio.timeout(2):
        with pytest.raises(OAuthRecoveryError, match=r"\(HTTP 400\)$"):
            await provider._handle_token_response(httpx2.Response(400, stream=stream))
    assert stream.started.is_set()


async def test_owner_cancellation_is_not_swallowed_by_diagnostic_read(provider):
    stream = DiagnosticStream(wait=True)
    task = asyncio.create_task(provider._handle_token_response(httpx2.Response(400, stream=stream)))
    try:
        await asyncio.wait_for(stream.started.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
