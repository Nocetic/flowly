"""End-to-end auth: device-code flow → Firebase token → on-disk storage.

The TUI's only entry point: ``run_login_flow()`` returns an ``Account``
once the user completes the browser handshake. ``load_account()`` reads
the saved credentials and refreshes the id_token if it's about to expire.

Storage layout
--------------
``~/.flowly/credentials/account.json`` (file mode 0600):

    {
      "user_id": "abc",
      "email":   "user@example.com",
      "id_token":      "...",
      "refresh_token": "...",
      "expires_at":    1716800000,
      "machine_id":    "<sha256-prefix>",
      "machine_name":  "MacBook Pro",
      "device_id":     "<from device-code POST>"
    }
"""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import asdict, dataclass
from typing import Any, Callable

import httpx

from flowly.account.fingerprint import machine_id, machine_name
from flowly.account.firebase_rest import (
    FirebaseAuthError,
    TokenBundle,
    lookup_account,
    refresh_id_token,
    sign_in_with_custom_token,
)
from flowly.account import audit_log
from flowly.account.token_store import (
    clear_credentials,
    load_credentials,
    save_credentials,
    storage_status,
)
RENEW_MARGIN_S = 600  # refresh when ID token has ≤ 10 min left

# Production Flowly API base. Override via FLOWLY_API_BASE for local dev.
FLOWLY_API_BASE = os.environ.get("FLOWLY_API_BASE", "https://useflowlyapp.com")

DEVICE_CODE_URL = f"{FLOWLY_API_BASE}/api/auth/device-code"
# CLI-dedicated authorization page (auto-submits when user is logged in,
# auto-closes the tab after success). Falls back to /auth/desktop if the
# CLI page returns 404 in an old deployment.
DEVICE_AUTH_URL = f"{FLOWLY_API_BASE}/auth/cli"
DEVICE_AUTH_URL_FALLBACK = f"{FLOWLY_API_BASE}/auth/desktop"
POLL_INTERVAL_S = 2.0
POLL_TIMEOUT_S = 5 * 60  # device-code expires in 5 min


class LoginCancelled(Exception):
    pass


class LoginTimeout(Exception):
    pass


@dataclass
class Account:
    user_id: str
    email: str | None
    id_token: str
    refresh_token: str
    expires_at: float
    machine_id: str
    machine_name: str
    device_id: str = ""
    # Server registration — populated after POST /api/servers (Phase 2).
    # When empty, the user is logged in but the machine isn't yet bound
    # to a Firestore server entry (transient state during/after a login
    # where the register call failed; retried opportunistically).
    server_id: str = ""
    server_name: str = ""
    gateway_auth_token: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Account":
        return cls(
            user_id=data["user_id"],
            email=data.get("email"),
            id_token=data["id_token"],
            refresh_token=data["refresh_token"],
            expires_at=float(data["expires_at"]),
            machine_id=data.get("machine_id") or machine_id(),
            machine_name=data.get("machine_name") or machine_name(),
            device_id=data.get("device_id", ""),
            server_id=data.get("server_id", ""),
            server_name=data.get("server_name", ""),
            gateway_auth_token=data.get("gateway_auth_token", ""),
        )

    def needs_refresh(self) -> bool:
        return self.expires_at - time.time() < RENEW_MARGIN_S


# ── on-disk storage ───────────────────────────────────────────────


def save_account(account: Account) -> None:
    """Persist account credentials via OS keychain (preferred) or file fallback."""
    save_credentials(account.to_dict())


def load_account_sync() -> Account | None:
    """Load saved account (transparent keychain / legacy file migration)."""
    data = load_credentials()
    if not data:
        return None
    try:
        return Account.from_dict(data)
    except (KeyError, ValueError, TypeError):
        return None


def clear_account() -> None:
    """Remove account credentials from keychain AND any legacy file."""
    existing = load_account_sync()
    clear_credentials()
    audit_log.info("logout.cleared",
                   had_account=existing is not None,
                   user_id=existing.user_id if existing else None)


def credential_storage_status() -> str:
    """Human-readable backend description for the /whoami output."""
    s = storage_status()
    lock = "🔒" if s.secure else "⚠"
    return f"{lock} {s.detail}"


# ── auth flows ────────────────────────────────────────────────────


async def run_login_flow(
    *,
    on_code: Callable[[str, str], None] | None = None,
    on_status: Callable[[str], None] | None = None,
) -> Account:
    """Drive the device-code handshake until the user completes it.

    ``on_code(code, url)`` is invoked once with the user-visible code and
    the authorization URL. ``on_status(msg)`` is called as the polling
    state changes ("waiting", "authorized", "fetching profile", …).
    """
    cid = audit_log.new_correlation_id()
    audit_log.info("login.flow.started",
                   machine_id=machine_id(),
                   machine_name=machine_name())

    async with httpx.AsyncClient(timeout=15.0) as client:
        # 1. Request a device code.
        if on_status: on_status("requesting code")
        audit_log.debug("login.device_code.request", url=DEVICE_CODE_URL)
        r = await client.post(
            DEVICE_CODE_URL,
            json={"deviceName": machine_name()},
        )
        if r.status_code != 200:
            audit_log.error("login.device_code.failed", status=r.status_code)
            raise FirebaseAuthError(f"device-code request failed: {r.status_code} {r.text[:200]}")
        body = r.json()
        code = body["code"]
        device_id = body["deviceId"]
        audit_log.info("login.device_code.issued", code=code, device_id=device_id)
        # Build a one-click URL: code + device name embedded. The
        # /auth/cli page auto-submits as soon as the user is signed in,
        # so the entire UX is "open URL → done" for logged-in users.
        from urllib.parse import quote
        prefilled_url = (
            f"{DEVICE_AUTH_URL}?code={code}&device={quote(machine_name())}"
        )
        if on_code:
            on_code(code, prefilled_url)

        # 2. Poll until status flips to "authorized" or expires.
        if on_status: on_status("waiting for authorization")
        custom_token = await _poll_for_token(client, code, device_id)

    # 3. Exchange custom token for ID + refresh tokens.
    if on_status: on_status("signing in")
    audit_log.info("login.custom_token.received",
                   token=audit_log.safe_token_summary(custom_token))
    tokens = await sign_in_with_custom_token(custom_token)
    audit_log.info("login.id_token.minted",
                   user_id=tokens.user_id,
                   id_token=audit_log.safe_token_summary(tokens.id_token),
                   expires_in=int(tokens.expires_at - time.time()))

    # 4. Fetch profile (email).
    if on_status: on_status("fetching profile")
    profile = await lookup_account(tokens.id_token)
    email = profile.get("email") or tokens.email

    account = Account(
        user_id=tokens.user_id,
        email=email,
        id_token=tokens.id_token,
        refresh_token=tokens.refresh_token,
        expires_at=tokens.expires_at,
        machine_id=machine_id(),
        machine_name=machine_name(),
        device_id=device_id,
    )
    save_account(account)
    audit_log.info("login.flow.success",
                   user_id=tokens.user_id,
                   email=email,
                   storage=storage_status().detail)
    if on_status: on_status("done")
    return account


async def _poll_for_token(
    client: httpx.AsyncClient, code: str, device_id: str
) -> str:
    """Poll GET /api/auth/device-code until authorized or expired."""
    deadline = time.monotonic() + POLL_TIMEOUT_S
    while time.monotonic() < deadline:
        r = await client.get(
            DEVICE_CODE_URL, params={"code": code, "deviceId": device_id}
        )
        body: dict[str, Any] = {}
        try:
            body = r.json()
        except ValueError:
            pass

        status = body.get("status", "")
        if status == "authorized" and body.get("token"):
            return str(body["token"])
        if status == "expired" or r.status_code in (403, 404):
            raise LoginTimeout(f"device code {code} expired")
        # status == "pending" or anything else → keep polling.
        await asyncio.sleep(POLL_INTERVAL_S)

    raise LoginTimeout(f"device code {code} expired after {POLL_TIMEOUT_S}s")


async def load_account_refreshing() -> Account | None:
    """Load saved account, refreshing the id_token if it's near expiry.

    Returns ``None`` if no account is saved or refresh fails (caller
    should treat as logged-out).
    """
    acc = load_account_sync()
    if acc is None:
        return None
    if not acc.needs_refresh():
        return acc

    try:
        bundle = await refresh_id_token(acc.refresh_token)
    except FirebaseAuthError as exc:
        # Refresh token revoked / invalid — caller should re-login.
        audit_log.warn("token.refresh.failed", user_id=acc.user_id, reason=str(exc))
        return None

    acc.id_token = bundle.id_token
    acc.refresh_token = bundle.refresh_token
    acc.expires_at = bundle.expires_at
    save_account(acc)
    audit_log.info("token.refreshed",
                   user_id=acc.user_id,
                   expires_in=int(acc.expires_at - time.time()))
    return acc


# ── background refresh task ───────────────────────────────────────


async def background_refresh_loop(account_ref: list[Account | None]) -> None:
    """Long-running task: refresh id_token a few minutes before it expires.

    ``account_ref`` is a 1-element list used as a mutable holder so the
    refreshed account propagates back to the caller without callbacks.
    """
    while True:
        acc = account_ref[0]
        if acc is None:
            await asyncio.sleep(60)
            continue

        sleep_s = max(60.0, acc.expires_at - time.time() - RENEW_MARGIN_S)
        await asyncio.sleep(sleep_s)

        try:
            bundle = await refresh_id_token(acc.refresh_token)
        except FirebaseAuthError:
            # Revoked or network — try again in a minute.
            await asyncio.sleep(60)
            continue

        acc.id_token = bundle.id_token
        acc.refresh_token = bundle.refresh_token
        acc.expires_at = bundle.expires_at
        save_account(acc)
        account_ref[0] = acc
