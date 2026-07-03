"""xAI OAuth PKCE helpers for Grok subscription access.

xAI offers no self-service OAuth client registration: the subscription
login scope (``grok-cli:access``) only works with the public ``grok-cli``
client id that Grok-subscription CLIs reuse. Flowly bakes the same id in
as a constant so login works out of the box; it is intentionally not
user-configurable.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import threading
import time
import urllib.parse
import webbrowser
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
from loguru import logger

from flowly.profile import credential_scope_suffix, get_flowly_home

DEFAULT_XAI_OAUTH_BASE_URL = "https://api.x.ai/v1"
XAI_OAUTH_DISCOVERY_URL = "https://auth.x.ai/.well-known/openid-configuration"
XAI_OAUTH_SCOPE = "openid profile email offline_access grok-cli:access api:access"
XAI_OAUTH_REDIRECT_HOST = "127.0.0.1"
XAI_OAUTH_REDIRECT_PORT = 56121
XAI_OAUTH_REDIRECT_PATH = "/callback"
XAI_OAUTH_REDIRECT_URI = (
    f"http://{XAI_OAUTH_REDIRECT_HOST}:{XAI_OAUTH_REDIRECT_PORT}"
    f"{XAI_OAUTH_REDIRECT_PATH}"
)
XAI_OAUTH_CLIENT_ID_ENV = "FLOWLY_XAI_OAUTH_CLIENT_ID"
# Public ``grok-cli`` OAuth client that Grok-subscription CLIs reuse. xAI
# has no self-service client registration, so this is the only id its
# subscription scope accepts. Hardcoded and not user-configurable — login
# must "just work" without setup.
XAI_OAUTH_CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"
# Origins allowed to read the loopback callback response cross-origin. xAI's
# consent page confirms the redirect with a fetch to 127.0.0.1; without these
# CORS headers (and Private-Network-Access on Chrome) that fetch is blocked
# and xAI falls back to a "couldn't reach your app — paste the code" page even
# though our listener already received the code.
_XAI_OAUTH_CORS_ORIGINS = frozenset({"https://accounts.x.ai", "https://auth.x.ai"})

_KEYRING_ACCOUNT = "xai-oauth"


def _keyring_service() -> str:
    """Keychain service name, scoped to the active FLOWLY_HOME.

    Unsuffixed (``"flowly-tui"``) at the default home for backward
    compatibility; suffixed everywhere else so two homes (e.g. a second
    product built on this codebase) never share one keychain entry. See
    :func:`flowly.profile.credential_scope_suffix`.
    """
    suffix = credential_scope_suffix()
    return f"flowly-tui:{suffix}" if suffix else "flowly-tui"


_REFRESH_SKEW_SECONDS = 90
_HTTP_TIMEOUT = httpx.Timeout(20.0, connect=8.0)


class XAIAuthError(RuntimeError):
    """Base class for xAI OAuth failures."""


class XAIEntitlementError(XAIAuthError):
    """The OAuth token is valid but the account tier cannot use the API."""


class XAIClientIDMissingError(XAIAuthError):
    """Flowly has not been configured with an xAI OAuth client id."""


@dataclass(frozen=True)
class XAITokenPayload:
    access_token: str
    refresh_token: str = ""
    id_token: str = ""
    token_type: str = "Bearer"
    scope: str = ""
    expires_at: int = 0
    base_url: str = DEFAULT_XAI_OAUTH_BASE_URL
    email: str = ""
    subject: str = ""

    @classmethod
    def from_raw(cls, raw: dict[str, Any] | None) -> "XAITokenPayload | None":
        if not isinstance(raw, dict):
            return None
        tokens = raw.get("tokens") if isinstance(raw.get("tokens"), dict) else raw
        access = str(tokens.get("access_token") or "").strip()
        if not access:
            return None
        profile = raw.get("profile") if isinstance(raw.get("profile"), dict) else {}
        base = str(raw.get("base_url") or tokens.get("base_url") or DEFAULT_XAI_OAUTH_BASE_URL)
        return cls(
            access_token=access,
            refresh_token=str(tokens.get("refresh_token") or raw.get("refresh_token") or ""),
            id_token=str(tokens.get("id_token") or raw.get("id_token") or ""),
            token_type=str(tokens.get("token_type") or "Bearer"),
            scope=str(tokens.get("scope") or ""),
            expires_at=int(tokens.get("expires_at") or raw.get("expires_at") or 0),
            base_url=validate_xai_oauth_base_url(base),
            email=str(profile.get("email") or raw.get("email") or ""),
            subject=str(profile.get("sub") or raw.get("subject") or ""),
        )

    def to_raw(self) -> dict[str, Any]:
        now = int(time.time())
        return {
            "provider": "xai_oauth",
            "base_url": validate_xai_oauth_base_url(self.base_url),
            "updated_at": now,
            "tokens": {
                "access_token": self.access_token,
                "refresh_token": self.refresh_token,
                "id_token": self.id_token,
                "token_type": self.token_type or "Bearer",
                "scope": self.scope,
                "expires_at": int(self.expires_at or 0),
            },
            "profile": {
                "email": self.email,
                "sub": self.subject,
            },
        }


@dataclass(frozen=True)
class XAIRuntimeCredentials:
    provider: str
    api_key: str
    base_url: str
    auth_mode: str
    expires_at: int = 0
    email: str = ""


def credentials_path() -> Path:
    return get_flowly_home() / "credentials" / "xai_oauth.json"


def resolve_client_id(config: Any | None = None) -> str:
    # Always the shared grok-cli client (see XAI_OAUTH_CLIENT_ID). It is the
    # only id xAI's subscription scope accepts, so there is nothing to
    # configure — config/env knobs would only be footguns. The ``config``
    # parameter is kept for call-site compatibility.
    return XAI_OAUTH_CLIENT_ID


def require_client_id(config: Any | None = None) -> str:
    return XAI_OAUTH_CLIENT_ID


def redact_secret(text: str, *secrets_to_hide: str) -> str:
    result = str(text)
    for secret in secrets_to_hide:
        if isinstance(secret, str) and len(secret) >= 8:
            result = result.replace(secret, "***")
    return result


def validate_xai_oauth_base_url(url: str | None) -> str:
    candidate = (url or DEFAULT_XAI_OAUTH_BASE_URL).strip().rstrip("/")
    parsed = urllib.parse.urlparse(candidate)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https":
        raise XAIAuthError("xAI OAuth bearer tokens may only be sent to HTTPS endpoints")
    if host != "api.x.ai" and not host.endswith(".x.ai"):
        raise XAIAuthError(
            f"Refusing to send xAI OAuth bearer token to non-xAI host: {host or '<empty>'}"
        )
    return candidate


def _provider_host_is_xai(url: str) -> bool:
    parsed = urllib.parse.urlparse(url)
    host = (parsed.hostname or "").lower()
    return parsed.scheme == "https" and (host == "x.ai" or host.endswith(".x.ai"))


def _storage_status() -> str:
    if _try_keyring() is not None:
        return "keyring"
    return f"file:{credentials_path()}"


def _try_keyring():
    marker = get_flowly_home() / "credentials" / ".keychain-broken"
    if marker.exists():
        return None
    try:
        import keyring  # type: ignore[import-not-found]
        backend = keyring.get_keyring()
        module = type(backend).__module__ or ""
        if "fail" in module or "null" in module:
            return None
        return keyring
    except Exception:
        return None


def _write_file(raw: dict[str, Any]) -> None:
    path = credentials_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".tmp.{secrets.token_hex(4)}")
    tmp.write_text(json.dumps(raw, separators=(",", ":")), encoding="utf-8")
    os.replace(str(tmp), str(path))
    try:
        from flowly.utils.file_security import secure_file
        secure_file(path)  # POSIX chmod; real owner-only ACL on Windows
    except OSError:
        pass


def _read_file() -> dict[str, Any] | None:
    path = credentials_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return raw if isinstance(raw, dict) else None


def load_token_payload() -> XAITokenPayload | None:
    keyring = _try_keyring()
    if keyring is not None:
        try:
            raw_blob = keyring.get_password(_keyring_service(), _KEYRING_ACCOUNT)
        except Exception as exc:
            logger.warning("xAI OAuth keyring read failed, falling back to file: {}", exc)
            raw_blob = None
        if raw_blob:
            try:
                return XAITokenPayload.from_raw(json.loads(raw_blob))
            except json.JSONDecodeError:
                return None
    return XAITokenPayload.from_raw(_read_file())


def save_token_payload(payload: XAITokenPayload) -> str:
    raw = payload.to_raw()
    keyring = _try_keyring()
    if keyring is not None:
        try:
            keyring.set_password(_keyring_service(), _KEYRING_ACCOUNT, json.dumps(raw, separators=(",", ":")))
            try:
                credentials_path().unlink(missing_ok=True)
            except OSError:
                pass
            return _storage_status()
        except Exception as exc:
            logger.warning("xAI OAuth keyring write failed, falling back to file: {}", exc)
    _write_file(raw)
    return _storage_status()


def clear_token_payload() -> None:
    keyring = _try_keyring()
    if keyring is not None:
        try:
            keyring.delete_password(_keyring_service(), _KEYRING_ACCOUNT)
        except Exception:
            pass
    try:
        credentials_path().unlink(missing_ok=True)
    except OSError:
        pass


def _b64url_decode(value: str) -> bytes:
    padded = value + ("=" * ((4 - len(value) % 4) % 4))
    return base64.urlsafe_b64decode(padded.encode("ascii"))


def _jwt_claims(token: str) -> dict[str, Any]:
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return {}
        return json.loads(_b64url_decode(parts[1]))
    except Exception:
        return {}


def _expires_at_from_token(access_token: str, expires_in: Any = None) -> int:
    now = int(time.time())
    try:
        ttl = int(expires_in)
    except (TypeError, ValueError):
        ttl = 0
    if ttl > 0:
        return now + ttl
    claims = _jwt_claims(access_token)
    try:
        return int(claims.get("exp") or 0)
    except (TypeError, ValueError):
        return 0


def _profile_from_tokens(access_token: str, id_token: str = "") -> dict[str, str]:
    claims = _jwt_claims(id_token) or _jwt_claims(access_token)
    return {
        "email": str(claims.get("email") or ""),
        "sub": str(claims.get("sub") or claims.get("subject") or ""),
    }


def token_is_expiring(payload: XAITokenPayload | None) -> bool:
    if payload is None or not payload.access_token:
        return True
    if not payload.expires_at:
        claims_exp = _expires_at_from_token(payload.access_token)
        if not claims_exp:
            return False
        return claims_exp <= int(time.time()) + _REFRESH_SKEW_SECONDS
    return payload.expires_at <= int(time.time()) + _REFRESH_SKEW_SECONDS


def pkce_verifier() -> str:
    return secrets.token_urlsafe(64)[:96]


def pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def discover_oauth_metadata() -> dict[str, str]:
    with httpx.Client(timeout=_HTTP_TIMEOUT) as client:
        response = client.get(
            XAI_OAUTH_DISCOVERY_URL,
            headers={"Accept": "application/json", "User-Agent": "flowly/xai-oauth"},
        )
    response.raise_for_status()
    data = response.json()
    authorization_endpoint = str(data.get("authorization_endpoint") or "")
    token_endpoint = str(data.get("token_endpoint") or "")
    issuer = str(data.get("issuer") or "")
    for endpoint_name, endpoint in {
        "authorization_endpoint": authorization_endpoint,
        "token_endpoint": token_endpoint,
        "issuer": issuer,
    }.items():
        if endpoint and not _provider_host_is_xai(endpoint):
            raise XAIAuthError(f"xAI discovery returned unsafe {endpoint_name}: {endpoint}")
    if not authorization_endpoint or not token_endpoint:
        raise XAIAuthError("xAI discovery response is missing OAuth endpoints")
    return {
        "authorization_endpoint": authorization_endpoint,
        "token_endpoint": token_endpoint,
        "issuer": issuer,
    }


def build_authorize_url(
    *,
    client_id: str,
    code_challenge: str,
    state: str,
    nonce: str,
    authorization_endpoint: str,
) -> str:
    query = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": XAI_OAUTH_REDIRECT_URI,
        "scope": XAI_OAUTH_SCOPE,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "state": state,
        "nonce": nonce,
        "plan": "generic",
        "referrer": "flowly",
    }
    return f"{authorization_endpoint}?{urllib.parse.urlencode(query)}"


def _parse_callback_input(value: str) -> dict[str, str]:
    raw = value.strip()
    if not raw:
        return {}
    if raw.startswith("http://") or raw.startswith("https://"):
        parsed = urllib.parse.urlparse(raw)
        query = parsed.query or parsed.fragment
    elif raw.startswith("?") or "=" in raw:
        query = raw[1:] if raw.startswith("?") else raw
    else:
        return {"code": raw}
    pairs = urllib.parse.parse_qs(query, keep_blank_values=True)
    return {k: v[-1] for k, v in pairs.items() if v}


def exchange_code_for_tokens(
    *,
    code: str,
    client_id: str,
    code_verifier: str,
    code_challenge_value: str,
    token_endpoint: str,
) -> XAITokenPayload:
    data = {
        "grant_type": "authorization_code",
        "client_id": client_id,
        "code": code,
        "redirect_uri": XAI_OAUTH_REDIRECT_URI,
        "code_verifier": code_verifier,
        # xAI's OAuth surface has historically required these to be echoed.
        "code_challenge": code_challenge_value,
        "code_challenge_method": "S256",
    }
    try:
        with httpx.Client(timeout=_HTTP_TIMEOUT) as client:
            response = client.post(
                token_endpoint,
                data=data,
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/x-www-form-urlencoded",
                    "User-Agent": "flowly/xai-oauth",
                },
            )
    except httpx.HTTPError as exc:
        raise XAIAuthError(f"xAI token exchange failed: {type(exc).__name__}") from exc
    if response.status_code == 403:
        raise XAIEntitlementError(
            "xAI accepted the OAuth login but this account is not entitled to "
            "API access. Use an xAI API key or upgrade/link the subscription."
        )
    if response.status_code >= 400:
        raise XAIAuthError(
            f"xAI token exchange failed: HTTP {response.status_code} "
            f"{redact_secret(response.text[:300], code, code_verifier)}"
        )
    body = response.json()
    access = str(body.get("access_token") or "").strip()
    if not access:
        raise XAIAuthError("xAI token exchange did not return an access token")
    refresh = str(body.get("refresh_token") or "").strip()
    id_token = str(body.get("id_token") or "").strip()
    profile = _profile_from_tokens(access, id_token)
    return XAITokenPayload(
        access_token=access,
        refresh_token=refresh,
        id_token=id_token,
        token_type=str(body.get("token_type") or "Bearer"),
        scope=str(body.get("scope") or ""),
        expires_at=_expires_at_from_token(access, body.get("expires_in")),
        base_url=DEFAULT_XAI_OAUTH_BASE_URL,
        email=profile["email"],
        subject=profile["sub"],
    )


def refresh_tokens(*, client_id: str | None = None, payload: XAITokenPayload | None = None) -> XAITokenPayload:
    current = payload or load_token_payload()
    if current is None or not current.refresh_token:
        raise XAIAuthError("xAI OAuth refresh token is missing; run `flowly xai login`.")
    client_id = client_id or require_client_id()
    metadata = discover_oauth_metadata()
    data = {
        "grant_type": "refresh_token",
        "client_id": client_id,
        "refresh_token": current.refresh_token,
    }
    try:
        with httpx.Client(timeout=_HTTP_TIMEOUT) as client:
            response = client.post(
                metadata["token_endpoint"],
                data=data,
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/x-www-form-urlencoded",
                    "User-Agent": "flowly/xai-oauth",
                },
            )
    except httpx.HTTPError as exc:
        raise XAIAuthError(f"xAI token refresh failed: {type(exc).__name__}") from exc
    if response.status_code == 403:
        raise XAIEntitlementError(
            "xAI OAuth refresh was rejected with HTTP 403. The account is "
            "authenticated but not entitled to this API surface."
        )
    if response.status_code in (400, 401):
        clear_token_payload()
        raise XAIAuthError("xAI OAuth refresh token was rejected; run `flowly xai login` again.")
    if response.status_code >= 400:
        raise XAIAuthError(f"xAI token refresh failed: HTTP {response.status_code}")
    body = response.json()
    access = str(body.get("access_token") or "").strip()
    if not access:
        raise XAIAuthError("xAI refresh response did not return an access token")
    refresh = str(body.get("refresh_token") or current.refresh_token).strip()
    id_token = str(body.get("id_token") or current.id_token).strip()
    profile = _profile_from_tokens(access, id_token)
    updated = XAITokenPayload(
        access_token=access,
        refresh_token=refresh,
        id_token=id_token,
        token_type=str(body.get("token_type") or current.token_type or "Bearer"),
        scope=str(body.get("scope") or current.scope or ""),
        expires_at=_expires_at_from_token(access, body.get("expires_in")),
        base_url=current.base_url or DEFAULT_XAI_OAUTH_BASE_URL,
        email=profile["email"] or current.email,
        subject=profile["sub"] or current.subject,
    )
    save_token_payload(updated)
    return updated


def resolve_runtime_credentials(
    *,
    config: Any | None = None,
    force_refresh: bool = False,
) -> XAIRuntimeCredentials | None:
    payload = load_token_payload()
    if payload is None:
        return None
    if force_refresh or token_is_expiring(payload):
        payload = refresh_tokens(client_id=require_client_id(config), payload=payload)
    return XAIRuntimeCredentials(
        provider="xai_oauth",
        api_key=payload.access_token,
        base_url=validate_xai_oauth_base_url(payload.base_url),
        auth_mode="oauth_pkce",
        expires_at=payload.expires_at,
        email=payload.email,
    )


class _CallbackState:
    def __init__(self, expected_state: str):
        self.expected_state = expected_state
        self.event = threading.Event()
        self.code = ""
        self.error = ""
        self.state_ok = False


def _make_callback_handler(callback_state: _CallbackState):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
            return

        def _write_cors_headers(self) -> None:
            # Let xAI's consent page read this response. Chrome additionally
            # requires Allow-Private-Network for a public→localhost request.
            origin = self.headers.get("Origin")
            if origin in _XAI_OAUTH_CORS_ORIGINS:
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "Content-Type")
                self.send_header("Access-Control-Allow-Private-Network", "true")
                self.send_header("Vary", "Origin")

        def do_OPTIONS(self) -> None:  # noqa: N802
            # CORS / Private-Network preflight for the cross-origin callback.
            self.send_response(204)
            self._write_cors_headers()
            self.end_headers()

        def do_GET(self) -> None:  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path != XAI_OAUTH_REDIRECT_PATH:
                self.send_error(404)
                return
            query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
            code = (query.get("code") or [""])[-1]
            error = (query.get("error") or [""])[-1]
            received_state = (query.get("state") or [""])[-1]
            callback_state.state_ok = bool(received_state) and secrets.compare_digest(
                received_state,
                callback_state.expected_state,
            )
            if error:
                callback_state.error = error
            elif not callback_state.state_ok:
                callback_state.error = "OAuth state mismatch"
            elif code:
                callback_state.code = code
            else:
                callback_state.error = "OAuth callback missing code"
            callback_state.event.set()
            ok = bool(callback_state.code and not callback_state.error)
            body = (
                "<html><body><h2>Flowly xAI login complete.</h2>"
                "<p>You can close this tab and return to Flowly.</p></body></html>"
                if ok
                else "<html><body><h2>Flowly xAI login failed.</h2>"
                "<p>Return to Flowly for details.</p></body></html>"
            )
            self.send_response(200 if ok else 400)
            self._write_cors_headers()
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body.encode("utf-8"))

    return Handler


def login_with_loopback(
    *,
    client_id: str,
    no_browser: bool = False,
    manual_code: str = "",
    timeout_seconds: int = 300,
    on_authorize_url: Any | None = None,
) -> XAITokenPayload:
    metadata = discover_oauth_metadata()
    verifier = pkce_verifier()
    challenge = pkce_challenge(verifier)
    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    authorize_url = build_authorize_url(
        client_id=client_id,
        code_challenge=challenge,
        state=state,
        nonce=nonce,
        authorization_endpoint=metadata["authorization_endpoint"],
    )

    if manual_code:
        parsed = _parse_callback_input(manual_code)
        code = parsed.get("code", "")
        received_state = parsed.get("state", "")
        if received_state and not secrets.compare_digest(received_state, state):
            raise XAIAuthError("OAuth state mismatch in pasted callback URL")
        return exchange_code_for_tokens(
            code=code,
            client_id=client_id,
            code_verifier=verifier,
            code_challenge_value=challenge,
            token_endpoint=metadata["token_endpoint"],
        )

    callback_state = _CallbackState(expected_state=state)
    try:
        server = ThreadingHTTPServer(
            (XAI_OAUTH_REDIRECT_HOST, XAI_OAUTH_REDIRECT_PORT),
            _make_callback_handler(callback_state),
        )
    except OSError as exc:
        raise XAIAuthError(
            f"Could not bind OAuth callback server on {XAI_OAUTH_REDIRECT_URI}: {exc}. "
            "Retry with `flowly xai login --manual-paste`."
        ) from exc

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        if on_authorize_url is not None:
            on_authorize_url(authorize_url)
        if not no_browser:
            webbrowser.open(authorize_url)
        logger.info(
            "xAI OAuth login started: browser_opened={} redirect_uri={} state_fingerprint={}",
            not no_browser,
            XAI_OAUTH_REDIRECT_URI,
            hashlib.sha256(state.encode()).hexdigest()[:12],
        )
        if not callback_state.event.wait(timeout=max(1, int(timeout_seconds))):
            raise XAIAuthError("Timed out waiting for xAI OAuth callback")
        if callback_state.error:
            raise XAIAuthError(callback_state.error)
        return exchange_code_for_tokens(
            code=callback_state.code,
            client_id=client_id,
            code_verifier=verifier,
            code_challenge_value=challenge,
            token_endpoint=metadata["token_endpoint"],
        )
    finally:
        server.shutdown()
        server.server_close()

