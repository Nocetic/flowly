"""Pairing store for secure channel authorization."""

import json
import os
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal

from filelock import FileLock
from loguru import logger
from flowly.profile import get_flowly_home

# Constants
PAIRING_CODE_LENGTH = 8
PAIRING_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # No ambiguous chars (0O1I)
PAIRING_TTL = timedelta(hours=1)
PAIRING_MAX_PENDING = 3

Channel = Literal["telegram", "whatsapp", "imessage"]


@dataclass
class PairingRequest:
    """A pending pairing request."""
    id: str
    code: str
    created_at: str
    last_seen_at: str
    meta: dict[str, str] = field(default_factory=dict)


def _get_credentials_dir() -> Path:
    """Get the credentials directory."""
    creds_dir = get_flowly_home() / "credentials"
    creds_dir.mkdir(parents=True, exist_ok=True)
    return creds_dir


def _get_pairing_path(channel: Channel) -> Path:
    """Get path to pairing requests file."""
    return _get_credentials_dir() / f"{channel}-pairing.json"


def _get_allow_from_path(channel: Channel) -> Path:
    """Get path to allow_from store file."""
    return _get_credentials_dir() / f"{channel}-allowFrom.json"


def _read_json_file(path: Path, default: dict) -> dict:
    """Safely read a JSON file."""
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, IOError) as e:
        logger.warning(f"Error reading {path}: {e}")
    return default


def _write_json_file(path: Path, data: dict) -> None:
    """Safely write a JSON file with atomic rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(f".{secrets.token_hex(4)}.tmp")
    tmp_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    try:
        from flowly.utils.file_security import secure_file
        secure_file(tmp_path)  # POSIX chmod; real owner-only ACL on Windows
    except OSError:
        pass
    os.replace(str(tmp_path), str(path))


def _generate_code(existing_codes: set[str]) -> str:
    """Generate a unique pairing code."""
    for _ in range(500):
        code = "".join(secrets.choice(PAIRING_CODE_ALPHABET) for _ in range(PAIRING_CODE_LENGTH))
        if code not in existing_codes:
            return code
    raise RuntimeError("Failed to generate unique pairing code")


def _is_expired(request: PairingRequest) -> bool:
    """Check if a pairing request has expired."""
    try:
        created = datetime.fromisoformat(request.created_at.replace("Z", "+00:00"))
        return datetime.now(created.tzinfo) - created > PAIRING_TTL
    except (ValueError, TypeError):
        return True


def _prune_requests(requests: list[PairingRequest]) -> tuple[list[PairingRequest], bool]:
    """Remove expired requests, return (kept, was_modified)."""
    kept = [r for r in requests if not _is_expired(r)]

    # Also limit to max pending
    if len(kept) > PAIRING_MAX_PENDING:
        # Sort by last_seen_at and keep most recent
        kept.sort(key=lambda r: r.last_seen_at)
        kept = kept[-PAIRING_MAX_PENDING:]

    return kept, len(kept) != len(requests)


def list_pairing_requests(channel: Channel) -> list[PairingRequest]:
    """List pending pairing requests for a channel."""
    path = _get_pairing_path(channel)
    lock_path = path.with_suffix(".lock")

    with FileLock(lock_path, timeout=10):
        data = _read_json_file(path, {"version": 1, "requests": []})
        requests = [
            PairingRequest(
                id=r["id"],
                code=r["code"],
                created_at=r["created_at"],
                last_seen_at=r.get("last_seen_at", r["created_at"]),
                meta=r.get("meta", {}),
            )
            for r in data.get("requests", [])
            if isinstance(r, dict) and "id" in r and "code" in r
        ]

        pruned, modified = _prune_requests(requests)

        if modified:
            _write_json_file(path, {
                "version": 1,
                "requests": [
                    {
                        "id": r.id,
                        "code": r.code,
                        "created_at": r.created_at,
                        "last_seen_at": r.last_seen_at,
                        "meta": r.meta,
                    }
                    for r in pruned
                ],
            })

        return sorted(pruned, key=lambda r: r.created_at)


def upsert_pairing_request(
    channel: Channel,
    id: str,
    meta: dict[str, str] | None = None,
) -> tuple[str, bool]:
    """
    Create or update a pairing request.

    Returns (code, created) where created=True if new request.
    """
    path = _get_pairing_path(channel)
    lock_path = path.with_suffix(".lock")

    with FileLock(lock_path, timeout=10):
        data = _read_json_file(path, {"version": 1, "requests": []})
        requests = [
            PairingRequest(
                id=r["id"],
                code=r["code"],
                created_at=r["created_at"],
                last_seen_at=r.get("last_seen_at", r["created_at"]),
                meta=r.get("meta", {}),
            )
            for r in data.get("requests", [])
            if isinstance(r, dict) and "id" in r and "code" in r
        ]

        # Prune expired
        requests, _ = _prune_requests(requests)

        now = datetime.utcnow().isoformat() + "Z"
        existing_codes = {r.code.upper() for r in requests}

        # Check if request already exists
        for i, r in enumerate(requests):
            if r.id == id:
                # Update last_seen_at
                requests[i] = PairingRequest(
                    id=r.id,
                    code=r.code,
                    created_at=r.created_at,
                    last_seen_at=now,
                    meta=meta or r.meta,
                )
                _write_json_file(path, {
                    "version": 1,
                    "requests": [
                        {
                            "id": req.id,
                            "code": req.code,
                            "created_at": req.created_at,
                            "last_seen_at": req.last_seen_at,
                            "meta": req.meta,
                        }
                        for req in requests
                    ],
                })
                return r.code, False

        # Check max pending limit
        if len(requests) >= PAIRING_MAX_PENDING:
            logger.warning(f"Max pending pairing requests reached for {channel}")
            return "", False

        # Create new request
        code = _generate_code(existing_codes)
        new_request = PairingRequest(
            id=id,
            code=code,
            created_at=now,
            last_seen_at=now,
            meta=meta or {},
        )
        requests.append(new_request)

        _write_json_file(path, {
            "version": 1,
            "requests": [
                {
                    "id": r.id,
                    "code": r.code,
                    "created_at": r.created_at,
                    "last_seen_at": r.last_seen_at,
                    "meta": r.meta,
                }
                for r in requests
            ],
        })

        return code, True


def approve_pairing_code(channel: Channel, code: str) -> PairingRequest | None:
    """
    Approve a pairing code and add the user to allow_from.

    Returns the approved request, or None if code not found.
    """
    code = code.strip().upper()
    if not code:
        return None

    path = _get_pairing_path(channel)
    lock_path = path.with_suffix(".lock")

    with FileLock(lock_path, timeout=10):
        data = _read_json_file(path, {"version": 1, "requests": []})
        requests = [
            PairingRequest(
                id=r["id"],
                code=r["code"],
                created_at=r["created_at"],
                last_seen_at=r.get("last_seen_at", r["created_at"]),
                meta=r.get("meta", {}),
            )
            for r in data.get("requests", [])
            if isinstance(r, dict) and "id" in r and "code" in r
        ]

        # Prune expired
        requests, _ = _prune_requests(requests)

        # Find matching request
        approved = None
        remaining = []
        for r in requests:
            if r.code.upper() == code:
                approved = r
            else:
                remaining.append(r)

        if not approved:
            return None

        # Remove from pending
        _write_json_file(path, {
            "version": 1,
            "requests": [
                {
                    "id": r.id,
                    "code": r.code,
                    "created_at": r.created_at,
                    "last_seen_at": r.last_seen_at,
                    "meta": r.meta,
                }
                for r in remaining
            ],
        })

        # Add to allow_from store
        add_allow_from_entry(channel, approved.id)

        return approved


def read_allow_from_store(channel: Channel) -> list[str]:
    """Read the allow_from store for a channel."""
    path = _get_allow_from_path(channel)
    data = _read_json_file(path, {"version": 1, "allow_from": []})
    return [str(e).strip() for e in data.get("allow_from", []) if e]


def add_allow_from_entry(channel: Channel, entry: str) -> bool:
    """Add an entry to the allow_from store. Returns True if added."""
    entry = str(entry).strip()
    if not entry:
        return False

    path = _get_allow_from_path(channel)
    lock_path = path.with_suffix(".lock")

    with FileLock(lock_path, timeout=10):
        data = _read_json_file(path, {"version": 1, "allow_from": []})
        allow_from = [str(e).strip() for e in data.get("allow_from", []) if e]

        if entry in allow_from:
            return False

        allow_from.append(entry)
        _write_json_file(path, {"version": 1, "allow_from": allow_from})
        return True


def remove_allow_from_entry(channel: Channel, entry: str) -> bool:
    """Remove an entry from the allow_from store. Returns True if removed."""
    entry = str(entry).strip()
    if not entry:
        return False

    path = _get_allow_from_path(channel)
    lock_path = path.with_suffix(".lock")

    with FileLock(lock_path, timeout=10):
        data = _read_json_file(path, {"version": 1, "allow_from": []})
        allow_from = [str(e).strip() for e in data.get("allow_from", []) if e]

        if entry not in allow_from:
            return False

        allow_from.remove(entry)
        _write_json_file(path, {"version": 1, "allow_from": allow_from})
        return True
