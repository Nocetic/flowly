"""Secure token storage with OS keychain primary + JSON file fallback.

Why this exists
---------------
Tokens used to live in plaintext at ``~/.flowly/credentials/account.json``
(file mode 0600). That's safe-ish on a single-user machine but leaks via
backups (Time Machine, rsync, BorgBackup), sync tools (Dropbox, iCloud),
and file-system enumeration. Enterprise environments require credentials
in OS-managed keychains.

This module wraps ``keyring`` (cross-platform: macOS Keychain, Linux
Secret Service via D-Bus, Windows Credential Manager) and transparently
migrates any legacy JSON file on first read.

If keyring is unavailable (headless Linux without dbus/gnome-keyring, CI
sandboxes, exotic platforms), we fall back to the old file with a clear
warning so users know they're below the security bar.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

SERVICE_NAME = "flowly-tui"
ACCOUNT_KEY = "account"  # single composite blob (id_token + refresh_token + metadata)

LEGACY_PATH = Path.home() / ".flowly" / "credentials" / "account.json"
FALLBACK_PATH = Path.home() / ".flowly" / "credentials" / "account.json"
# Persistent marker: when present, this process and all future ones skip
# keyring entirely. Written the first time macOS surfaces "Keychain Not
# Found" (or any other set_password/get_password failure). Without this,
# every launch re-prompts the user — the in-process latch alone is reset
# each time Python exits. Delete the file to re-enable keyring (e.g.
# after fixing your login keychain).
KEYRING_MARKER_PATH = Path.home() / ".flowly" / "credentials" / ".keychain-broken"


@dataclass(frozen=True)
class StorageStatus:
    backend: str       # "keyring" | "file" | "unavailable"
    detail: str        # e.g. "macOS Keychain" / "fallback to ~/.flowly/credentials/account.json"
    secure: bool       # True iff backend is OS-protected


# In-process latch; seeded from the persistent marker at first use.
_KEYRING_DISABLED: bool | None = None  # None = not yet decided


def _is_keyring_marked_broken() -> bool:
    """Check the persistent disable marker.

    Returns True iff a previous run wrote the marker after a keyring
    failure. This is the **first thing** to consult on every launch —
    skipping it means the macOS "Keychain Not Found" dialog re-appears on
    cold start even though we already learned it doesn't work.
    """
    try:
        return KEYRING_MARKER_PATH.exists()
    except OSError:
        return False


def _write_marker(reason: str) -> None:
    """Persist the keyring-broken latch so future processes skip it too."""
    try:
        KEYRING_MARKER_PATH.parent.mkdir(parents=True, exist_ok=True)
        import time
        KEYRING_MARKER_PATH.write_text(
            f"# Flowly disabled the OS keychain after this error:\n"
            f"# {reason}\n"
            f"# Recorded at unix={int(time.time())}\n"
            f"# Delete this file to let Flowly try the keychain again.\n",
            encoding="utf-8",
        )
        try:
            from flowly.utils.file_security import secure_file
            secure_file(KEYRING_MARKER_PATH)  # POSIX chmod; owner-only ACL on Windows
        except OSError:
            pass
    except OSError as exc:
        log.warning("keyring marker write failed: %s (will re-prompt next launch)", exc)


def _try_keyring():
    """Return the keyring module if a *working* backend is available.

    macOS uses ``keyring.backends.macOS.Keyring`` (class literally "Keyring"
    — naming collision), so we filter by module path, not class name. The
    null/fail backends live under ``keyring.backends.fail`` and
    ``keyring.backends.null``.

    Two-layer self-disable:

    1. **In-process latch** (``_KEYRING_DISABLED``) — flipped after the
       first ``set_password``/``get_password`` exception this run. Stops
       background token refresh from re-triggering the dialog within the
       same launch.

    2. **Persistent marker file** (``KEYRING_MARKER_PATH``) — written
       alongside the in-process flag. Consulted on first call of every
       new process so cold starts skip keyring entirely. The user can
       ``rm`` the marker to retry once they've fixed their keychain.
    """
    global _KEYRING_DISABLED
    if _KEYRING_DISABLED is None:
        # First call this process — seed the in-process latch from disk
        # so the very first save/load doesn't hit the OS dialog if a prior
        # launch already learned it's broken.
        _KEYRING_DISABLED = _is_keyring_marked_broken()
    if _KEYRING_DISABLED:
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


def _disable_keyring(reason: str) -> None:
    """Latch keyring off for this process AND every future process."""
    global _KEYRING_DISABLED
    already = bool(_KEYRING_DISABLED)
    _KEYRING_DISABLED = True
    if not already:
        log.warning("keyring disabled: %s (writing %s)", reason, KEYRING_MARKER_PATH)
        _write_marker(reason)


def reset_keyring_disable() -> bool:
    """Remove the persistent keyring-broken marker so the next launch
    attempts the keychain again. Returns True iff a marker existed.

    Exposed for a future ``/keyring retry`` slash command and for tests."""
    global _KEYRING_DISABLED
    existed = False
    try:
        existed = KEYRING_MARKER_PATH.exists()
        KEYRING_MARKER_PATH.unlink(missing_ok=True)
    except OSError as exc:
        log.warning("keyring marker unlink failed: %s", exc)
    _KEYRING_DISABLED = None  # re-probe on next _try_keyring()
    return existed


def storage_status() -> StorageStatus:
    """Probe what backend is actually in use right now."""
    keyring = _try_keyring()
    if keyring is not None:
        backend = keyring.get_keyring()
        module = type(backend).__module__ or ""
        # Friendly per-platform name from module path.
        if "macOS" in module:
            nice = "macOS Keychain"
        elif "Windows" in module:
            nice = "Windows Credential Manager"
        elif "SecretService" in module:
            nice = "Linux Secret Service (libsecret)"
        elif "kwallet" in module.lower():
            nice = "KDE KWallet"
        elif "chainer" in module.lower():
            nice = f"keyring chainer ({type(backend).__name__})"
        else:
            nice = f"{type(backend).__name__} ({module})"
        return StorageStatus(backend="keyring", detail=nice, secure=True)
    if _is_keyring_marked_broken():
        return StorageStatus(
            backend="file",
            detail=(
                f"fallback to {FALLBACK_PATH} (mode 0600) — OS keychain was "
                f"unavailable in a prior run. Delete {KEYRING_MARKER_PATH} "
                f"to retry once you've fixed it."
            ),
            secure=False,
        )
    return StorageStatus(
        backend="file",
        detail=f"fallback to {FALLBACK_PATH} (mode 0600) — install gnome-keyring or libsecret for OS keychain",
        secure=False,
    )


def save_credentials(payload: dict[str, Any]) -> StorageStatus:
    """Persist token payload. Returns the storage status actually used."""
    blob = json.dumps(payload, separators=(",", ":"))
    keyring = _try_keyring()
    if keyring is not None:
        try:
            keyring.set_password(SERVICE_NAME, ACCOUNT_KEY, blob)
            # If a legacy file exists, sweep it now that the keychain is
            # authoritative. We refuse to leave plaintext sitting around.
            _purge_legacy_file()
            return storage_status()
        except Exception as exc:
            # Latch keyring as broken — without this, the next background
            # token refresh re-triggers the macOS "Keychain Not Found"
            # dialog ad infinitum.
            _disable_keyring(f"set_password: {type(exc).__name__}: {exc}")
    # File fallback
    _write_file(blob)
    return storage_status()


def load_credentials() -> dict[str, Any] | None:
    """Read token payload. Migrates legacy JSON file if keyring is available."""
    keyring = _try_keyring()
    if keyring is not None:
        try:
            raw = keyring.get_password(SERVICE_NAME, ACCOUNT_KEY)
        except Exception as exc:
            _disable_keyring(f"get_password: {type(exc).__name__}: {exc}")
            raw = None
        if raw:
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                log.error("keyring blob malformed — clearing")
                try: keyring.delete_password(SERVICE_NAME, ACCOUNT_KEY)
                except Exception: pass
                return None
        # No entry in keyring yet — see if there's a legacy file to import.
        migrated = _migrate_legacy_file_to_keyring(keyring)
        if migrated is not None:
            return migrated
        return None

    # No keyring — read file directly.
    return _read_file()


def clear_credentials() -> None:
    keyring = _try_keyring()
    if keyring is not None:
        try:
            keyring.delete_password(SERVICE_NAME, ACCOUNT_KEY)
        except Exception:
            pass
    _purge_legacy_file()


# ── private helpers ────────────────────────────────────────────────


def _write_file(blob: str) -> None:
    FALLBACK_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = FALLBACK_PATH.with_suffix(".tmp")
    tmp.write_text(blob, encoding="utf-8")
    tmp.replace(FALLBACK_PATH)
    try:
        from flowly.utils.file_security import secure_file
        secure_file(FALLBACK_PATH)  # POSIX chmod; owner-only ACL on Windows
    except OSError:
        pass


def _read_file() -> dict[str, Any] | None:
    try:
        return json.loads(FALLBACK_PATH.read_text(encoding="utf-8"))
    except (OSError, FileNotFoundError, json.JSONDecodeError):
        return None


def _purge_legacy_file() -> None:
    try:
        LEGACY_PATH.unlink()
    except OSError:
        pass


def _migrate_legacy_file_to_keyring(keyring) -> dict[str, Any] | None:
    """On first keyring use, import any existing ~/.flowly/credentials/account.json.

    The file is deleted after a successful import — no plaintext lingering.
    """
    try:
        raw = LEGACY_PATH.read_text(encoding="utf-8")
    except (OSError, FileNotFoundError):
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        log.error("legacy credentials file malformed — leaving in place")
        return None
    try:
        keyring.set_password(SERVICE_NAME, ACCOUNT_KEY, raw)
        log.info("migrated %s → keyring", LEGACY_PATH)
        _purge_legacy_file()
    except Exception as exc:
        # Macs without a default keychain (or with one locked by a sync
        # tool, e.g. iCloud Keychain reset) raise here. Disable keyring
        # for the rest of this process so we stop re-prompting the user.
        _disable_keyring(f"migration set_password: {type(exc).__name__}: {exc}")
        return data
    return data
