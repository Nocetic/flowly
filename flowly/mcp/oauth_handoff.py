"""One bounded OAuth exchange with a callback owned by the user's Desktop.

Only runtime code can install this context; server configuration cannot replace
OAuth handlers. Futures are thread-safe because MCP runs on its own event loop.
PKCE, discovery and issuer validation remain with the OAuth SDK/provider.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import hashlib
import json
import re
import secrets
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from urllib.parse import parse_qs, urlsplit


class OAuthHandoffError(ValueError):
    """An intentionally credential-free, user-actionable validation error."""


def validate_desktop_redirect(uri: str) -> str:
    if not isinstance(uri, str) or len(uri) > 512:
        raise OAuthHandoffError("Invalid Desktop callback address")
    try:
        parsed = urlsplit(uri)
        valid = (
            parsed.scheme == "http" and parsed.hostname == "127.0.0.1"
            and parsed.port is not None and 1024 <= parsed.port <= 65535
            and parsed.netloc == f"127.0.0.1:{parsed.port}"
            and not parsed.query and not parsed.fragment
            and re.fullmatch(r"/mcp/oauth/callback/[a-f0-9]{32,64}", parsed.path)
            and not any(ord(char) <= 32 for char in uri)
        )
    except ValueError:
        valid = False
    if not valid:
        raise OAuthHandoffError("OAuth callback must be a private Desktop loopback listener")
    return uri


class DesktopOAuthHandoff:
    def __init__(self, redirect_uri: str, *, timeout: float = 300.0):
        self.redirect_uri = validate_desktop_redirect(redirect_uri)
        if not 0 < timeout <= 600:
            raise OAuthHandoffError("Invalid OAuth callback deadline")
        self._deadline = time.monotonic() + timeout
        self._lock = threading.Lock()
        self._callback: concurrent.futures.Future[dict[str, str | None]] = concurrent.futures.Future()
        self._authorization_url: str | None = None
        self._state: str | None = None
        self._accepted_digest: str | None = None
        self._closed = False

    def _assert_open(self) -> None:
        if self._closed or time.monotonic() >= self._deadline:
            raise OAuthHandoffError("OAuth request expired or was cancelled; start again")

    async def redirect(self, authorization_url: str) -> None:
        if not isinstance(authorization_url, str) or len(authorization_url) > 16_384:
            raise OAuthHandoffError("Invalid OAuth authorization address")
        parsed = urlsplit(authorization_url)
        query = parse_qs(parsed.query, keep_blank_values=True)
        # A provider-supplied URL is untrusted data, never an arbitrary desktop
        # protocol/command launcher. HTTP is only useful for loopback dev servers.
        if (
            not parsed.hostname or parsed.username or parsed.password or parsed.fragment
            or (parsed.scheme != "https" and not (
                parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "[::1]", "::1", "localhost"}
            ))
            or any(ord(char) <= 32 for char in authorization_url)
            or query.get("redirect_uri") != [self.redirect_uri]
            or len(query.get("state", [])) != 1
            or not 32 <= len(query["state"][0]) <= 512
            or query.get("code_challenge_method") != ["S256"]
            or len(query.get("code_challenge", [])) != 1
            or not 43 <= len(query["code_challenge"][0]) <= 128
        ):
            raise OAuthHandoffError("OAuth server returned an unsafe authorization request")
        with self._lock:
            self._assert_open()
            if self._authorization_url is not None:
                raise OAuthHandoffError("OAuth request already started")
            self._state = query["state"][0]
            self._authorization_url = authorization_url

    def snapshot(self) -> dict:
        """Owner-only status. Contains no access token, refresh token or verifier."""
        with self._lock:
            return {
                "authorizationUrl": self._authorization_url if not self._closed else None,
                "callbackReceived": self._accepted_digest is not None,
                "expiresIn": max(0, int(self._deadline - time.monotonic())),
            }

    def submit(self, payload: dict) -> None:
        if not isinstance(payload, dict):
            raise OAuthHandoffError("Invalid OAuth callback")
        values: dict[str, str | None] = {}
        for key in ("code", "state", "iss", "error"):
            value = payload.get(key)
            if value is not None and (not isinstance(value, str) or not value or len(value) > 8192):
                raise OAuthHandoffError("Invalid OAuth callback")
            values[key] = value
        if bool(values["code"]) == bool(values["error"]):
            raise OAuthHandoffError("OAuth callback requires a code or an error")
        digest = hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()
        with self._lock:
            self._assert_open()
            state = values["state"] or ""
            if not state.isascii() or not self._state or not secrets.compare_digest(state, self._state):
                raise OAuthHandoffError("OAuth callback does not match this request")
            if self._accepted_digest:
                if secrets.compare_digest(self._accepted_digest, digest):
                    return  # Lost RPC acknowledgment: the same callback is safe to retry.
                raise OAuthHandoffError("OAuth callback was already consumed")
            if self._callback.done():
                raise OAuthHandoffError("OAuth request is no longer waiting")
            self._accepted_digest = digest
            self._callback.set_result(values)

    async def wait(self) -> dict[str, str | None]:
        try:
            remaining = max(0, self._deadline - time.monotonic())
            return await asyncio.wait_for(asyncio.wrap_future(self._callback), remaining)
        except TimeoutError:
            self.close()
            raise OAuthHandoffError("OAuth sign-in timed out; start again") from None
        except asyncio.CancelledError:
            self.close()
            raise

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._authorization_url = None
            self._state = None
            self._callback.cancel()


_active_handoff: ContextVar[tuple[str, str, DesktopOAuthHandoff] | None] = ContextVar(
    "mcp_desktop_oauth_handoff", default=None,
)


@contextmanager
def desktop_oauth_handoff(server_name: str, url: str, handoff: DesktopOAuthHandoff):
    token = _active_handoff.set((server_name, url, handoff))
    try:
        yield handoff
    finally:
        _active_handoff.reset(token)
        handoff.close()


def handoff_for(server_name: str, url: str) -> DesktopOAuthHandoff | None:
    current = _active_handoff.get()
    if current is None:
        return None
    if current[:2] != (server_name, url):
        raise OAuthHandoffError("OAuth callback belongs to a different connection")
    return current[2]
