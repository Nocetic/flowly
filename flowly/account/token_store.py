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
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from flowly.profile import credential_scope_suffix, get_flowly_home, is_default_home

log = logging.getLogger(__name__)

ACCOUNT_KEY = "account"  # single composite blob (id_token + refresh_token + metadata)

# LEGACY_PATH is intentionally NOT home-scoped and the migration/purge
# functions below intentionally only run at the default home: this is a
# one-time migration source for plaintext files written before OS-keychain
# support existed, back when every install used the default ~/.flowly
# (profiles/custom homes came later). A legacy plaintext file only ever
# belongs to the default home — a second home (e.g. a different product
# built on this codebase) must never read, migrate, or delete it.
LEGACY_PATH = Path.home() / ".flowly" / "credentials" / "account.json"


def _service_name() -> str:
    """Keychain service name, scoped to the active FLOWLY_HOME.

    See :func:`flowly.profile.credential_scope_suffix` — unsuffixed
    (``"flowly-tui"``) at the default home for backward compatibility (no
    re-login for existing users), suffixed everywhere else so two homes
    never share one keychain entry.
    """
    suffix = credential_scope_suffix()
    return f"flowly-tui:{suffix}" if suffix else "flowly-tui"


def _credentials_dir() -> Path:
    return get_flowly_home() / "credentials"


def _fallback_path() -> Path:
    return _credentials_dir() / "account.json"


def _keyring_marker_path() -> Path:
    # Persistent marker: when present, this process and all future ones skip
    # keyring entirely. Written the first time macOS surfaces "Keychain Not
    # Found" (or any other set_password/get_password failure). Without this,
    # every launch re-prompts the user — the in-process latch alone is reset
    # each time Python exits. Delete the file to re-enable keyring (e.g.
    # after fixing your login keychain).
    return _credentials_dir() / ".keychain-broken"


@dataclass(frozen=True)
class StorageStatus:
    backend: str       # "keyring" | "file" | "unavailable"
    detail: str        # e.g. "macOS Keychain" / "fallback to ~/.flowly/credentials/account.json"
    secure: bool       # True iff backend is OS-protected


# In-process latch; seeded from the persistent marker at first use.
_KEYRING_DISABLED: bool | None = None  # None = not yet decided

# Cached result of the macOS pre-flight below (None = not yet probed).
_MACOS_KEYCHAIN_MISSING: bool | None = None


def _default_keychain_path(value: Any) -> Path | None:
    """Path named by ``com.apple.security.plist``'s ``DefaultKeychain`` entry.

    Returns None whenever the value isn't a shape we recognise — the caller
    treats that as "assume the keychain works", never as "it's broken".
    """
    if isinstance(value, str):
        raw = value
    elif isinstance(value, dict):
        raw = ""
        for key in ("path", "Path", "PathName"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate:
                raw = candidate
                break
    else:
        return None
    if not raw:
        return None
    return Path(os.path.expanduser(raw))


def _macos_keychain_missing() -> bool:
    """True only when we can *prove* this Mac has no default keychain.

    Writing a password to a Mac whose default keychain is missing makes
    macOS put up a blocking "Keychain Not Found" panel. Dismissing it
    returns ``errAuthorizationCanceled`` (-60006) — which is what first-run
    users actually hit, mid-sign-in, with no idea what it wants. Looking
    before we leap costs two ``stat`` calls and skips the panel entirely.

    Deliberately one-sided: every uncertain case returns False and we try
    the keychain exactly as before. A wrong True would silently downgrade a
    perfectly good Mac to file storage — far worse than one dialog. This
    also never writes the marker, so a Mac that gets fixed starts using its
    keychain again on the next launch with no user action.

    Note ``com.apple.security.plist`` does not exist on a healthy Mac — it
    is only written once the default keychain is explicitly changed — so
    its absence means "the default is ~/Library/Keychains/login.keychain-db",
    not "something is wrong".
    """
    if sys.platform != "darwin":
        return False
    try:
        keychains = Path.home() / "Library" / "Keychains"
        if (keychains / "login.keychain-db").exists() or (keychains / "login.keychain").exists():
            return False
        prefs = Path.home() / "Library" / "Preferences" / "com.apple.security.plist"
        if not prefs.exists():
            return True  # no override, and the default file is gone
        import plistlib
        with prefs.open("rb") as handle:
            data = plistlib.load(handle)
        configured = data.get("DefaultKeychain") if isinstance(data, dict) else None
        target = _default_keychain_path(configured)
        if target is None:
            return False  # unreadable / unfamiliar shape — give it a go
        return not target.exists()
    except Exception:
        return False


def _is_keyring_marked_broken() -> bool:
    """Check the persistent disable marker.

    Returns True iff a previous run wrote the marker after a keyring
    failure. This is the **first thing** to consult on every launch —
    skipping it means the macOS "Keychain Not Found" dialog re-appears on
    cold start even though we already learned it doesn't work.
    """
    try:
        return _keyring_marker_path().exists()
    except OSError:
        return False


def _write_marker(reason: str) -> None:
    """Persist the keyring-broken latch so future processes skip it too."""
    marker_path = _keyring_marker_path()
    try:
        marker_path.parent.mkdir(parents=True, exist_ok=True)
        import time
        marker_path.write_text(
            f"# Flowly disabled the OS keychain after this error:\n"
            f"# {reason}\n"
            f"# Recorded at unix={int(time.time())}\n"
            f"# Delete this file to let Flowly try the keychain again.\n",
            encoding="utf-8",
        )
        try:
            from flowly.utils.file_security import secure_file
            secure_file(marker_path)  # POSIX chmod; owner-only ACL on Windows
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

    2. **Persistent marker file** (``_keyring_marker_path()``) — written
       alongside the in-process flag. Consulted on first call of every
       new process so cold starts skip keyring entirely. The user can
       run ``flowly keychain retry`` (or delete the marker) once they've
       fixed their keychain.

    Ahead of both sits the macOS pre-flight (``_macos_keychain_missing``),
    which keeps a Mac with no default keychain from ever reaching the
    blocking OS panel. Unlike the two latches it is *not* sticky — nothing
    is written, so a repaired Mac recovers on its own.
    """
    global _KEYRING_DISABLED, _MACOS_KEYCHAIN_MISSING
    if _KEYRING_DISABLED is None:
        # First call this process — seed the in-process latch from disk
        # so the very first save/load doesn't hit the OS dialog if a prior
        # launch already learned it's broken.
        _KEYRING_DISABLED = _is_keyring_marked_broken()
    if _KEYRING_DISABLED:
        return None
    if _MACOS_KEYCHAIN_MISSING is None:
        _MACOS_KEYCHAIN_MISSING = _macos_keychain_missing()
    if _MACOS_KEYCHAIN_MISSING:
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


# macOS hands back a bare OSStatus and ``keyring`` only translates a
# handful of them, so anything else reaches the user as
# "(-60006, 'Unknown Error')" — mid-onboarding, in a terminal. These are
# the codes we actually see in the wild; the raw code still goes into the
# marker file for support.
_OS_STATUS_REASONS = {
    -60006: "the macOS keychain prompt was dismissed",
    -25294: "this Mac has no keychain to save into",
    -25308: "macOS couldn't show the keychain prompt",
    -25293: "macOS turned down the keychain request",
    -128: "the keychain prompt was dismissed",
}


def explain_keyring_error(exc: Exception) -> str:
    """One plain sentence describing why the OS keychain refused us."""
    match = re.search(r"\((-?\d+)", str(exc))
    if match:
        reason = _OS_STATUS_REASONS.get(int(match.group(1)))
        if reason:
            return reason
    return "the OS keychain wasn't available"


def available_keyring():
    """The working keyring backend, or None — shared across credential stores.

    ``flowly.auth.*`` each used to carry their own copy of this probe, so a
    Mac that couldn't write to its keychain re-opened the blocking OS panel
    once per provider. They now share this one, which means one marker file,
    one macOS pre-flight and one in-process latch decide it for everybody.
    """
    return _try_keyring()


def disable_keyring_for(
    operation: str, exc: Exception, *, fallback: Path | None = None
) -> None:
    """Latch the OS keychain off after a failed read/write.

    Shared entry point for every credential store in the codebase (the
    personal account here, plus the subscription tokens under
    ``flowly.auth``). ``fallback`` names the file that store writes to
    instead, so the message points at the user's actual credentials.
    """
    _disable_keyring(
        explain_keyring_error(exc),
        raw=f"{operation}: {type(exc).__name__}: {exc}",
        fallback=fallback,
    )


def _disable_keyring(
    reason: str, *, raw: str = "", fallback: Path | None = None
) -> None:
    """Latch keyring off for this process AND every future process.

    ``reason`` is the plain sentence the user reads; ``raw`` is the verbatim
    exception, kept in the marker file only so support can see the code.
    """
    global _KEYRING_DISABLED
    already = bool(_KEYRING_DISABLED)
    _KEYRING_DISABLED = True
    if already:
        return
    log.warning(
        "Couldn't use the OS keychain — %s. Saved to %s instead, readable "
        "only by you. Run `flowly keychain retry` once it's fixed.",
        reason, fallback or _fallback_path(),
    )
    _write_marker(f"{reason}\n# {raw}" if raw else reason)


def reset_keyring_disable() -> bool:
    """Remove the persistent keyring-broken marker so the next launch
    attempts the keychain again. Returns True iff a marker existed.

    Backs ``flowly keychain retry``."""
    global _KEYRING_DISABLED, _MACOS_KEYCHAIN_MISSING
    existed = False
    try:
        marker_path = _keyring_marker_path()
        existed = marker_path.exists()
        marker_path.unlink(missing_ok=True)
    except OSError as exc:
        log.warning("keyring marker unlink failed: %s", exc)
    _KEYRING_DISABLED = None  # re-probe on next _try_keyring()
    _MACOS_KEYCHAIN_MISSING = None  # …including the macOS pre-flight
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
    fallback_path = _fallback_path()
    if _is_keyring_marked_broken():
        return StorageStatus(
            backend="file",
            detail=(
                f"fallback to {fallback_path} (mode 0600) — the OS keychain "
                f"was unavailable in an earlier run. Run `flowly keychain "
                f"retry` once you've fixed it."
            ),
            secure=False,
        )
    return StorageStatus(
        backend="file",
        detail=f"fallback to {fallback_path} (mode 0600) — {_no_keyring_hint()}",
        secure=False,
    )


def _no_keyring_hint() -> str:
    """Why there's no OS keychain, phrased for the platform in front of us.

    The old text told everyone to install gnome-keyring, which is nonsense
    advice on the Mac where this fallback actually fires most often.
    """
    if sys.platform == "darwin":
        return "this Mac has no keychain Flowly can write to"
    if os.name == "nt":
        return "Windows Credential Manager wasn't available"
    return "install gnome-keyring or libsecret for OS keychain storage"


def save_credentials(payload: dict[str, Any]) -> StorageStatus:
    """Persist token payload. Returns the storage status actually used."""
    blob = json.dumps(payload, separators=(",", ":"))
    keyring = _try_keyring()
    if keyring is not None:
        try:
            keyring.set_password(_service_name(), ACCOUNT_KEY, blob)
            # If a legacy file exists, sweep it now that the keychain is
            # authoritative. We refuse to leave plaintext sitting around.
            # (Only at the default home — see _purge_legacy_file.)
            _purge_legacy_file()
            return storage_status()
        except Exception as exc:
            # Latch keyring as broken — without this, the next background
            # token refresh re-triggers the macOS "Keychain Not Found"
            # dialog ad infinitum.
            disable_keyring_for("set_password", exc)
    # File fallback
    _write_file(blob)
    return storage_status()


def load_credentials() -> dict[str, Any] | None:
    """Read token payload. Migrates legacy JSON file if keyring is available."""
    keyring = _try_keyring()
    service_name = _service_name()
    if keyring is not None:
        try:
            raw = keyring.get_password(service_name, ACCOUNT_KEY)
        except Exception as exc:
            disable_keyring_for("get_password", exc)
            raw = None
        if raw:
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                log.error("keyring blob malformed — clearing")
                try: keyring.delete_password(service_name, ACCOUNT_KEY)
                except Exception: pass
                return None
        # No entry in keyring yet — see if there's a legacy file to import
        # (default home only — see _migrate_legacy_file_to_keyring).
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
            keyring.delete_password(_service_name(), ACCOUNT_KEY)
        except Exception:
            pass
    _purge_legacy_file()


# ── private helpers ────────────────────────────────────────────────


def _write_file(blob: str) -> None:
    fallback_path = _fallback_path()
    fallback_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = fallback_path.with_suffix(".tmp")
    tmp.write_text(blob, encoding="utf-8")
    tmp.replace(fallback_path)
    try:
        from flowly.utils.file_security import secure_file
        secure_file(fallback_path)  # POSIX chmod; owner-only ACL on Windows
    except OSError:
        pass


def _read_file() -> dict[str, Any] | None:
    try:
        return json.loads(_fallback_path().read_text(encoding="utf-8"))
    except (OSError, FileNotFoundError, json.JSONDecodeError):
        return None


def _purge_legacy_file() -> None:
    # LEGACY_PATH is a default-home-only artifact (see its docstring) — a
    # non-default home must never touch another home's file.
    if not is_default_home():
        return
    try:
        LEGACY_PATH.unlink()
    except OSError:
        pass


def _migrate_legacy_file_to_keyring(keyring) -> dict[str, Any] | None:
    """On first keyring use, import any existing ~/.flowly/credentials/account.json.

    The file is deleted after a successful import — no plaintext lingering.
    Default-home only: LEGACY_PATH is fixed at ~/.flowly, so a non-default
    home (e.g. a second product built on this codebase) must never read,
    migrate, or delete it — that file belongs to a different install.
    """
    if not is_default_home():
        return None
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
        keyring.set_password(_service_name(), ACCOUNT_KEY, raw)
        log.info("migrated %s → keyring", LEGACY_PATH)
        _purge_legacy_file()
    except Exception as exc:
        # Macs without a default keychain (or with one locked by a sync
        # tool, e.g. iCloud Keychain reset) raise here. Disable keyring
        # for the rest of this process so we stop re-prompting the user.
        disable_keyring_for("migration set_password", exc)
        return data
    return data
