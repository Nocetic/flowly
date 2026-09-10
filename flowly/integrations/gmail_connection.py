"""Profile-local Gmail setup shared by the CLI and authenticated management RPC.

Only this runtime receives the Flowly grant secret. Clients see an authorization
URL and a confirmation code, never Google tokens or the web client's secret.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
from filelock import FileLock

from flowly.profile import get_active_profile_name, get_flowly_home

BROKER_ORIGIN = "https://useflowlyapp.com"
BROKER_API = BROKER_ORIGIN + "/api/gmail/broker"
MANAGED_MODE = "flowly_broker"
MAX_BODY = 64 * 1024
_ID = re.compile(r"^[a-f0-9]{32}$")
_SECRET = re.compile(r"^[A-Za-z0-9_-]{43}$")
_TERMINAL = {"expired", "cancelled", "revoked", "failed", "reauthorize"}


class GmailConnectionError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _private(path: Path, directory: bool = False) -> None:
    if os.name == "nt":
        user = os.environ.get("USERNAME", "")
        domain = os.environ.get("USERDOMAIN", "")
        if not user:
            raise GmailConnectionError("SECURE_STORAGE_UNAVAILABLE")
        principal = f"{domain}\\{user}" if domain else user
        try:
            subprocess.run(
                ["icacls", str(path), "/inheritance:r", "/grant:r", f"{principal}:F"],
                check=True, capture_output=True, timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            raise GmailConnectionError("SECURE_STORAGE_UNAVAILABLE") from None
    else:
        path.chmod(0o700 if directory else 0o600)


def atomic_private_json(path: Path, value: dict[str, Any]) -> None:
    """Create private storage before writing secrets, then atomically replace."""
    if path.is_symlink():
        raise GmailConnectionError("SECURE_STORAGE_UNAVAILABLE")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _private(path.parent, directory=True)
    fd, temporary = tempfile.mkstemp(prefix=".gmail-", dir=path.parent)
    temp = Path(temporary)
    try:
        _private(temp)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            fd = -1
            json.dump(value, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if fd != -1:
            os.close(fd)
        temp.unlink(missing_ok=True)


def _read(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    if path.is_symlink() or path.stat().st_size > MAX_BODY:
        raise GmailConnectionError("INVALID_LOCAL_STATE")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError()
        return value
    except (ValueError, OSError):
        raise GmailConnectionError("INVALID_LOCAL_STATE") from None


def is_managed_credentials(value: dict[str, Any]) -> bool:
    return bool(
        value.get("mode") == MANAGED_MODE
        and value.get("issuer") == BROKER_ORIGIN
        and isinstance(value.get("grant_id"), str) and _ID.fullmatch(value["grant_id"])
        and isinstance(value.get("grant_secret"), str) and _SECRET.fullmatch(value["grant_secret"])
    )


class GmailConnection:
    def __init__(self, home: Path | None = None, *, client: httpx.Client | None = None):
        self.home = home if home is not None else get_flowly_home()
        self.credentials = self.home / "credentials" / "gmail.json"
        self.pending = self.home / "credentials" / "gmail-setup.json"
        self._client = client

    def _lock(self) -> FileLock:
        self.credentials.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        _private(self.credentials.parent, directory=True)
        return FileLock(str(self.credentials.parent / ".gmail.lock"), timeout=40)

    def _request(self, method: str, url: str, *, secret: str | None = None, body: dict | None = None) -> dict:
        # URLs are constructed locally, never taken from an RPC payload or remote response.
        headers = {"Accept": "application/json"}
        if secret:
            headers["Authorization"] = f"Bearer {secret}"
        client = self._client or httpx.Client(timeout=20, follow_redirects=False)
        try:
            with client.stream(method, url, headers=headers, json=body, timeout=20, follow_redirects=False) as response:
                chunks = bytearray()
                for chunk in response.iter_bytes():
                    chunks.extend(chunk)
                    if len(chunks) > MAX_BODY:
                        raise GmailConnectionError("INVALID_RESPONSE")
                try:
                    data = json.loads(chunks)
                except (ValueError, UnicodeError):
                    raise GmailConnectionError("UNAVAILABLE") from None
                if not isinstance(data, dict):
                    raise GmailConnectionError("INVALID_RESPONSE")
                if response.status_code != 200:
                    code = data.get("error", {}).get("code") if isinstance(data.get("error"), dict) else None
                    allowed = {"REAUTHORIZE", "EXPIRED", "NOT_FOUND", "UNAUTHORIZED", "RATE_LIMITED", "RETRY_LATER"}
                    raise GmailConnectionError(code if code in allowed else "UNAVAILABLE")
                return data
        except (httpx.HTTPError, OSError):
            raise GmailConnectionError("UNAVAILABLE") from None
        finally:
            if self._client is None:
                client.close()

    def _grant_request(self, grant: dict, action: str | None = None) -> dict:
        if not is_managed_credentials(grant):
            raise GmailConnectionError("INVALID_LOCAL_STATE")
        return self._request(
            "POST" if action else "GET", f"{BROKER_API}/{grant['grant_id']}",
            secret=grant["grant_secret"], body={"action": action} if action else None,
        )

    @staticmethod
    def _public_setup(grant: dict, status: str = "pending") -> dict:
        return {
            "requestId": grant["grant_id"], "status": status,
            "authorizationUrl": grant["authorization_url"],
            "verificationCode": grant["verification_code"], "expiresAt": grant["expires_at"],
            "label": grant["label"], "profile": grant["profile"], "interval": 5,
        }

    def begin(self, *, locale: str = "en", label: str | None = None) -> dict:
        if locale not in {"en", "tr", "es"}:
            raise GmailConnectionError("INVALID_PARAMS")
        with self._lock():
            existing = _read(self.pending)
            credentials = _read(self.credentials)
            saved_same_grant = bool(existing and credentials and credentials.get("grant_id") == existing.get("grant_id"))
            if existing and (existing.get("expires_at", 0) > time.time() * 1000 or saved_same_grant):
                if not is_managed_credentials(existing):
                    raise GmailConnectionError("INVALID_LOCAL_STATE")
                return self._public_setup(existing)
            if existing:
                # Keep the revocation credential until an expired request is closed remotely.
                try:
                    self._grant_request(existing, "disconnect")
                except GmailConnectionError as error:
                    if error.code not in {"NOT_FOUND", "EXPIRED"}:
                        raise
                self.pending.unlink(missing_ok=True)
            if _read(self.credentials):
                # Never silently overwrite a legacy connection or change Google accounts.
                raise GmailConnectionError("ALREADY_CONFIGURED")
            target_label = (label or socket.gethostname())[:100]
            profile = get_active_profile_name()
            data = self._request("POST", BROKER_API, body={"label": target_label, "profile": profile, "locale": locale})
            grant = {
                "mode": MANAGED_MODE, "issuer": BROKER_ORIGIN,
                "grant_id": data.get("requestId"), "grant_secret": data.get("secret"),
                "authorization_url": data.get("authorizationUrl"), "verification_code": data.get("verificationCode"),
                "expires_at": data.get("expiresAt"), "label": target_label, "profile": profile,
            }
            if not is_managed_credentials(grant):
                raise GmailConnectionError("INVALID_RESPONSE")
            url = urlparse(str(grant["authorization_url"]))
            if (url.scheme != "https" or url.netloc != "useflowlyapp.com"
                    or url.path != f"/{locale}/gmail/connect" or url.fragment
                    or parse_qs(url.query) != {"request": [grant["grant_id"]]}):
                raise GmailConnectionError("INVALID_RESPONSE")
            if (not isinstance(grant["verification_code"], str)
                    or not re.fullmatch(r"[A-F0-9]{8}", grant["verification_code"])
                    or not isinstance(grant["expires_at"], (int, float))
                    or not time.time() * 1000 < grant["expires_at"] <= (time.time() + 16 * 60) * 1000):
                raise GmailConnectionError("INVALID_RESPONSE")
            atomic_private_json(self.pending, grant)
            return self._public_setup(grant)

    def _require_pending(self, request_id: str) -> dict:
        grant = _read(self.pending)
        if not grant or grant.get("grant_id") != request_id:
            raise GmailConnectionError("SETUP_NOT_FOUND")
        if not is_managed_credentials(grant):
            raise GmailConnectionError("INVALID_LOCAL_STATE")
        return grant

    def setup_status(self, request_id: str) -> dict:
        with self._lock():
            # Reply loss after a successful local save is safe to retry.
            credentials = _read(self.credentials)
            if credentials and credentials.get("grant_id") == request_id and not self.pending.exists():
                return {"requestId": request_id, **self._status_locked(credentials, verify=True)}
            grant = self._require_pending(request_id)
            # A saved grant may still need the config write after a process or
            # disk failure. Its already-claimed authorization outlives setup TTL.
            saved_same_grant = bool(credentials and credentials.get("grant_id") == request_id)
            if saved_same_grant and credentials.get("disconnect_pending"):
                return self._public_setup(grant, "cancelled")
            if grant["expires_at"] <= time.time() * 1000 and not saved_same_grant:
                return self._public_setup(grant, "expired")
            status = self._grant_request(grant).get("status")
            if status in {"authorized", "active"}:
                if credentials and credentials.get("grant_id") != request_id:
                    raise GmailConnectionError("ALREADY_CONFIGURED")
                tokens = self._token(grant)
                self._verify_gmail(tokens)
                saved = {**grant, **tokens}
                atomic_private_json(self.credentials, saved)
                self._enable_email()
                self.pending.unlink(missing_ok=True)
                return {"requestId": request_id, "status": "connected", "connected": True, "email": tokens["email"]}
            if status not in {"pending", "authorizing", "exchanging", *_TERMINAL}:
                raise GmailConnectionError("INVALID_RESPONSE")
            result = self._public_setup(grant, status)
            if status in {"cancelled", "revoked", "failed", "reauthorize"}:
                self.pending.unlink(missing_ok=True)
            return result

    def _enable_email(self) -> None:
        # Read/modify only the email card; never rewrite provider/model or enable an inbound channel.
        from flowly.config.loader import get_config_path
        from flowly.integrations.config_io import apply_card_values, read_card_values
        from flowly.integrations.registry import get_card
        if get_config_path().resolve() != (self.home / "config.json").resolve():
            raise GmailConnectionError("PROFILE_CHANGED")
        card = get_card("email")
        if card is None:
            raise GmailConnectionError("UNAVAILABLE")
        values = read_card_values(card)
        values["enabled"] = True
        apply_card_values(card, values)

    def _token(self, grant: dict) -> dict:
        data = self._grant_request(grant, "token")
        access = data.get("accessToken")
        expiry = data.get("expiresAt")
        email = data.get("email")
        if (not isinstance(access, str) or not access or len(access) > 16384
                or not isinstance(expiry, (int, float)) or not time.time() * 1000 < expiry <= (time.time() + 86400) * 1000
                or not isinstance(email, str) or len(email) > 254 or not re.fullmatch(r"[^\s@]+@[^\s@]+", email)):
            raise GmailConnectionError("INVALID_RESPONSE")
        return {"access_token": access, "expiry": datetime.fromtimestamp(expiry / 1000, timezone.utc).isoformat(), "email": email, "scopes": data.get("scope", "")}

    def _verify_gmail(self, tokens: dict) -> None:
        data = self._request("GET", "https://gmail.googleapis.com/gmail/v1/users/me/profile", secret=tokens["access_token"])
        if data.get("emailAddress") != tokens["email"]:
            raise GmailConnectionError("GMAIL_ACCOUNT_MISMATCH")

    def cancel(self, request_id: str) -> dict:
        with self._lock():
            grant = self._require_pending(request_id)
            credentials = _read(self.credentials)
            saved_same_grant = bool(credentials and credentials.get("grant_id") == request_id)
            if saved_same_grant:
                # A config write may have failed after credentials were saved.
                # Cancellation must also stop that partial local connection.
                credentials["disconnect_pending"] = True
                credentials.pop("access_token", None)
                atomic_private_json(self.credentials, credentials)
            self._grant_request(grant, "disconnect")
            if saved_same_grant:
                self.credentials.unlink(missing_ok=True)
            self.pending.unlink(missing_ok=True)
            return {"requestId": request_id, "status": "cancelled"}

    def pending_setup(self) -> dict | None:
        with self._lock():
            grant = _read(self.pending)
            if grant is None:
                return None
            if not is_managed_credentials(grant):
                raise GmailConnectionError("INVALID_LOCAL_STATE")
            credentials = _read(self.credentials)
            saved_same_grant = bool(credentials and credentials.get("grant_id") == grant["grant_id"])
            return self._public_setup(grant, "expired" if grant["expires_at"] <= time.time() * 1000 and not saved_same_grant else "pending")

    @staticmethod
    def connection_id(credentials: dict) -> str:
        if is_managed_credentials(credentials):
            return credentials["grant_id"]
        return hashlib.sha256(str(credentials.get("refresh_token", "")).encode()).hexdigest()[:32]

    def _status_locked(self, credentials: dict | None, *, verify: bool) -> dict:
        if not credentials:
            return {"status": "not_configured", "connected": False}
        result = {"connectionId": self.connection_id(credentials), "email": credentials.get("email"), "mode": "managed" if is_managed_credentials(credentials) else "legacy"}
        if credentials.get("disconnect_pending"):
            return {**result, "status": "disconnect_pending", "connected": False}
        if not verify:
            return {**result, "status": "saved", "connected": False}
        try:
            if is_managed_credentials(credentials):
                tokens = self._token(credentials)
                self._verify_gmail(tokens)
                credentials.pop("reauthorize_required", None)
                atomic_private_json(self.credentials, {**credentials, **tokens})
            else:
                from flowly.channels.gmail_auth import get_valid_access_token
                token, email = get_valid_access_token()
                if not token:
                    raise GmailConnectionError("REAUTHORIZE")
                self._verify_gmail({"access_token": token, "email": email})
            return {**result, "status": "connected", "connected": True}
        except GmailConnectionError as error:
            if is_managed_credentials(credentials) and error.code in {"UNAUTHORIZED", "REAUTHORIZE", "NOT_FOUND"}:
                credentials["reauthorize_required"] = True
                credentials.pop("access_token", None)
                atomic_private_json(self.credentials, credentials)
            return {**result, "status": "reauthorize" if error.code in {"UNAUTHORIZED", "REAUTHORIZE", "NOT_FOUND"} else "unavailable", "connected": False, "error": {"code": error.code}}

    def status(self, *, verify: bool = True) -> dict:
        with self._lock():
            return self._status_locked(_read(self.credentials), verify=verify)

    def disconnect(self, connection_id: str) -> dict:
        with self._lock():
            credentials = _read(self.credentials)
            if not credentials or self.connection_id(credentials) != connection_id:
                raise GmailConnectionError("CONNECTION_CHANGED")
            if is_managed_credentials(credentials):
                # Stop local use immediately, even if the remote revocation must be retried.
                credentials["disconnect_pending"] = True
                credentials.pop("access_token", None)
                atomic_private_json(self.credentials, credentials)
                try:
                    self._grant_request(credentials, "disconnect")
                except GmailConnectionError as error:
                    if error.code not in {"NOT_FOUND", "REAUTHORIZE"}:
                        return {"connected": False, "status": "disconnect_pending", "connectionId": connection_id}
            self.credentials.unlink(missing_ok=True)
            pending = _read(self.pending)
            if pending and pending.get("grant_id") == connection_id:
                self.pending.unlink(missing_ok=True)
            return {"connected": False, "status": "not_configured"}

    def access_token(self) -> tuple[str | None, str | None]:
        with self._lock():
            credentials = _read(self.credentials)
            if not credentials or not is_managed_credentials(credentials) or credentials.get("disconnect_pending") or credentials.get("reauthorize_required"):
                return None, None
            from flowly.channels.gmail_auth import _is_expired
            if _is_expired(credentials):
                tokens = self._token(credentials)
                if tokens["email"] != credentials.get("email"):
                    raise GmailConnectionError("GMAIL_ACCOUNT_MISMATCH")
                credentials.update(tokens)
                atomic_private_json(self.credentials, credentials)
            return credentials.get("access_token"), credentials.get("email")
