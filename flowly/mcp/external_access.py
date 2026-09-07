"""Private, profile-local authority for owner-created external MCP clients.

Only SHA-256 digests are stored. The owner UI generates and retains the secret
until it has acknowledged creation, so a lost RPC reply needs no second key.
This is a protocol permission boundary, not a sandbox against local file access.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import secrets
import time
from pathlib import Path

from flowly.mcp.oauth_state import atomic_private_write, read_private, state_lock

MAX_KEYS = 256
MAX_TOOLS = 64
MAX_TTL = 90 * 86400
_HEX = re.compile(r"[a-f0-9]{64}")
_ID = re.compile(r"[a-zA-Z0-9_-]{16,64}")


class ExternalAccessError(ValueError):
    """Credential-free errors suitable for the owner and external client."""


def _bounded_text(value, maximum: int) -> bool:
    return isinstance(value, str) and 0 < len(value) <= maximum and not any(ord(c) < 32 for c in value)


class ExternalAccessStore:
    def __init__(self, path: Path, *, clock=time.time):
        self.path = path
        self.clock = clock

    @staticmethod
    def _validate(row: dict) -> None:
        try:
            valid = (
                isinstance(row, dict) and isinstance(row["id"], str) and _ID.fullmatch(row["id"])
                and _bounded_text(row["label"], 80)
                and isinstance(row["digest"], str) and _HEX.fullmatch(row["digest"])
                and _bounded_text(row["sessionKey"], 512) and ":" in row["sessionKey"]
                and isinstance(row["tools"], list) and 0 < len(row["tools"]) <= MAX_TOOLS
                and all(_bounded_text(name, 256) for name in row["tools"])
                and len(set(row["tools"])) == len(row["tools"])
                and all(type(row[field]) in (int, float) and math.isfinite(row[field])
                        for field in ("createdAt", "expiresAt"))
                and 60 <= row["expiresAt"] - row["createdAt"] <= MAX_TTL
                and isinstance(row["revoked"], bool)
            )
        except (KeyError, TypeError, ValueError):
            valid = False
        if not valid:
            raise ExternalAccessError("Invalid external access settings")

    def _read(self) -> list[dict]:
        try:
            raw = json.loads(read_private(self.path))
            if not isinstance(raw, dict) or raw.get("version") != 1:
                raise ValueError
            rows = raw["credentials"]
            if not isinstance(rows, list) or len(rows) > MAX_KEYS:
                raise ValueError
            for row in rows:
                self._validate(row)
            if len({row["id"] for row in rows}) != len(rows) or len({row["digest"] for row in rows}) != len(rows):
                raise ValueError
            return rows
        except FileNotFoundError:
            return []
        except (OSError, ValueError, KeyError, TypeError, RecursionError):
            raise ExternalAccessError("External access state cannot be safely read") from None

    def _save(self, rows: list[dict]) -> None:
        data = json.dumps({"version": 1, "credentials": rows}).encode()
        if len(data) > 1024 * 1024:
            raise ExternalAccessError("External access state reached its safe size limit")
        atomic_private_write(self.path, data)

    def public(self, row: dict) -> dict:
        value = {key: copy.deepcopy(row[key]) for key in (
            "id", "label", "sessionKey", "tools", "createdAt", "expiresAt",
        )}
        value["status"] = "revoked" if row["revoked"] else "expired" if self.clock() >= row["expiresAt"] else "active"
        return value

    def list(self) -> list[dict]:
        return [self.public(row) for row in self._read()]

    def create(self, params: dict, *, available: set[str]) -> dict:
        """Owner RPC only. A request ID and secret digest are retry-stable."""
        if not isinstance(params, dict) or set(params) != {"id", "label", "sessionKey", "tools", "tokenDigest", "ttlSeconds"}:
            raise ExternalAccessError("Choose a name, conversation, tools and key lifetime")
        ttl = params["ttlSeconds"]
        if type(ttl) is not int or not 60 <= ttl <= MAX_TTL:
            raise ExternalAccessError("Key lifetime must be between one minute and 90 days")
        now = self.clock()
        row = {
            "id": params["id"], "label": params["label"], "sessionKey": params["sessionKey"],
            "digest": params["tokenDigest"], "tools": params["tools"],
            "createdAt": now, "expiresAt": now + ttl, "revoked": False,
        }
        self._validate(row)
        row["tools"] = sorted(row["tools"])
        if not set(row["tools"]) <= available:
            raise ExternalAccessError("Selected tools are no longer available; review permissions again")
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with state_lock(self.path.with_suffix(".lock"), timeout=0.25):
            rows = self._read()
            for existing in rows:
                if existing["id"] == row["id"]:
                    if any(existing[key] != row[key] for key in ("label", "sessionKey", "digest", "tools")) or existing["expiresAt"] - existing["createdAt"] != ttl:
                        raise ExternalAccessError("Creation request was already used with different permissions")
                    return self.public(existing)
                if secrets.compare_digest(existing["digest"], row["digest"]):
                    raise ExternalAccessError("This key is already registered")
            if len(rows) >= MAX_KEYS:
                raise ExternalAccessError("External access key limit reached")
            rows.append(row)
            self._save(rows)
        return self.public(row)

    def revoke(self, key_id: str) -> dict:
        if not isinstance(key_id, str) or not _ID.fullmatch(key_id):
            raise ExternalAccessError("Invalid access key identifier")
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with state_lock(self.path.with_suffix(".lock"), timeout=0.25):
            rows = self._read()
            for row in rows:
                if row["id"] == key_id:
                    if not row["revoked"]:
                        row["revoked"] = True
                        self._save(rows)
                    return self.public(row)
        raise ExternalAccessError("Access key was not found")

    def revoke_session(self, session_key: str) -> None:
        """Durably withdraw every key for a deleted conversation, including expired keys."""
        with state_lock(self.path.with_suffix(".lock"), timeout=0.25):
            rows = self._read()
            changed = False
            for row in rows:
                if row["sessionKey"] == session_key and not row["revoked"]:
                    row["revoked"] = True
                    changed = True
            if changed:
                self._save(rows)

    def authorize(self, token: str, tool: str | None = None) -> dict:
        if not isinstance(token, str) or not re.fullmatch(r"flm_[a-f0-9]{64}", token):
            raise ExternalAccessError("Invalid, revoked or expired access key")
        digest = hashlib.sha256(token.encode()).hexdigest()
        row = next((row for row in self._read() if secrets.compare_digest(row["digest"], digest)), None)
        if row is None or row["revoked"] or self.clock() >= row["expiresAt"]:
            raise ExternalAccessError("Invalid, revoked or expired access key")
        if tool is not None and tool not in row["tools"]:
            raise ExternalAccessError("This tool is not permitted by the access key")
        return row
