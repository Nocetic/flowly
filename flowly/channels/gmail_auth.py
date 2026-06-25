"""Gmail OAuth token management.

Loads, saves, and refreshes OAuth 2.0 tokens stored at
``~/.flowly/credentials/gmail.json``.  Tokens are obtained via the
web app OAuth flow — the bot never sees the user's password.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import httpx
from loguru import logger

from flowly.profile import get_flowly_home

_TOKEN_URI = "https://oauth2.googleapis.com/token"
_REFRESH_BUFFER_SECONDS = 300  # refresh 5 min before expiry


def _creds_path() -> Path:
    return get_flowly_home() / "credentials" / "gmail.json"


def load_credentials() -> dict[str, Any] | None:
    """Load Gmail OAuth credentials from disk.  Returns None if missing."""
    path = _creds_path()
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not data.get("refresh_token"):
            logger.warning("[Gmail] Credentials missing refresh_token")
            return None
        return data
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"[Gmail] Failed to load credentials: {e}")
        return None


def save_credentials(creds: dict[str, Any]) -> None:
    """Save Gmail OAuth credentials to disk (mode 0600)."""
    path = _creds_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(creds, indent=2), encoding="utf-8")
    try:
        from flowly.utils.file_security import secure_file
        secure_file(path)  # POSIX chmod; real owner-only ACL on Windows
    except OSError:
        pass


def _is_expired(creds: dict[str, Any]) -> bool:
    """Check if the access token is expired or about to expire."""
    expiry = creds.get("expiry")
    if not expiry:
        return True
    try:
        from datetime import datetime, timezone
        exp_dt = datetime.fromisoformat(expiry.replace("Z", "+00:00"))
        return exp_dt.timestamp() - time.time() < _REFRESH_BUFFER_SECONDS
    except (ValueError, TypeError):
        return True


def _refresh(creds: dict[str, Any]) -> dict[str, Any] | None:
    """Refresh the access token using the refresh token."""
    refresh_token = creds.get("refresh_token")
    client_id = creds.get("client_id")
    client_secret = creds.get("client_secret")

    if not (refresh_token and client_id and client_secret):
        logger.error("[Gmail] Cannot refresh — missing client_id/client_secret/refresh_token")
        return None

    try:
        resp = httpx.post(
            _TOKEN_URI,
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": client_id,
                "client_secret": client_secret,
            },
            timeout=15,
        )
        if resp.status_code != 200:
            logger.error(f"[Gmail] Token refresh failed ({resp.status_code}): {resp.text[:200]}")
            return None

        data = resp.json()
        from datetime import datetime, timezone, timedelta
        expiry = datetime.now(timezone.utc) + timedelta(seconds=data.get("expires_in", 3600))

        creds["access_token"] = data["access_token"]
        creds["expiry"] = expiry.isoformat()
        # Google may issue a new refresh token
        if data.get("refresh_token"):
            creds["refresh_token"] = data["refresh_token"]

        save_credentials(creds)
        logger.debug("[Gmail] Access token refreshed")
        return creds
    except Exception as e:
        logger.error(f"[Gmail] Token refresh error: {e}")
        return None


def get_valid_access_token() -> tuple[str | None, str | None]:
    """Return (access_token, email) with a valid (non-expired) token.

    Automatically refreshes if needed.  Returns (None, None) on failure.
    """
    creds = load_credentials()
    if not creds:
        return None, None

    if _is_expired(creds):
        creds = _refresh(creds)
        if not creds:
            return None, None

    return creds.get("access_token"), creds.get("email")
