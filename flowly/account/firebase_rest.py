"""Account-token client — talks to the Flowly backend, never to Google directly.

Three calls, all proxied through ``${FLOWLY_API_BASE}/api/auth/*`` so this
open-source client ships **no Firebase key and no third-party auth coupling** —
the backend holds the Firebase web key and performs the actual Google exchange
server-side:

  • exchange: a device-code custom token → idToken + refreshToken (called once
    after device-code authorization).
  • refresh:  a refresh token → a fresh idToken (called when the current token
    is within RENEW_MARGIN_S of expiry).
  • account:  profile (email, …) for the holder of an idToken.

The endpoints return a normalized snake_case shape; the dataclass + error type
keep their names so callers (``auth.py``) are unchanged.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import httpx

# Same env + default as flowly/account/auth.py (read here too, not imported, to
# avoid a circular import). The base URL is public — no secret.
_API_BASE = os.environ.get("FLOWLY_API_BASE", "https://useflowlyapp.com").rstrip("/")


class FirebaseAuthError(Exception):
    """Raised when the auth backend rejects a request."""


@dataclass
class TokenBundle:
    id_token: str        # short-lived (1h) — used as Authorization bearer
    refresh_token: str   # long-lived — exchanged for fresh id_tokens
    expires_at: float    # unix seconds — when id_token stops working
    user_id: str         # Firebase UID
    email: str | None = None


async def sign_in_with_custom_token(custom_token: str) -> TokenBundle:
    """Exchange a server-issued custom token for an ID/refresh pair."""
    data = await _post("/api/auth/exchange", {"customToken": custom_token})
    return _bundle(data)


async def refresh_id_token(refresh_token: str) -> TokenBundle:
    """Exchange a refresh token for a fresh id_token."""
    data = await _post("/api/auth/refresh", {"refreshToken": refresh_token})
    # Backend may omit a rotated refresh token — keep the existing one.
    data.setdefault("refresh_token", refresh_token)
    return _bundle(data)


async def lookup_account(id_token: str) -> dict[str, Any]:
    """Fetch profile (email, displayName, …) for the holder of ``id_token``."""
    return await _post("/api/auth/account", {"idToken": id_token}, timeout=10.0)


async def _post(path: str, payload: dict[str, Any], *, timeout: float = 15.0) -> dict[str, Any]:
    url = f"{_API_BASE}{path}"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.post(url, json=payload)
    except httpx.HTTPError as exc:
        raise FirebaseAuthError(f"auth backend unreachable: {exc}") from exc
    if r.status_code != 200:
        raise FirebaseAuthError(_error_message(_safe_json(r), r.status_code))
    return _safe_json(r)


def _bundle(data: dict[str, Any]) -> TokenBundle:
    return TokenBundle(
        id_token=data["id_token"],
        refresh_token=data["refresh_token"],
        expires_at=_now() + int(data.get("expires_in") or 3600),
        user_id=data.get("user_id", ""),
        email=data.get("email"),
    )


def _safe_json(r: httpx.Response) -> dict[str, Any]:
    try:
        body = r.json()
    except ValueError:
        return {"error": (r.text or "")[:200]}
    return body if isinstance(body, dict) else {"error": str(body)}


def _error_message(body: dict[str, Any], status: int) -> str:
    err = body.get("error") if isinstance(body, dict) else None
    if isinstance(err, dict):
        return f"auth {status}: {err.get('message', 'unknown')}"
    return f"auth {status}: {err if err else body!r}"


def _now() -> float:
    import time
    return time.time()
