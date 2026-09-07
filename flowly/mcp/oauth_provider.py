"""Concurrent authenticated requests with serialized, durable OAuth recovery.

The SDK remains responsible for discovery validation, PKCE, registration and
token exchange. Only credential recovery holds a lease: ordinary requests and
long-lived SSE streams must not serialize behind the SDK's context lock.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
import time
from typing import Any

import httpx2
from mcp.client.auth import OAuthClientProvider
from mcp.client.auth.utils import (
    build_oauth_authorization_server_metadata_discovery_urls,
    build_protected_resource_metadata_discovery_urls,
    create_oauth_metadata_request,
    credentials_match_issuer,
    extract_field_from_www_auth,
    extract_resource_metadata_from_www_auth,
    handle_auth_metadata_response,
    handle_protected_resource_response,
    validate_metadata_issuer,
)
from mcp.shared.auth import (
    OAuthClientInformationFull,
    OAuthMetadata,
    OAuthToken,
    ProtectedResourceMetadata,
)

from flowly.mcp.oauth import FlowlyTokenStorage, OAuthStateChangedError
from flowly.mcp.oauth_state import recovery_lease

_OAUTH_ERROR_BODY_LIMIT = 16 * 1024
_OAUTH_ERROR_READ_TIMEOUT_SECONDS = 2.0
# RFC 6749 section 5.2. Never render arbitrary error/error_description values:
# providers may echo credentials or user-controlled input into either field.
_TOKEN_ERROR_CODES = frozenset({
    "invalid_request", "invalid_client", "invalid_grant", "unauthorized_client",
    "unsupported_grant_type", "invalid_scope",
})
_TOKEN_ERROR_FIELDS = (
    "client_id", "client_secret", "code", "code_verifier", "redirect_uri", "grant_type", "resource", "scope",
)


class OAuthRecoveryError(RuntimeError):
    """Safe diagnostic: never includes response bodies or credential values."""


async def _token_error_detail(response: httpx2.Response) -> str | None:
    """Best-effort diagnostic; a broken/slow body must not mask the HTTP error."""
    try:
        async with asyncio.timeout(_OAUTH_ERROR_READ_TIMEOUT_SECONDS):
            content = bytearray()
            async for chunk in response.aiter_bytes():
                if len(content) + len(chunk) > _OAUTH_ERROR_BODY_LIMIT:
                    return None
                content.extend(chunk)
            # Match aread() after consuming the stream, without an unbounded read.
            response._content = bytes(content)
            body = json.loads(content)
        code = body.get("error") if isinstance(body, dict) else None
        if not isinstance(code, str) or code not in _TOKEN_ERROR_CODES:
            return None
        # Report only fixed parameter names mentioned by the provider, never
        # their values or its free-form description. A mention is context, not
        # proof that the corresponding field caused the failure.
        description = body.get("error_description")
        fields = []
        if isinstance(description, str):
            fields = [field for field in _TOKEN_ERROR_FIELDS if re.search(
                rf"(?<![\w-]){field}(?![\w-])", description, flags=re.IGNORECASE,
            )]
        return f"{code} (provider fields: {', '.join(fields)})" if fields else code
    except (httpx2.HTTPError, httpx2.StreamError, ValueError, RecursionError, TimeoutError):
        return None


def _model(cls, value):
    try:
        return cls.model_validate(value) if value else None
    except (ValueError, TypeError):
        return None


def _expiry(snapshot: dict) -> float | None:
    value = snapshot.get("expires_at")
    return float(value) if isinstance(value, (float, int)) and math.isfinite(value) else None


def _valid(snapshot: dict) -> bool:
    token = _model(OAuthToken, snapshot.get("tokens"))
    expiry = _expiry(snapshot)
    return bool(token and token.access_token and (expiry is None or time.time() < expiry))


def _refresh_key(token: OAuthToken) -> str:
    return hashlib.sha256((token.refresh_token or "").encode()).hexdigest()


def _token_revision(snapshot: dict) -> str | None:
    # Older files have no separate credential generation. Metadata writes
    # must not count as a repaired credential; successful token writes must,
    # even when an authorization server returns the same access-token string.
    token = _model(OAuthToken, snapshot.get("tokens"))
    return snapshot.get("token_revision") or (token.access_token if token else None)


class CoordinatedOAuthProvider(OAuthClientProvider):
    # Auth must not consume/buffer the body of a successful long-lived SSE GET.
    # SDK metadata/token handlers explicitly read the responses they parse.
    requires_response_body = False

    def __init__(
        self, *, interactive: bool, server_name: str,
        allow_same_origin_paths: bool = False, **kwargs: Any,
    ):
        super().__init__(**kwargs)
        self.interactive = interactive
        self.server_name = server_name
        self._allow_same_origin_paths = allow_same_origin_paths
        self._recovery_lock = asyncio.Lock()

    @property
    def storage(self) -> FlowlyTokenStorage:
        return self.context.storage  # type: ignore[return-value]

    def _login_required(self) -> OAuthRecoveryError:
        return OAuthRecoveryError(
            f"MCP server '{self.server_name}' needs interactive OAuth; "
            f"run `flowly mcp login {self.server_name}`"
        )

    def _load(self, snapshot: dict) -> None:
        self.context.current_tokens = _model(OAuthToken, snapshot.get("tokens"))
        self.context.client_info = _model(OAuthClientInformationFull, snapshot.get("client_info"))
        self.context.token_expiry_time = _expiry(snapshot)
        self.context.oauth_metadata = _model(OAuthMetadata, snapshot.get("oauth_metadata"))
        self.context.protected_resource_metadata = _model(
            ProtectedResourceMetadata, snapshot.get("protected_resource_metadata")
        )
        self.context.auth_server_url = snapshot.get("auth_server_url")
        self._initialized = True

    async def _save_metadata(self) -> None:
        await self.storage.update({
            "oauth_metadata": self.context.oauth_metadata.model_dump(mode="json")
            if self.context.oauth_metadata else None,
            "protected_resource_metadata": self.context.protected_resource_metadata.model_dump(mode="json")
            if self.context.protected_resource_metadata else None,
            "auth_server_url": self.context.auth_server_url,
        })

    async def _parse_token(self, response: httpx2.Response) -> OAuthToken:
        content = bytearray()
        async for chunk in response.aiter_bytes():
            content.extend(chunk)
            if len(content) > 1024 * 1024:
                raise OAuthRecoveryError("OAuth token response exceeds the size limit")
        # httpx2 drains an auth response again before sending the next request.
        # Cache the bounded body just as Response.aread() does, otherwise its
        # second read raises StreamConsumed after a successful token rotation.
        response._content = bytes(content)
        try:
            return OAuthToken.model_validate_json(response.content)
        except ValueError:
            # Pydantic/SDK exception strings can contain the entire token.
            raise OAuthRecoveryError("OAuth server returned an invalid token response") from None

    async def _handle_token_response(self, response: httpx2.Response) -> None:
        if response.status_code not in {200, 201}:
            diagnostic = await _token_error_detail(response)
            detail = f": {diagnostic}" if diagnostic else ""
            info = self.context.client_info
            if diagnostic and info is not None:
                method = info.token_endpoint_auth_method
                if method is None:
                    method = "unspecified"
                elif method not in {"none", "client_secret_basic", "client_secret_post"}:
                    method = "other"
                presence = "yes" if info.client_secret else "no"
                detail += f" [client_auth={method}; secret_present={presence}]"
            raise OAuthRecoveryError(f"OAuth token exchange failed (HTTP {response.status_code}){detail}")
        token = await self._parse_token(response)
        if token.scope is None:
            token.scope = self.context.client_metadata.scope
        await self.storage.set_tokens(token)
        self.context.current_tokens = token
        self.context.update_token_expiry(token)

    async def _handle_refresh_response(self, response: httpx2.Response) -> bool:
        token = await self._parse_token(response)
        prior = self.context.current_tokens
        if prior is not None:
            if token.refresh_token is None:
                token.refresh_token = prior.refresh_token
            if token.scope is None:
                token.scope = prior.scope
        await self.storage.set_tokens(token)
        self.context.current_tokens = token
        self.context.update_token_expiry(token)
        return True

    def _validate_refresh_issuer(self) -> None:
        issuer = self.context.auth_server_url
        if issuer is None and self.context.oauth_metadata:
            issuer = str(self.context.oauth_metadata.issuer)
        if issuer is None:
            # Legacy /token fallback may only reuse same-origin credentials.
            issuer = self.context.get_authorization_base_url(self.context.server_url)
        if self.context.client_info and not credentials_match_issuer(
            self.context.client_info, issuer, self.context.client_metadata_url
        ):
            raise self._login_required()

    async def _discover_refresh_endpoint(self, challenge: httpx2.Response | None):
        """Discover once for old stores; cached metadata survives process restart."""
        if self.context.oauth_metadata is not None:
            self._validate_refresh_issuer()
            return
        hint = extract_resource_metadata_from_www_auth(challenge) if challenge else None
        for url in build_protected_resource_metadata_discovery_urls(hint, self.context.server_url):
            response = yield create_oauth_metadata_request(url)
            metadata = await handle_protected_resource_response(response)
            if metadata:
                await self._validate_resource_match(metadata)
                self.context.protected_resource_metadata = metadata
                self.context.auth_server_url = str(metadata.authorization_servers[0])
                break
        for url in build_oauth_authorization_server_metadata_discovery_urls(
            self.context.auth_server_url, self.context.server_url
        ):
            response = yield create_oauth_metadata_request(url)
            ok, metadata = await handle_auth_metadata_response(response)
            if not ok:
                break
            if metadata:
                if self.context.auth_server_url:
                    validate_metadata_issuer(metadata, self.context.auth_server_url)
                self.context.oauth_metadata = metadata
                break
        issuer = self.context.auth_server_url
        if issuer is None and self.context.oauth_metadata:
            issuer = str(self.context.oauth_metadata.issuer)
        self._validate_refresh_issuer()
        if issuer and (not self.context.oauth_metadata or not self.context.oauth_metadata.token_endpoint):
            raise OAuthRecoveryError("OAuth authorization server metadata is unavailable; retry later")
        await self._save_metadata()

    async def _recover(self, request: httpx2.Request, challenge: httpx2.Response | None, snapshot: dict):
        self._load(snapshot)
        self.context.protocol_version = request.headers.get("mcp-protocol-version")
        scope_challenge = bool(challenge and challenge.status_code == 403)
        token = self.context.current_tokens
        if not scope_challenge and self.context.can_refresh_token() and token:
            if snapshot.get("refresh_rejected") == _refresh_key(token):
                if not self.interactive:
                    raise self._login_required()
            else:
                retry_at = snapshot.get("refresh_retry_at", 0)
                if isinstance(retry_at, (int, float)) and time.time() < retry_at:
                    raise OAuthRecoveryError("OAuth refresh is temporarily unavailable; retry later")
                discovery = self._discover_refresh_endpoint(challenge)
                try:
                    outgoing = await anext(discovery)
                    while True:
                        response = yield outgoing
                        outgoing = await discovery.asend(response)
                except StopAsyncIteration:
                    pass
                finally:
                    await discovery.aclose()
                # Record the attempt before sending it. A transport timeout,
                # invalid response, or cancellation must not let every waiter
                # immediately retry the same potentially rotated refresh token.
                await self.storage.update({"refresh_retry_at": time.time() + 5})
                response = yield await self._refresh_token()
                if response.status_code == 200:
                    if not await self._handle_refresh_response(response):
                        raise OAuthRecoveryError("OAuth refresh returned an invalid token response")
                    return
                # Temporary AS failures must not erase refreshable credentials
                # or launch a browser. Suppress a concurrent retry storm too.
                if response.status_code in {408, 429} or response.status_code >= 500:
                    await self.storage.update({"refresh_retry_at": time.time() + 5})
                    raise OAuthRecoveryError(
                        f"OAuth refresh temporarily failed (HTTP {response.status_code}); retry later"
                    )
                await self.storage.update({"refresh_rejected": _refresh_key(token)})
        if not self.interactive:
            raise self._login_required()

        # Drive the official SDK for interactive authorization. Reuse the
        # challenge already received; don't replay a denied tool request to
        # obtain the same challenge. Send the final authenticated request only
        # after leaving the recovery lease.
        if not scope_challenge:
            self.context.current_tokens = None
            self.context.token_expiry_time = None
        driver = super().async_auth_flow(request)
        try:
            outgoing = await anext(driver)
            response = challenge if challenge is not None else (yield outgoing)
            while True:
                outgoing = await driver.asend(response)
                if outgoing is request and self.context.is_token_valid():
                    await self._save_metadata()
                    return
                response = yield outgoing
        except StopAsyncIteration:
            raise self._login_required() from None
        finally:
            await driver.aclose()

    async def async_auth_flow(self, request: httpx2.Request):
        configured = httpx2.URL(self.context.server_url)
        # This auth object is attached to a shared HTTP client. Never attach
        # its credentials to an unrelated origin/path through that client.
        same_origin = (request.url.scheme, request.url.host, request.url.port) == (
            configured.scheme, configured.host, configured.port,
        )
        same_path = request.url.path.rstrip("/") == configured.path.rstrip("/")
        # Legacy SSE advertises a separate POST path. The SDK validates its
        # origin before using it; only that transport opts into sibling paths.
        if not same_origin or not (same_path or self._allow_same_origin_paths):
            yield request
            return
        request.headers.pop("Authorization", None)
        snapshot = await asyncio.to_thread(self.storage.snapshot)
        rejected_revision = None
        challenge = None
        if _valid(snapshot):
            rejected_revision = _token_revision(snapshot)
            request.headers["Authorization"] = f"Bearer {snapshot['tokens']['access_token']}"
            challenge = yield request
            if challenge.status_code != 401 and not (
                challenge.status_code == 403
                and extract_field_from_www_auth(challenge, "error") == "insufficient_scope"
            ):
                return

        async with self._recovery_lock:
            async with recovery_lease(self.storage.recovery_path):
                current = await asyncio.to_thread(self.storage.snapshot)
                token = _model(OAuthToken, current.get("tokens"))
                # Another request/process may already have repaired this token.
                if not (_valid(current) and token and _token_revision(current) != rejected_revision):
                    with self.storage.guard(current):
                        driver = self._recover(request, challenge, current)
                        try:
                            outgoing = await anext(driver)
                            while True:
                                response = yield outgoing
                                outgoing = await driver.asend(response)
                        except StopAsyncIteration:
                            pass
                        except OAuthStateChangedError:
                            # A concurrent login/logout has higher authority than
                            # the response to an old refresh. Never overwrite it.
                            pass
                        finally:
                            await driver.aclose()
                current = await asyncio.to_thread(self.storage.snapshot)
                if not _valid(current):
                    raise self._login_required()
                request.headers["Authorization"] = f"Bearer {current['tokens']['access_token']}"
        response = yield request
        if response.status_code == 401:
            raise OAuthRecoveryError("OAuth credentials were rejected after recovery; log in again")
