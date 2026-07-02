"""OpenAI Codex OAuth PKCE helpers for ChatGPT subscription access.

OpenAI offers no self-service OAuth client registration for the Codex
"Sign in with ChatGPT" scope: it only works with the public ``codex``
CLI client id that every Codex surface reuses. Flowly bakes the same id
in as a constant so login works out of the box; it is intentionally not
user-configurable.

The flow mints an access token that is accepted by the ChatGPT Codex
Responses backend (``chatgpt.com/backend-api/codex/responses``), billed
against the user's ChatGPT plan rather than the metered OpenAI API. The
account id needed for the ``ChatGPT-Account-Id`` header is derived from
the id_token JWT, not sent separately.

Two credential sources, in priority order:
  1. Flowly's own store — OS keychain / ``~/.flowly/credentials/
     openai_codex.json``, written by ``flowly codex login``.
  2. The Codex CLI's ``~/.codex/auth.json`` (respecting ``CODEX_HOME``),
     read as a fallback so anyone who already ran ``codex login`` is
     usable with zero extra steps. On refresh we write the rotated
     tokens back to that file so the CLI keeps working too.
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

from flowly.profile import get_flowly_home

CODEX_OAUTH_ISSUER = "https://auth.openai.com"
CODEX_OAUTH_AUTHORIZE_URL = f"{CODEX_OAUTH_ISSUER}/oauth/authorize"
CODEX_OAUTH_TOKEN_URL = f"{CODEX_OAUTH_ISSUER}/oauth/token"
CODEX_OAUTH_SCOPE = "openid profile email offline_access"
CODEX_OAUTH_REDIRECT_HOST = "localhost"
# The Codex OAuth client registers exactly this loopback port; the
# authorize request is rejected if the redirect_uri points anywhere else.
CODEX_OAUTH_REDIRECT_PORT = 1455
CODEX_OAUTH_REDIRECT_PATH = "/auth/callback"
CODEX_OAUTH_REDIRECT_URI = (
    f"http://{CODEX_OAUTH_REDIRECT_HOST}:{CODEX_OAUTH_REDIRECT_PORT}"
    f"{CODEX_OAUTH_REDIRECT_PATH}"
)
# Public Codex CLI OAuth client that every Codex surface reuses. OpenAI has
# no self-service client registration, so this is the only id its ChatGPT
# subscription scope accepts. Hardcoded and not user-configurable — login
# must "just work" without setup.
CODEX_OAUTH_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
# The ChatGPT Codex Responses backend billed against the user's plan.
CODEX_RESPONSES_URL = "https://chatgpt.com/backend-api/codex/responses"
CODEX_RESPONSES_BASE_URL = "https://chatgpt.com/backend-api/codex"
# JWT claim namespace OpenAI stuffs the ChatGPT account id / plan into.
_AUTH_CLAIM = "https://api.openai.com/auth"
# Device-code login endpoints (headless / no-browser path).
_DEVICE_CODE_URL = f"{CODEX_OAUTH_ISSUER}/api/accounts/deviceauth/usercode"
_DEVICE_TOKEN_URL = f"{CODEX_OAUTH_ISSUER}/api/accounts/deviceauth/token"
_DEVICE_VERIFICATION_URL = f"{CODEX_OAUTH_ISSUER}/codex/device"

_KEYRING_SERVICE = "flowly-tui"
_KEYRING_ACCOUNT = "openai-codex"
_REFRESH_SKEW_SECONDS = 300  # refresh when < 5 min of life remains
_HTTP_TIMEOUT = httpx.Timeout(30.0, connect=8.0)


class CodexAuthError(RuntimeError):
    """Base class for OpenAI Codex OAuth failures."""


class CodexEntitlementError(CodexAuthError):
    """The OAuth token is valid but the ChatGPT plan can't use Codex."""


@dataclass(frozen=True)
class CodexTokenPayload:
    access_token: str
    refresh_token: str = ""
    id_token: str = ""
    account_id: str = ""
    token_type: str = "Bearer"
    scope: str = ""
    expires_at: int = 0
    email: str = ""
    plan: str = ""

    @classmethod
    def from_raw(cls, raw: dict[str, Any] | None) -> "CodexTokenPayload | None":
        """Parse Flowly's own on-disk / keychain blob."""
        if not isinstance(raw, dict):
            return None
        tokens = raw.get("tokens") if isinstance(raw.get("tokens"), dict) else raw
        access = str(tokens.get("access_token") or "").strip()
        if not access:
            return None
        profile = raw.get("profile") if isinstance(raw.get("profile"), dict) else {}
        id_token = str(tokens.get("id_token") or raw.get("id_token") or "")
        account_id = str(tokens.get("account_id") or raw.get("account_id") or "")
        if not account_id:
            account_id = _account_id_from_token(id_token)
        return cls(
            access_token=access,
            refresh_token=str(tokens.get("refresh_token") or raw.get("refresh_token") or ""),
            id_token=id_token,
            account_id=account_id,
            token_type=str(tokens.get("token_type") or "Bearer"),
            scope=str(tokens.get("scope") or ""),
            expires_at=int(tokens.get("expires_at") or raw.get("expires_at") or 0)
            or _expires_at_from_token(access),
            email=str(profile.get("email") or raw.get("email") or "")
            or _email_from_token(id_token, access),
            plan=str(profile.get("plan") or raw.get("plan") or "")
            or _plan_from_token(access, id_token),
        )

    @classmethod
    def from_codex_auth_json(cls, raw: dict[str, Any] | None) -> "CodexTokenPayload | None":
        """Parse the Codex CLI's ``~/.codex/auth.json`` shape.

        Format: ``{"OPENAI_API_KEY": …, "tokens": {"id_token", "access_token",
        "refresh_token", "account_id"}, "last_refresh": …}``.
        """
        if not isinstance(raw, dict):
            return None
        tokens = raw.get("tokens")
        if not isinstance(tokens, dict):
            return None
        access = str(tokens.get("access_token") or "").strip()
        if not access:
            return None
        id_token = str(tokens.get("id_token") or "")
        account_id = str(tokens.get("account_id") or "") or _account_id_from_token(id_token)
        return cls(
            access_token=access,
            refresh_token=str(tokens.get("refresh_token") or ""),
            id_token=id_token,
            account_id=account_id,
            token_type="Bearer",
            scope="",
            expires_at=_expires_at_from_token(access),
            email=_email_from_token(id_token, access),
            plan=_plan_from_token(access, id_token),
        )

    def to_raw(self) -> dict[str, Any]:
        now = int(time.time())
        return {
            "provider": "openai_codex",
            "updated_at": now,
            "tokens": {
                "access_token": self.access_token,
                "refresh_token": self.refresh_token,
                "id_token": self.id_token,
                "account_id": self.account_id,
                "token_type": self.token_type or "Bearer",
                "scope": self.scope,
                "expires_at": int(self.expires_at or 0),
            },
            "profile": {
                "email": self.email,
                "plan": self.plan,
            },
        }


@dataclass(frozen=True)
class CodexRuntimeCredentials:
    provider: str
    api_key: str
    account_id: str
    base_url: str
    auth_mode: str
    expires_at: int = 0
    email: str = ""
    plan: str = ""


def credentials_path() -> Path:
    return get_flowly_home() / "credentials" / "openai_codex.json"


def codex_home_dir() -> Path:
    """The Codex CLI's home (``CODEX_HOME`` or ``~/.codex``)."""
    override = os.getenv("CODEX_HOME")
    if override and override.strip():
        return Path(override).expanduser()
    return Path.home() / ".codex"


def codex_auth_json_path() -> Path:
    return codex_home_dir() / "auth.json"


def resolve_client_id(config: Any | None = None) -> str:
    # Always the shared Codex CLI client. It is the only id OpenAI's
    # subscription scope accepts, so there is nothing to configure. The
    # ``config`` parameter is kept for call-site compatibility.
    del config
    return CODEX_OAUTH_CLIENT_ID


def require_client_id(config: Any | None = None) -> str:
    del config
    return CODEX_OAUTH_CLIENT_ID


def redact_secret(text: str, *secrets_to_hide: str) -> str:
    result = str(text)
    for secret in secrets_to_hide:
        if isinstance(secret, str) and len(secret) >= 8:
            result = result.replace(secret, "***")
    return result


# ── JWT helpers ────────────────────────────────────────────────────────


def _b64url_decode(value: str) -> bytes:
    padded = value + ("=" * ((4 - len(value) % 4) % 4))
    return base64.urlsafe_b64decode(padded.encode("ascii"))


def _jwt_claims(token: str) -> dict[str, Any]:
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return {}
        claims = json.loads(_b64url_decode(parts[1]))
        return claims if isinstance(claims, dict) else {}
    except Exception:
        return {}


def _account_id_from_token(id_token: str) -> str:
    """Extract ``chatgpt_account_id`` from the id_token (or access token)."""
    for token in (id_token,):
        claims = _jwt_claims(token)
        auth = claims.get(_AUTH_CLAIM)
        if isinstance(auth, dict):
            account_id = auth.get("chatgpt_account_id")
            if isinstance(account_id, str) and account_id:
                return account_id
    return ""


def _plan_from_token(access_token: str, id_token: str = "") -> str:
    for token in (access_token, id_token):
        claims = _jwt_claims(token)
        auth = claims.get(_AUTH_CLAIM)
        if isinstance(auth, dict):
            plan = auth.get("chatgpt_plan_type")
            if isinstance(plan, str) and plan:
                return plan
    return ""


def _email_from_token(id_token: str, access_token: str = "") -> str:
    for token in (id_token, access_token):
        claims = _jwt_claims(token)
        email = claims.get("email")
        if isinstance(email, str) and email:
            return email
        # Some id_tokens nest the email under the auth claim / profile.
        auth = claims.get(_AUTH_CLAIM)
        if isinstance(auth, dict):
            email = auth.get("email")
            if isinstance(email, str) and email:
                return email
    return ""


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


def token_is_expiring(payload: CodexTokenPayload | None) -> bool:
    if payload is None or not payload.access_token:
        return True
    if not payload.expires_at:
        claims_exp = _expires_at_from_token(payload.access_token)
        if not claims_exp:
            return False
        return claims_exp <= int(time.time()) + _REFRESH_SKEW_SECONDS
    return payload.expires_at <= int(time.time()) + _REFRESH_SKEW_SECONDS


# ── PKCE ───────────────────────────────────────────────────────────────


def pkce_verifier() -> str:
    return secrets.token_urlsafe(64)[:96]


def pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


# ── Storage (keychain first, file fallback) ────────────────────────────


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


def _storage_status() -> str:
    if _try_keyring() is not None:
        return "keyring"
    return f"file:{credentials_path()}"


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


def _read_codex_auth_json() -> dict[str, Any] | None:
    path = codex_auth_json_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return raw if isinstance(raw, dict) else None


def _write_codex_auth_json(payload: CodexTokenPayload) -> bool:
    """Write rotated tokens back into ``~/.codex/auth.json``.

    Preserves any keys the Codex CLI wrote (``OPENAI_API_KEY`` etc.) and
    only updates the ``tokens`` block + ``last_refresh`` so the CLI keeps
    working after Flowly refreshes on its behalf. Never creates the file
    if it didn't already exist — we only mirror, never mint, the CLI's store.
    """
    path = codex_auth_json_path()
    if not path.exists():
        return False
    existing = _read_codex_auth_json() or {}
    tokens = existing.get("tokens") if isinstance(existing.get("tokens"), dict) else {}
    tokens = dict(tokens)
    tokens["access_token"] = payload.access_token
    if payload.refresh_token:
        tokens["refresh_token"] = payload.refresh_token
    if payload.id_token:
        tokens["id_token"] = payload.id_token
    if payload.account_id:
        tokens["account_id"] = payload.account_id
    existing["tokens"] = tokens
    existing["last_refresh"] = _now_iso8601()
    try:
        tmp = path.with_suffix(f".tmp.{secrets.token_hex(4)}")
        tmp.write_text(json.dumps(existing, separators=(",", ":")), encoding="utf-8")
        os.replace(str(tmp), str(path))
        try:
            from flowly.utils.file_security import secure_file
            secure_file(path)
        except OSError:
            pass
        return True
    except OSError as exc:
        logger.warning("Could not write back to codex auth.json: {}", exc)
        return False


def _now_iso8601() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def load_token_payload() -> CodexTokenPayload | None:
    """Load Codex credentials: Flowly store first, then ``~/.codex/auth.json``."""
    keyring = _try_keyring()
    if keyring is not None:
        try:
            raw_blob = keyring.get_password(_KEYRING_SERVICE, _KEYRING_ACCOUNT)
        except Exception as exc:
            logger.warning("Codex OAuth keyring read failed, falling back to file: {}", exc)
            raw_blob = None
        if raw_blob:
            try:
                payload = CodexTokenPayload.from_raw(json.loads(raw_blob))
            except json.JSONDecodeError:
                payload = None
            if payload is not None:
                return payload
    payload = CodexTokenPayload.from_raw(_read_file())
    if payload is not None:
        return payload
    # Fallback: the Codex CLI's own store, so `codex login` is enough.
    return CodexTokenPayload.from_codex_auth_json(_read_codex_auth_json())


def _payload_source() -> str:
    """Which store backs the current credentials — for refresh write-back routing."""
    keyring = _try_keyring()
    if keyring is not None:
        try:
            if keyring.get_password(_KEYRING_SERVICE, _KEYRING_ACCOUNT):
                return "flowly"
        except Exception:
            pass
    if _read_file() is not None:
        return "flowly"
    if _read_codex_auth_json() is not None:
        return "codex_cli"
    return "none"


def save_token_payload(payload: CodexTokenPayload) -> str:
    raw = payload.to_raw()
    keyring = _try_keyring()
    if keyring is not None:
        try:
            keyring.set_password(
                _KEYRING_SERVICE, _KEYRING_ACCOUNT, json.dumps(raw, separators=(",", ":"))
            )
            try:
                credentials_path().unlink(missing_ok=True)
            except OSError:
                pass
            return _storage_status()
        except Exception as exc:
            logger.warning("Codex OAuth keyring write failed, falling back to file: {}", exc)
    _write_file(raw)
    return _storage_status()


def clear_token_payload() -> None:
    keyring = _try_keyring()
    if keyring is not None:
        try:
            keyring.delete_password(_KEYRING_SERVICE, _KEYRING_ACCOUNT)
        except Exception:
            pass
    try:
        credentials_path().unlink(missing_ok=True)
    except OSError:
        pass


# ── Token exchange / refresh ──────────────────────────────────────────


def _payload_from_token_response(
    body: dict[str, Any], fallback: CodexTokenPayload | None = None
) -> CodexTokenPayload:
    access = str(body.get("access_token") or "").strip()
    if not access:
        raise CodexAuthError("OpenAI token response did not return an access token")
    refresh = str(body.get("refresh_token") or (fallback.refresh_token if fallback else "")).strip()
    id_token = str(body.get("id_token") or (fallback.id_token if fallback else "")).strip()
    account_id = _account_id_from_token(id_token) or (fallback.account_id if fallback else "")
    return CodexTokenPayload(
        access_token=access,
        refresh_token=refresh,
        id_token=id_token,
        account_id=account_id,
        token_type=str(body.get("token_type") or "Bearer"),
        scope=str(body.get("scope") or (fallback.scope if fallback else "")),
        expires_at=_expires_at_from_token(access, body.get("expires_in")),
        email=_email_from_token(id_token, access) or (fallback.email if fallback else ""),
        plan=_plan_from_token(access, id_token) or (fallback.plan if fallback else ""),
    )


def exchange_code_for_tokens(
    *,
    code: str,
    client_id: str,
    code_verifier: str,
    redirect_uri: str = CODEX_OAUTH_REDIRECT_URI,
) -> CodexTokenPayload:
    data = {
        "grant_type": "authorization_code",
        "client_id": client_id,
        "code": code,
        "redirect_uri": redirect_uri,
        "code_verifier": code_verifier,
    }
    try:
        with httpx.Client(timeout=_HTTP_TIMEOUT) as client:
            response = client.post(
                CODEX_OAUTH_TOKEN_URL,
                data=data,
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/x-www-form-urlencoded",
                    "User-Agent": "flowly/codex-oauth",
                },
            )
    except httpx.HTTPError as exc:
        raise CodexAuthError(f"OpenAI token exchange failed: {type(exc).__name__}") from exc
    if response.status_code >= 400:
        raise CodexAuthError(
            f"OpenAI token exchange failed: HTTP {response.status_code} "
            f"{redact_secret(response.text[:300], code, code_verifier)}"
        )
    return _payload_from_token_response(response.json())


def refresh_tokens(
    *, client_id: str | None = None, payload: CodexTokenPayload | None = None
) -> CodexTokenPayload:
    current = payload or load_token_payload()
    if current is None or not current.refresh_token:
        raise CodexAuthError("Codex OAuth refresh token is missing; run `flowly codex login`.")
    client_id = client_id or require_client_id()
    data = {
        "grant_type": "refresh_token",
        "client_id": client_id,
        "refresh_token": current.refresh_token,
        "scope": CODEX_OAUTH_SCOPE,
    }
    try:
        with httpx.Client(timeout=_HTTP_TIMEOUT) as client:
            response = client.post(
                CODEX_OAUTH_TOKEN_URL,
                data=data,
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/x-www-form-urlencoded",
                    "User-Agent": "flowly/codex-oauth",
                },
            )
    except httpx.HTTPError as exc:
        raise CodexAuthError(f"Codex token refresh failed: {type(exc).__name__}") from exc
    if response.status_code in (400, 401):
        clear_token_payload()
        raise CodexAuthError("Codex OAuth refresh token was rejected; run `flowly codex login` again.")
    if response.status_code >= 400:
        raise CodexAuthError(f"Codex token refresh failed: HTTP {response.status_code}")
    updated = _payload_from_token_response(response.json(), fallback=current)
    # Route the write-back to whichever store the tokens came from so we
    # never split-brain the Codex CLI and Flowly.
    source = _payload_source()
    if source == "codex_cli":
        if not _write_codex_auth_json(updated):
            save_token_payload(updated)
    else:
        save_token_payload(updated)
    return updated


def resolve_runtime_credentials(
    *,
    config: Any | None = None,
    force_refresh: bool = False,
) -> CodexRuntimeCredentials | None:
    payload = load_token_payload()
    if payload is None:
        return None
    if force_refresh or token_is_expiring(payload):
        try:
            payload = refresh_tokens(client_id=require_client_id(config), payload=payload)
        except CodexAuthError:
            if force_refresh:
                raise
            # A soft-expiring token may still work; let the request try.
    if not payload.account_id:
        raise CodexEntitlementError(
            "Codex OAuth token is missing a ChatGPT account id. Sign in again "
            "with `flowly codex login`."
        )
    return CodexRuntimeCredentials(
        provider="openai_codex",
        api_key=payload.access_token,
        account_id=payload.account_id,
        base_url=CODEX_RESPONSES_BASE_URL,
        auth_mode="oauth_pkce",
        expires_at=payload.expires_at,
        email=payload.email,
        plan=payload.plan,
    )


# ── Browser (loopback) login ──────────────────────────────────────────


def build_authorize_url(*, client_id: str, code_challenge: str, state: str) -> str:
    query = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": CODEX_OAUTH_REDIRECT_URI,
        "scope": CODEX_OAUTH_SCOPE,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "id_token_add_organizations": "true",
        "codex_cli_simplified_flow": "true",
        "state": state,
        "originator": "flowly",
    }
    return f"{CODEX_OAUTH_AUTHORIZE_URL}?{urllib.parse.urlencode(query)}"


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

        def do_GET(self) -> None:  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path != CODEX_OAUTH_REDIRECT_PATH:
                self.send_error(404)
                return
            query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
            code = (query.get("code") or [""])[-1]
            error = (query.get("error") or [""])[-1]
            received_state = (query.get("state") or [""])[-1]
            callback_state.state_ok = bool(received_state) and secrets.compare_digest(
                received_state, callback_state.expected_state
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
                "<html><body><h2>Flowly ChatGPT login complete.</h2>"
                "<p>You can close this tab and return to Flowly.</p></body></html>"
                if ok
                else "<html><body><h2>Flowly ChatGPT login failed.</h2>"
                "<p>Return to Flowly for details.</p></body></html>"
            )
            self.send_response(200 if ok else 400)
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
) -> CodexTokenPayload:
    verifier = pkce_verifier()
    challenge = pkce_challenge(verifier)
    state = secrets.token_urlsafe(32)
    authorize_url = build_authorize_url(
        client_id=client_id, code_challenge=challenge, state=state
    )

    if manual_code:
        parsed = _parse_callback_input(manual_code)
        code = parsed.get("code", "")
        received_state = parsed.get("state", "")
        if received_state and not secrets.compare_digest(received_state, state):
            raise CodexAuthError("OAuth state mismatch in pasted callback URL")
        payload = exchange_code_for_tokens(
            code=code, client_id=client_id, code_verifier=verifier
        )
        save_token_payload(payload)
        return payload

    callback_state = _CallbackState(expected_state=state)
    try:
        server = ThreadingHTTPServer(
            (CODEX_OAUTH_REDIRECT_HOST, CODEX_OAUTH_REDIRECT_PORT),
            _make_callback_handler(callback_state),
        )
    except OSError as exc:
        raise CodexAuthError(
            f"Could not bind OAuth callback server on {CODEX_OAUTH_REDIRECT_URI}: {exc}. "
            "Retry with `flowly codex login --device` or `--manual-paste`."
        ) from exc

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        if on_authorize_url is not None:
            on_authorize_url(authorize_url)
        if not no_browser:
            webbrowser.open(authorize_url)
        logger.info(
            "Codex OAuth login started: browser_opened={} redirect_uri={} state_fingerprint={}",
            not no_browser,
            CODEX_OAUTH_REDIRECT_URI,
            hashlib.sha256(state.encode()).hexdigest()[:12],
        )
        if not callback_state.event.wait(timeout=max(1, int(timeout_seconds))):
            raise CodexAuthError("Timed out waiting for OpenAI OAuth callback")
        if callback_state.error:
            raise CodexAuthError(callback_state.error)
        payload = exchange_code_for_tokens(
            code=callback_state.code, client_id=client_id, code_verifier=verifier
        )
        save_token_payload(payload)
        return payload
    finally:
        server.shutdown()
        server.server_close()


# ── Device-code login (headless / no-browser) ─────────────────────────


def login_with_device_code(
    *,
    client_id: str,
    on_user_code: Any | None = None,
    timeout_seconds: int = 900,
) -> CodexTokenPayload:
    """Poll the device-code endpoints; return tokens once the user approves.

    ``on_user_code(user_code, verification_url)`` is invoked with the code
    the user must enter. Suited to the gateway / headless installs where no
    browser + loopback is available.
    """
    headers = {"Content-Type": "application/json", "User-Agent": "flowly/codex-device"}
    try:
        with httpx.Client(timeout=_HTTP_TIMEOUT) as client:
            resp = client.post(_DEVICE_CODE_URL, json={"client_id": client_id}, headers=headers)
    except httpx.HTTPError as exc:
        raise CodexAuthError(f"Device login could not start: {type(exc).__name__}") from exc
    if resp.status_code >= 400:
        raise CodexAuthError(f"Device login could not start: HTTP {resp.status_code}")
    data = resp.json()
    device_auth_id = data.get("device_auth_id")
    user_code = data.get("user_code") or data.get("usercode")
    try:
        interval = max(int(data.get("interval") or 5), 1)
    except (TypeError, ValueError):
        interval = 5
    if not isinstance(device_auth_id, str) or not isinstance(user_code, str):
        raise CodexAuthError("Device login response missing expected fields")

    if on_user_code is not None:
        on_user_code(user_code, _DEVICE_VERIFICATION_URL)

    deadline = time.monotonic() + max(60, int(timeout_seconds))
    while time.monotonic() < deadline:
        time.sleep(interval)
        try:
            with httpx.Client(timeout=_HTTP_TIMEOUT) as client:
                token_resp = client.post(
                    _DEVICE_TOKEN_URL,
                    json={"device_auth_id": device_auth_id, "user_code": user_code},
                    headers=headers,
                )
        except httpx.HTTPError:
            continue
        if token_resp.status_code in (403, 404):
            continue  # still pending
        if token_resp.status_code >= 400:
            raise CodexAuthError(f"Device login failed: HTTP {token_resp.status_code}")
        body = token_resp.json()
        authorization_code = body.get("authorization_code")
        code_verifier = body.get("code_verifier")
        if not isinstance(authorization_code, str) or not isinstance(code_verifier, str):
            raise CodexAuthError("Device login token response missing expected fields")
        payload = exchange_code_for_tokens(
            code=authorization_code,
            client_id=client_id,
            code_verifier=code_verifier,
            redirect_uri=f"{CODEX_OAUTH_ISSUER}/deviceauth/callback",
        )
        save_token_payload(payload)
        return payload

    raise CodexAuthError("Device login timed out")
