"""Profile management — run multiple isolated Flowly instances.

Each profile is a fully independent Flowly environment with its own
config, memory, sessions, skills, and gateway service.

Directory layout::

    ~/.flowly/                      ← "default" profile (backward compatible)
    ~/.flowly/profiles/coder/       ← named profile "coder"
    ~/.flowly/active_profile        ← sticky default profile name

Core mechanism: ``FLOWLY_HOME`` environment variable.  Every path in the
codebase resolves via :func:`get_flowly_home`, which reads this variable.
The CLI entry point sets it *before* any module import so that all
module-level constants evaluate to the correct profile directory.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# ── Constants ──────────────────────────────────────────────────────

_ENV_VAR = "FLOWLY_HOME"
_PROFILE_ENV_VAR = "FLOWLY_PROFILE"
_DEFAULT_HOME = Path.home() / ".flowly"
_PROFILES_ROOT = Path.home() / ".flowly" / "profiles"
_PROFILE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

_RESERVED_NAMES = frozenset({
    "flowly", "default", "test", "tmp", "root", "sudo",
})

_PROFILE_SUBDIRS = [
    "workspace", "workspace/memory", "workspace/personas", "workspace/skills",
    "sessions", "skills", "credentials", "logs", "audit",
    "trajectories", "subagents", "screenshots", "media", "cron",
]

_CLONE_CONFIG_FILES = [
    "config.json", ".env",
]

_CLONE_WORKSPACE_FILES = [
    "workspace/AGENTS.md", "workspace/SOUL.md", "workspace/USER.md",
    "workspace/TOOLS.md", "workspace/IDENTITY.md",
]

_CLONE_ALL_STRIP = [
    "session_index.sqlite", "session_index.sqlite-wal",
    "session_index.sqlite-shm", "logs", "subagents",
]


# ── Path resolution ───────────────────────────────────────────────

def get_flowly_home() -> Path:
    """Return the active profile directory.

    Reads ``FLOWLY_HOME`` env var; falls back to ``~/.flowly``.
    This is the **single source of truth** — all path helpers must use it.
    """
    raw = os.environ.get(_ENV_VAR)
    if raw:
        return Path(raw)
    return _DEFAULT_HOME


def display_flowly_home() -> str:
    """Return a user-friendly display string for the current home.

    ``~/.flowly`` for default, ``~/.flowly/profiles/coder`` for named.
    """
    home = get_flowly_home()
    try:
        return "~/" + str(home.relative_to(Path.home()))
    except ValueError:
        return str(home)


# ── Profile selection ─────────────────────────────────────────────

def set_profile(name: str | None) -> Path:
    """Set ``FLOWLY_HOME`` for a named profile.

    Must be called **before** any Flowly module import.
    Returns the resolved profile directory.
    """
    if name is None or name == "default":
        home = _DEFAULT_HOME
    else:
        validate_profile_name(name)
        home = _PROFILES_ROOT / name
    os.environ[_ENV_VAR] = str(home)
    return home


def get_active_profile() -> str:
    """Read the sticky active profile from ``~/.flowly/active_profile``."""
    path = _DEFAULT_HOME / "active_profile"
    try:
        name = path.read_text().strip()
        return name if name and name != "default" else "default"
    except (FileNotFoundError, UnicodeDecodeError, OSError):
        return "default"


def set_active_profile(name: str) -> None:
    """Write the sticky active profile."""
    if name != "default":
        validate_profile_name(name)
        if not profile_exists(name):
            raise FileNotFoundError(
                f"Profile '{name}' does not exist. "
                f"Create it with: flowly profile create {name}"
            )
    path = _DEFAULT_HOME / "active_profile"
    path.parent.mkdir(parents=True, exist_ok=True)
    if name == "default":
        path.unlink(missing_ok=True)
    else:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(name + "\n")
        tmp.replace(path)


def get_active_profile_name() -> str:
    """Infer the current profile name from ``FLOWLY_HOME``."""
    home = get_flowly_home().resolve()
    if home == _DEFAULT_HOME.resolve():
        return "default"
    try:
        rel = home.relative_to(_PROFILES_ROOT.resolve())
        parts = rel.parts
        if len(parts) == 1 and _PROFILE_NAME_RE.match(parts[0]):
            return parts[0]
    except ValueError:
        pass
    return "custom"


# ── Validation ────────────────────────────────────────────────────

def validate_profile_name(name: str) -> None:
    """Raise ValueError if name is invalid."""
    if not name:
        raise ValueError("Profile name is required.")
    if name in _RESERVED_NAMES:
        raise ValueError(f"'{name}' is a reserved name.")
    if not _PROFILE_NAME_RE.match(name):
        raise ValueError(
            f"Invalid profile name '{name}'. "
            "Use lowercase letters, digits, hyphens, underscores (max 64 chars)."
        )


def profile_exists(name: str) -> bool:
    """Check if a named profile exists."""
    if name == "default":
        return True
    return (_PROFILES_ROOT / name).is_dir()


# ── Profile info ──────────────────────────────────────────────────

@dataclass
class ProfileInfo:
    """Summary information about a profile."""
    name: str
    path: Path
    is_default: bool
    has_config: bool = False
    skill_count: int = 0


def list_profiles() -> list[ProfileInfo]:
    """List all profiles (default + named)."""
    profiles = []

    # Default profile
    profiles.append(ProfileInfo(
        name="default",
        path=_DEFAULT_HOME,
        is_default=True,
        has_config=(_DEFAULT_HOME / "config.json").exists(),
    ))

    # Named profiles
    if _PROFILES_ROOT.exists():
        for d in sorted(_PROFILES_ROOT.iterdir()):
            if d.is_dir() and _PROFILE_NAME_RE.match(d.name):
                skill_count = 0
                skills_dir = d / "skills"
                if skills_dir.exists():
                    skill_count = sum(1 for s in skills_dir.iterdir() if s.is_dir())
                profiles.append(ProfileInfo(
                    name=d.name,
                    path=d,
                    is_default=False,
                    has_config=(d / "config.json").exists(),
                    skill_count=skill_count,
                ))

    return profiles


# ── CRUD ──────────────────────────────────────────────────────────

def create_profile(
    name: str,
    clone_from: str | None = None,
    clone_all: bool = False,
) -> Path:
    """Create a new profile directory.

    Args:
        name: Profile identifier.
        clone_from: Source profile to clone from (default: active profile).
        clone_all: If True, full copy including sessions/memory.
    """
    validate_profile_name(name)
    if name == "default":
        raise ValueError("Cannot create a profile named 'default'.")

    profile_dir = _PROFILES_ROOT / name
    if profile_dir.exists():
        raise FileExistsError(f"Profile '{name}' already exists at {profile_dir}")

    # Resolve clone source
    source_dir = None
    if clone_from is not None or clone_all:
        if clone_from is None or clone_from == "default":
            source_dir = _DEFAULT_HOME
        else:
            validate_profile_name(clone_from)
            source_dir = _PROFILES_ROOT / clone_from
        if not source_dir.is_dir():
            raise FileNotFoundError(f"Source profile does not exist at {source_dir}")

    if clone_all and source_dir:
        shutil.copytree(source_dir, profile_dir)
        # Strip runtime files
        for stale in _CLONE_ALL_STRIP:
            p = profile_dir / stale
            if p.is_file():
                p.unlink(missing_ok=True)
            elif p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
    else:
        # Bootstrap directory structure
        for subdir in _PROFILE_SUBDIRS:
            (profile_dir / subdir).mkdir(parents=True, exist_ok=True)

        # Clone config files
        if source_dir:
            for f in _CLONE_CONFIG_FILES + _CLONE_WORKSPACE_FILES:
                src = source_dir / f
                if src.exists():
                    dst = profile_dir / f
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dst)

            # Clone persona files
            src_personas = source_dir / "workspace" / "personas"
            if src_personas.exists():
                dst_personas = profile_dir / "workspace" / "personas"
                if src_personas.is_dir():
                    shutil.copytree(src_personas, dst_personas, dirs_exist_ok=True)

    return profile_dir


def delete_profile(name: str) -> None:
    """Delete a named profile."""
    validate_profile_name(name)
    if name == "default":
        raise ValueError("Cannot delete the default profile.")

    profile_dir = _PROFILES_ROOT / name
    if not profile_dir.exists():
        raise FileNotFoundError(f"Profile '{name}' does not exist.")

    shutil.rmtree(profile_dir)

    # Clean up active_profile if it pointed to deleted profile
    if get_active_profile() == name:
        set_active_profile("default")

    # Remove wrapper script
    remove_wrapper_script(name)


# ── Export / Import ───────────────────────────────────────────────

def export_profile(name: str, output_path: str) -> Path:
    """Export a profile to a tar.gz archive."""
    validate_profile_name(name)
    profile_dir = _PROFILES_ROOT / name if name != "default" else _DEFAULT_HOME
    if not profile_dir.is_dir():
        raise FileNotFoundError(f"Profile '{name}' does not exist.")

    base = str(output_path).removesuffix(".tar.gz").removesuffix(".tgz")
    result = shutil.make_archive(base, "gztar", str(profile_dir.parent), profile_dir.name)
    return Path(result)


def import_profile(archive_path: str, name: str | None = None) -> Path:
    """Import a profile from a tar.gz archive."""
    import tarfile

    archive = Path(archive_path)
    if not archive.exists():
        raise FileNotFoundError(f"Archive not found: {archive}")

    with tarfile.open(archive, "r:gz") as tf:
        top_dirs = {m.name.split("/")[0] for m in tf.getmembers() if "/" in m.name}

    inferred = name or (top_dirs.pop() if len(top_dirs) == 1 else None)
    if not inferred:
        raise ValueError("Cannot determine profile name from archive. Specify --name.")

    validate_profile_name(inferred)
    profile_dir = _PROFILES_ROOT / inferred
    if profile_dir.exists():
        raise FileExistsError(f"Profile '{inferred}' already exists.")

    _PROFILES_ROOT.mkdir(parents=True, exist_ok=True)
    shutil.unpack_archive(str(archive), str(_PROFILES_ROOT))

    extracted = _PROFILES_ROOT / (top_dirs.pop() if top_dirs else inferred)
    if extracted != profile_dir and extracted.exists():
        extracted.rename(profile_dir)

    return profile_dir


# ── Wrapper scripts ───────────────────────────────────────────────

def _get_wrapper_dir() -> Path:
    return Path.home() / ".local" / "bin"


def create_wrapper_script(name: str) -> Optional[Path]:
    """Create a profile wrapper at ``~/.local/bin/<name>``.

    Windows: writes ``<name>.bat`` (no shebang, no chmod needed).
    Unix:    writes a POSIX shell script with exec bit set.
    """
    wrapper_dir = _get_wrapper_dir()
    try:
        wrapper_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None

    if os.name == "nt":
        # Windows .bat wrapper — use %* to forward all args.
        # setlocal scopes the env var to this script invocation only.
        # newline="" disables Python's newline translation so that our
        # explicit CRLFs land on disk as \r\n (not \r\r\n on Windows).
        wrapper_path = wrapper_dir / f"{name}.bat"
        try:
            content = (
                f"@echo off\r\n"
                f"setlocal\r\n"
                f"set {_PROFILE_ENV_VAR}={name}\r\n"
                f"flowly %*\r\n"
            )
            with open(wrapper_path, "w", encoding="utf-8", newline="") as fh:
                fh.write(content)
            return wrapper_path
        except OSError:
            return None

    wrapper_path = wrapper_dir / name
    try:
        wrapper_path.write_text(
            f'#!/bin/sh\nexec env {_PROFILE_ENV_VAR}={name} flowly "$@"\n'
        )
        wrapper_path.chmod(
            wrapper_path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH
        )
        return wrapper_path
    except OSError:
        return None


def remove_wrapper_script(name: str) -> bool:
    """Remove the wrapper script for a profile.

    Checks ``<name>.bat`` first on Windows, then falls back to the
    extension-less Unix form (so existing mixed setups stay working).
    """
    wrapper_dir = _get_wrapper_dir()
    candidates = (
        [wrapper_dir / f"{name}.bat", wrapper_dir / name]
        if os.name == "nt"
        else [wrapper_dir / name]
    )
    for wrapper_path in candidates:
        if wrapper_path.exists():
            try:
                content = wrapper_path.read_text()
                if _PROFILE_ENV_VAR in content:
                    wrapper_path.unlink()
                    return True
            except Exception:
                pass
    return False


# ── Service helpers ───────────────────────────────────────────────

def get_service_label() -> str:
    """Return the service label scoped to the active profile.

    Default: ``ai.flowly.gateway``
    Named:  ``ai.flowly.gateway-coder``
    """
    name = get_active_profile_name()
    if name == "default" or name == "custom":
        return "ai.flowly.gateway"
    return f"ai.flowly.gateway-{name}"
