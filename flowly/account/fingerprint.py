"""Per-machine identifier — SHARED with flowly-desktop.

Why this matters
----------------
The backend de-dups desktop servers by ``machineId``. If TUI and desktop
disagree on what the machine's identifier is, the same physical machine
ends up with multiple Firestore server entries — conversations split
across them, and no cross-client sync.

Desktop persists a random UUIDv4 at the Electron ``userData`` path:

  • macOS   ~/Library/Application Support/flowly-desktop/.machine-id
  • Linux   ~/.config/flowly-desktop/.machine-id
  • Windows %APPDATA%/flowly-desktop/.machine-id

We read from the SAME file. If it exists, the raw UUID is returned as
the machine id (matching desktop's wire format). If not, we generate a
fresh UUIDv4 and write it to that path — so a desktop install AFTER
TUI converges on the same id without any extra user action.

The hardware-UUID-hash fallback is kept for environments where the
file path isn't writable (read-only filesystems, locked-down sandboxes).
"""

from __future__ import annotations

import hashlib
import os
import platform
import re
import subprocess
import sys
import uuid
from functools import lru_cache
from pathlib import Path

# Electron's app.getPath('userData') uses package.json `name`, not `productName`.
# Desktop's package.json:2 = "name": "flowly-desktop".
_DESKTOP_APP_NAME = "flowly-desktop"
_UUID_RE = re.compile(r"^[0-9a-f-]{32,}$", re.IGNORECASE)


def _desktop_machine_id_path() -> Path:
    """Path matching Electron's ``app.getPath('userData')`` resolution."""
    if sys.platform == "darwin":
        return (
            Path.home() / "Library" / "Application Support"
            / _DESKTOP_APP_NAME / ".machine-id"
        )
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA", "")
        base = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
        return base / _DESKTOP_APP_NAME / ".machine-id"
    # Linux / BSD — Electron honors XDG_CONFIG_HOME, falls back to ~/.config.
    xdg = os.environ.get("XDG_CONFIG_HOME", "")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / _DESKTOP_APP_NAME / ".machine-id"


def _read_shared_machine_id() -> str | None:
    """Read the desktop-compatible machine id if the file exists + valid."""
    path = _desktop_machine_id_path()
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except (OSError, FileNotFoundError):
        return None
    if raw and _UUID_RE.match(raw):
        return raw
    return None


def _write_shared_machine_id(value: str) -> bool:
    """Persist a fresh machine id at the desktop-compatible path."""
    path = _desktop_machine_id_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value, encoding="utf-8")
        return True
    except OSError:
        return False


def _hardware_fingerprint_fallback() -> str:
    """SHA-256(hardware UUID), used only when the shared path is unwritable."""
    system = platform.system()
    raw = ""
    if system == "Darwin":
        try:
            out = subprocess.check_output(
                ["system_profiler", "SPHardwareDataType"],
                stderr=subprocess.DEVNULL, timeout=3,
            ).decode("utf-8", errors="ignore")
            for line in out.splitlines():
                if "Hardware UUID" in line:
                    raw = line.split(":", 1)[1].strip()
                    break
        except (subprocess.SubprocessError, OSError):
            pass
    elif system == "Linux":
        for p in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
            try:
                raw = Path(p).read_text(encoding="utf-8").strip()
                if raw:
                    break
            except OSError:
                continue
    elif system == "Windows":
        try:
            out = subprocess.check_output(
                ["wmic", "csproduct", "get", "uuid"],
                stderr=subprocess.DEVNULL, timeout=3,
            ).decode("utf-8", errors="ignore")
            lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
            if len(lines) >= 2:
                raw = lines[1]
        except (subprocess.SubprocessError, OSError, FileNotFoundError):
            pass

    if not raw:
        raw = platform.node() or "unknown"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


@lru_cache(maxsize=1)
def machine_id() -> str:
    """Return the machine identifier, sharing with flowly-desktop when possible.

    Priority:
      1. Read the desktop's ``.machine-id`` file → use raw UUID
      2. Generate a fresh UUIDv4 and write to that same file
         → next desktop install picks it up
      3. If write fails (locked filesystem), fall back to a SHA-256 of
         the OS hardware UUID (deterministic, but won't match desktop)
    """
    shared = _read_shared_machine_id()
    if shared:
        return shared

    fresh = str(uuid.uuid4())
    if _write_shared_machine_id(fresh):
        return fresh

    return _hardware_fingerprint_fallback()


@lru_cache(maxsize=1)
def machine_name() -> str:
    """Human-friendly device name shown in picker UIs."""
    host = platform.node() or "computer"
    for suffix in (".local", ".lan", ".home"):
        if host.lower().endswith(suffix):
            host = host[: -len(suffix)]
            break
    system = platform.system()
    if system == "Darwin":
        return host
    return f"{host} ({system})"
