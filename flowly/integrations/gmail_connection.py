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
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
from filelock import FileLock

from flowly.integrations.google_permissions import (
    CONNECTION_SERVICES,
    granted_services,
    normalize_services,
    service_permissions,
)
from flowly.profile import get_active_profile_name, get_flowly_home

BROKER_ORIGIN = "https://useflowlyapp.com"
BROKER_API = BROKER_ORIGIN + "/api/gmail/broker"
MANAGED_MODE = "flowly_broker"
MAX_BODY = 64 * 1024
_ID = re.compile(r"^[a-f0-9]{32}$")
_SECRET = re.compile(r"^[A-Za-z0-9_-]{43}$")
_TERMINAL = {"expired", "cancelled", "revoked", "failed", "reauthorize"}
_LOCK_CREATION = threading.Lock()


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
    def __init__(self, home: Path | None = None, *, client: httpx.Client | None = None, service: str | None = None):
        if service is not None and service not in CONNECTION_SERVICES:
            raise GmailConnectionError("INVALID_SERVICE")
        self.service = service
        self.home = home if home is not None else get_flowly_home()
        self.credentials = self.home / "credentials" / "gmail.json"
        self.pending = self.home / "credentials" / "gmail-setup.json"
        if service is not None:
            self.credentials = self.home / "credentials" / f"google-{service}.json"
            self.pending = self.home / "credentials" / f"google-{service}-setup.json"
        # Construct the reentrant singleton atomically across parallel RPC workers.
        with _LOCK_CREATION:
            self._file_lock = FileLock(str(self.credentials.parent / ".gmail.lock"), timeout=40, is_singleton=True)
        self._client = client

    def _lock(self) -> FileLock:
        self.credentials.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        _private(self.credentials.parent, directory=True)
        return self._file_lock

    def _read_credentials(self) -> dict | None:
        own = _read(self.credentials)
        if own is not None or self.service is None:
            return own
        shared = _read(self.home / "credentials" / "gmail.json")
        if not shared or self.service in shared.get("disabled_services", []):
            return None
        actual = granted_services(shared)
        # Historical native credentials omitted scopes; keep their compatibility
        # path until the user explicitly reconnects or disconnects each service.
        if self.service not in actual and not (not shared.get("scopes", shared.get("scope")) and (self.service == "gmail" or shared.get("mode") != MANAGED_MODE)):
            return None
        return {**shared, "_shared": True, "_shared_service": self.service}

    def _save_credentials(self, value: dict) -> None:
        target = self.home / "credentials" / "gmail.json" if value.get("_shared") else self.credentials
        atomic_private_json(target, {key: item for key, item in value.items() if not key.startswith("_shared")})

    def _detach_shared(self, credentials: dict) -> bool:
        """Remove this service from an old bundle, preserving every other service."""
        path = self.home / "credentials" / "gmail.json"
        shared = _read(path)
        if not shared or self.connection_id(shared) != self.connection_id(credentials):
            return True
        shared["disabled_services"] = sorted(set(shared.get("disabled_services", [])) | {self.service})
        atomic_private_json(path, shared)
        unknown_native = shared.get("mode") != MANAGED_MODE and not shared.get("scopes", shared.get("scope"))
        if granted_services(shared) or (unknown_native and set(CONNECTION_SERVICES) - set(shared["disabled_services"])):
            return True
        if is_managed_credentials(shared):
            try:
                self._grant_request(shared, "disconnect")
            except GmailConnectionError as error:
                if error.code not in {"NOT_FOUND", "REAUTHORIZE", "EXPIRED"}:
                    return False
        path.unlink(missing_ok=True)
        return True

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
            "services": grant.get("services", ["gmail"]),
            **({"service": grant["connection_service"]} if grant.get("connection_service") else {}),
            "replacingConnectionId": grant.get("replacing_connection_id"),
        }

    def begin(self, *, locale: str = "en", label: str | None = None, services=None, connection_id: str | None = None) -> dict:
        if locale not in {"en", "tr", "es"}:
            raise GmailConnectionError("INVALID_PARAMS")
        try:
            selected = normalize_services(services if services is not None else (service_permissions(self.service) if self.service else None), require_gmail=self.service is None)
            if self.service and (self.service not in selected or set(selected) - set(service_permissions(self.service))):
                raise ValueError("INVALID_SERVICES")
        except ValueError:
            raise GmailConnectionError("INVALID_SERVICES") from None
        with self._lock():
            existing = _read(self.pending)
            credentials = self._read_credentials()
            saved_same_grant = bool(existing and credentials and credentials.get("grant_id") == existing.get("grant_id"))
            if existing and (existing.get("expires_at", 0) > time.time() * 1000 or saved_same_grant):
                if not is_managed_credentials(existing):
                    raise GmailConnectionError("INVALID_LOCAL_STATE")
                if services is not None and existing.get("services", ["gmail"]) != selected:
                    raise GmailConnectionError("SETUP_IN_PROGRESS")
                if connection_id and existing.get("replacing_connection_id") != connection_id:
                    raise GmailConnectionError("CONNECTION_CHANGED")
                return self._public_setup(existing)
            if existing:
                # Keep the revocation credential until an expired request is closed remotely.
                try:
                    self._grant_request(existing, "disconnect")
                except GmailConnectionError as error:
                    if error.code not in {"NOT_FOUND", "EXPIRED"}:
                        raise
                self.pending.unlink(missing_ok=True)
            if credentials:
                if not connection_id:
                    raise GmailConnectionError("ALREADY_CONFIGURED")
                if self.connection_id(credentials) != connection_id or credentials.get("disconnect_pending"):
                    raise GmailConnectionError("CONNECTION_CHANGED")
                if not credentials.get("email"):
                    raise GmailConnectionError("REAUTHORIZE")
                selected = normalize_services(list(set(selected) | (set(granted_services(credentials)) & set(service_permissions(self.service)) if self.service else set(granted_services(credentials)))), require_gmail=self.service is None)
            elif connection_id:
                raise GmailConnectionError("CONNECTION_CHANGED")
            target_label = (label or socket.gethostname())[:100]
            profile = get_active_profile_name()
            data = self._request("POST", BROKER_API, body={"label": target_label, "profile": profile, "locale": locale, "services": selected})
            grant = {
                "mode": MANAGED_MODE, "issuer": BROKER_ORIGIN,
                "grant_id": data.get("requestId"), "grant_secret": data.get("secret"),
                "authorization_url": data.get("authorizationUrl"), "verification_code": data.get("verificationCode"),
                "expires_at": data.get("expiresAt"), "label": target_label, "profile": profile,
                "services": selected,
                "replacing_connection_id": connection_id,
                "expected_email": credentials.get("email") if credentials else None,
                "expected_subject": credentials.get("subject") if credentials else None,
                "connection_service": self.service,
                "superseded": credentials if credentials and (is_managed_credentials(credentials) or credentials.get("_shared")) else None,
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
            credentials = self._read_credentials()
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
                if not saved_same_grant:
                    replacing = grant.get("replacing_connection_id")
                    if replacing and (not credentials or self.connection_id(credentials) != replacing or credentials.get("disconnect_pending")):
                        raise GmailConnectionError("CONNECTION_CHANGED")
                    if credentials and not replacing:
                        raise GmailConnectionError("ALREADY_CONFIGURED")
                tokens = self._token(grant)
                self._verify_gmail(tokens)
                if self.service and self.service not in granted_services({**grant, **tokens}):
                    raise GmailConnectionError("PERMISSION_REQUIRED")
                if grant.get("expected_subject") and tokens.get("subject") != grant["expected_subject"]:
                    raise GmailConnectionError("GMAIL_ACCOUNT_MISMATCH")
                if grant.get("expected_email") and tokens["email"].casefold() != grant["expected_email"].casefold():
                    raise GmailConnectionError("GMAIL_ACCOUNT_MISMATCH")
                saved = {key: value for key, value in {**grant, **tokens}.items() if key != "superseded"}
                self._save_credentials(saved)
                self._enable_email()
                cleanup_pending = not self._retire_superseded(grant)
                if not cleanup_pending:
                    self.pending.unlink(missing_ok=True)
                return {"requestId": request_id, "status": "connected", "connected": True, "mode": "managed", "email": tokens["email"],
                        "services": granted_services(saved), "requestedServices": grant.get("services", ["gmail"]), "cleanupPending": cleanup_pending}
            if status not in {"pending", "authorizing", "exchanging", *_TERMINAL}:
                raise GmailConnectionError("INVALID_RESPONSE")
            result = self._public_setup(grant, status)
            if status in {"cancelled", "revoked", "failed", "reauthorize"}:
                self.pending.unlink(missing_ok=True)
            return result

    def _retire_superseded(self, grant: dict) -> bool:
        old = grant.get("superseded")
        if not old:
            return True
        if old.get("_shared"):
            return self._detach_shared(old)
        try:
            self._grant_request(old, "disconnect")
            return True
        except GmailConnectionError as error:
            return error.code in {"NOT_FOUND", "REAUTHORIZE"}

    def _enable_email(self) -> None:
        if self.service and self.service != "gmail":
            return
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
        return {"access_token": access, "expiry": datetime.fromtimestamp(expiry / 1000, timezone.utc).isoformat(), "email": email, "scopes": data.get("scope", ""), **({"subject": data["subject"]} if isinstance(data.get("subject"), str) else {})}

    def _verify_gmail(self, tokens: dict) -> None:
        if self.service and self.service != "gmail" and not tokens.get("_shared"):
            data = self._request("GET", "https://openidconnect.googleapis.com/v1/userinfo", secret=tokens["access_token"])
            if (data.get("email_verified") is not True or data.get("email") != tokens["email"]
                    or not isinstance(data.get("sub"), str) or not data["sub"]
                    or data["sub"] != tokens.get("subject")):
                raise GmailConnectionError("GMAIL_ACCOUNT_MISMATCH")
            return
        data = self._request("GET", "https://gmail.googleapis.com/gmail/v1/users/me/profile", secret=tokens["access_token"])
        if data.get("emailAddress") != tokens["email"]:
            raise GmailConnectionError("GMAIL_ACCOUNT_MISMATCH")

    def cancel(self, request_id: str) -> dict:
        with self._lock():
            grant = self._require_pending(request_id)
            credentials = self._read_credentials()
            saved_same_grant = bool(credentials and credentials.get("grant_id") == request_id)
            if saved_same_grant and grant.get("replacing_connection_id"):
                # Authorization already committed. A late cancel must not erase it.
                raise GmailConnectionError("SETUP_NOT_FOUND")
            if saved_same_grant:
                # A config write may have failed after credentials were saved.
                # Cancellation must also stop that partial local connection.
                credentials["disconnect_pending"] = True
                credentials.pop("access_token", None)
                self._save_credentials(credentials)
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
            credentials = self._read_credentials()
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
        result = {"connectionId": self.connection_id(credentials), "email": credentials.get("email"), "mode": "managed" if is_managed_credentials(credentials) else "legacy",
                  "services": granted_services(credentials), "requestedServices": credentials.get("services", ["gmail"])}
        if credentials.get("disconnect_pending"):
            return {**result, "status": "disconnect_pending", "connected": False}
        if not verify:
            return {**result, "status": "saved", "connected": False}
        try:
            if is_managed_credentials(credentials):
                tokens = self._token(credentials)
                self._verify_gmail({**tokens, "_shared": credentials.get("_shared")})
                if credentials.get("subject") and tokens.get("subject") != credentials["subject"]:
                    raise GmailConnectionError("GMAIL_ACCOUNT_MISMATCH")
                if credentials.get("email") and tokens["email"].casefold() != credentials["email"].casefold():
                    raise GmailConnectionError("GMAIL_ACCOUNT_MISMATCH")
                credentials.pop("reauthorize_required", None)
                credentials.update(tokens)
                self._save_credentials(credentials)
            else:
                from flowly.channels.gmail_auth import get_valid_access_token
                token, email = get_valid_access_token(self.service) if self.service else get_valid_access_token()
                if not token:
                    raise GmailConnectionError("REAUTHORIZE")
                self._verify_gmail({"access_token": token, "email": email, "_shared": credentials.get("_shared")})
            return {**result, "status": "connected", "connected": True, "services": [item for item in granted_services(credentials) if not self.service or item in service_permissions(self.service)]}
        except GmailConnectionError as error:
            if is_managed_credentials(credentials) and error.code in {"UNAUTHORIZED", "REAUTHORIZE", "NOT_FOUND"}:
                credentials["reauthorize_required"] = True
                credentials.pop("access_token", None)
                self._save_credentials(credentials)
            return {**result, "status": "reauthorize" if error.code in {"UNAUTHORIZED", "REAUTHORIZE", "NOT_FOUND"} else "unavailable", "connected": False, "error": {"code": error.code}}

    def status(self, *, verify: bool = True) -> dict:
        with self._lock():
            credentials = self._read_credentials()
            result = self._status_locked(credentials, verify=verify)
            pending = _read(self.pending)
            if verify and result.get("connected") and pending and credentials and pending.get("grant_id") == credentials.get("grant_id"):
                # Retry a committed upgrade's revocation journal on normal status checks.
                self._enable_email()
                if self._retire_superseded(pending):
                    self.pending.unlink(missing_ok=True)
                else:
                    result["cleanupPending"] = True
            if self.service:
                result["service"] = self.service
                result["services"] = [item for item in result.get("services", []) if item in service_permissions(self.service)]
                if result.get("connected") and self.service not in result["services"]:
                    result.update(connected=False, status="reauthorize", error={"code": "PERMISSION_REQUIRED"})
            return result

    def disconnect(self, connection_id: str) -> dict:
        with self._lock():
            credentials = self._read_credentials()
            if not credentials or self.connection_id(credentials) != connection_id:
                raise GmailConnectionError("CONNECTION_CHANGED")
            if credentials.get("_shared"):
                # A tombstone shadows fallback immediately, including on revocation failure.
                stopped = {**credentials, "disconnect_pending": True}
                stopped.pop("access_token", None)
                atomic_private_json(self.credentials, stopped)
                pending = _read(self.pending)
                if pending:
                    try:
                        self._grant_request(pending, "disconnect")
                    except GmailConnectionError as error:
                        if error.code not in {"NOT_FOUND", "EXPIRED", "REAUTHORIZE"}:
                            return {"connected": False, "status": "disconnect_pending", "connectionId": connection_id}
                if not self._detach_shared(credentials):
                    return {"connected": False, "status": "disconnect_pending", "connectionId": connection_id}
                self.credentials.unlink(missing_ok=True)
                self.pending.unlink(missing_ok=True)
                return {"connected": False, "status": "not_configured"}
            # Stop local use before ANY network call, including pending upgrades.
            credentials["disconnect_pending"] = True
            credentials.pop("access_token", None)
            self._save_credentials(credentials)
            pending = _read(self.pending)
            try:
                if pending and pending.get("grant_id") != credentials.get("grant_id"):
                    try:
                        self._grant_request(pending, "disconnect")
                    except GmailConnectionError as error:
                        if error.code not in {"NOT_FOUND", "REAUTHORIZE"}:
                            raise
                if pending and not self._retire_superseded(pending):
                    raise GmailConnectionError("UNAVAILABLE")
                if is_managed_credentials(credentials):
                    self._grant_request(credentials, "disconnect")
            except GmailConnectionError as error:
                if error.code not in {"NOT_FOUND", "REAUTHORIZE"}:
                    return {"connected": False, "status": "disconnect_pending", "connectionId": connection_id}
            self.credentials.unlink(missing_ok=True)
            self.pending.unlink(missing_ok=True)
            return {"connected": False, "status": "not_configured"}

    def access_token(self) -> tuple[str | None, str | None]:
        with self._lock():
            credentials = self._read_credentials()
            if not credentials or not is_managed_credentials(credentials) or credentials.get("disconnect_pending") or credentials.get("reauthorize_required"):
                return None, None
            from flowly.channels.gmail_auth import _is_expired
            if _is_expired(credentials):
                tokens = self._token(credentials)
                if tokens["email"] != credentials.get("email"):
                    raise GmailConnectionError("GMAIL_ACCOUNT_MISMATCH")
                if credentials.get("subject") and tokens.get("subject") != credentials["subject"]:
                    raise GmailConnectionError("GMAIL_ACCOUNT_MISMATCH")
                credentials.update(tokens)
                self._save_credentials(credentials)
            return credentials.get("access_token"), credentials.get("email")
