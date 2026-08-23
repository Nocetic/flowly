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

import json
import os
import re
import shutil
import stat
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
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
    "session_index.sqlite-shm", "logs", "subagents", ".machine-id",
    ".desktop-runtime.json",
]

_PROFILE_METADATA_FILE = "profile.json"
_RUNTIME_LEASE_FILE = ".desktop-runtime.json"
_MAX_SOUL_BYTES = 64 * 1024
_MAX_MODEL_LENGTH = 256
_PROFILE_MARK_TONES = frozenset({
    "aqua", "violet", "rose", "amber", "lime", "sky", "slate",
})
_LOCAL_RUNTIME_ENV_DROP = frozenset({
    "FLOWLY_SERVER_ID",
    "MOLTBOT_PROXY_JWT_SECRET",
})


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


def default_home() -> Path:
    """The canonical home (``~/.flowly``), regardless of what is active.

    ``get_flowly_home()`` answers "where am I writing?"; this answers "which
    home owns the shared, non-profile-scoped resources?" — chiefly the
    background-service label, which is a single global name every home would
    otherwise fight over. Callers that need to tell "this unit is mine" from
    "this unit belongs to the default install" compare against this.
    """
    return _DEFAULT_HOME


def is_default_home() -> bool:
    """True iff the active ``FLOWLY_HOME`` is the default (``~/.flowly``)."""
    return get_flowly_home() == _DEFAULT_HOME


def current_profile_name() -> str:
    """Return the stable identifier for the process' active profile.

    Unlike ``get_active_profile()``, this reflects ``FLOWLY_HOME`` selected for
    this process instead of the user's sticky CLI preference. Desktop-managed
    runtimes rely on that distinction when brokering profile-to-profile work.
    """
    home = get_flowly_home()
    if home == _DEFAULT_HOME:
        return "default"
    try:
        relative = home.relative_to(_PROFILES_ROOT)
    except ValueError:
        return "default"
    if len(relative.parts) == 1 and _PROFILE_NAME_RE.fullmatch(relative.name):
        return relative.name
    return "default"


def credential_scope_suffix() -> str:
    """Suffix identifying the active ``FLOWLY_HOME``, for scoping OS-keychain
    service names.

    Keychain entries are keyed by ``(service, account)``, not by file path —
    unlike file-backed credentials, a keychain service name is shared by
    every process that names it, regardless of ``FLOWLY_HOME``. Without this,
    two engines running under different homes on the same OS user account
    (e.g. a second product built on this codebase, or a named profile) would
    silently read and write each other's stored tokens.

    Returns ``""`` for the default home (``~/.flowly``) so existing keychain
    entries there keep resolving under their current, unsuffixed service name
    — no re-login for the overwhelming majority of installs. Any other home
    gets a short deterministic suffix so its secrets never collide with
    another home's.
    """
    if is_default_home():
        return ""
    import hashlib
    return hashlib.sha256(str(get_flowly_home()).encode()).hexdigest()[:12]


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
    display_name: str = ""
    description: str = ""
    provider: str = ""
    model: str = ""
    mark_text: str = ""
    mark_tone: str = ""
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict:
        """Return the stable, JSON-safe profile descriptor used by clients."""
        return {
            "name": self.name,
            "path": str(self.path),
            "isDefault": self.is_default,
            "hasConfig": self.has_config,
            "skillCount": self.skill_count,
            "displayName": self.display_name or self.name,
            "description": self.description,
            "provider": self.provider,
            "model": self.model,
            "markText": self.mark_text,
            "markTone": self.mark_tone,
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
        }


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _profile_metadata(profile_dir: Path) -> dict:
    path = profile_dir / _PROFILE_METADATA_FILE
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _atomic_write_json(path: Path, value: dict) -> None:
    """Write owner-only JSON without exposing a partial profile descriptor."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp = Path(raw_tmp)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        try:
            tmp.chmod(0o600)
        except OSError:
            pass
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _atomic_write_text(path: Path, value: str) -> None:
    """Write owner-only UTF-8 text without exposing a partial file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp = Path(raw_tmp)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            tmp.chmod(0o600)
        except OSError:
            pass
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _validate_model(model: str) -> str:
    value = model.strip()
    if not value:
        raise ValueError("Model must not be empty.")
    if len(value) > _MAX_MODEL_LENGTH or "\x00" in value:
        raise ValueError(f"Model must be at most {_MAX_MODEL_LENGTH} characters.")
    return value


def _validate_provider(provider: str) -> str:
    value = provider.strip()
    if not value:
        raise ValueError("Provider must not be empty.")
    from flowly.config.schema import ProvidersConfig

    if value == "active" or value not in ProvidersConfig.model_fields:
        raise ValueError(f"Unknown model provider: {value}")
    return value


def _validate_soul(soul: str) -> str:
    if "\x00" in soul:
        raise ValueError("Persona instructions cannot contain null bytes.")
    if len(soul.encode("utf-8")) > _MAX_SOUL_BYTES:
        raise ValueError(f"Persona instructions must be at most {_MAX_SOUL_BYTES // 1024} KiB.")
    return soul


def _validate_mark_text(mark_text: str) -> str:
    value = mark_text.strip().upper()
    if not value:
        return ""
    if len(value) > 2 or not all(char.isalnum() for char in value):
        raise ValueError("Profile mark must contain one or two letters or numbers.")
    return value


def _validate_mark_tone(mark_tone: str) -> str:
    value = mark_tone.strip().lower()
    if not value:
        return ""
    if value not in _PROFILE_MARK_TONES:
        raise ValueError(f"Unknown profile mark tone: {value}")
    return value


def _load_config_object(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Invalid profile config.json: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError("Invalid profile config.json: root must be an object")
    return value


def _set_profile_model(config: dict, model: str) -> None:
    agents = config.setdefault("agents", {})
    if not isinstance(agents, dict):
        agents = {}
        config["agents"] = agents
    defaults = agents.setdefault("defaults", {})
    if not isinstance(defaults, dict):
        defaults = {}
        agents["defaults"] = defaults
    defaults["model"] = _validate_model(model)


def _set_profile_provider(config: dict, provider: str) -> None:
    providers = config.setdefault("providers", {})
    if not isinstance(providers, dict):
        providers = {}
        config["providers"] = providers
    providers["active"] = _validate_provider(provider)


def _set_profile_workspace(config: dict, workspace: Path) -> None:
    agents = config.setdefault("agents", {})
    if not isinstance(agents, dict):
        agents = {}
        config["agents"] = agents
    defaults = agents.setdefault("defaults", {})
    if not isinstance(defaults, dict):
        defaults = {}
        agents["defaults"] = defaults
    defaults["workspace"] = str(workspace)


def _metadata_for(name: str, profile_dir: Path, *, is_default: bool) -> dict:
    meta = _profile_metadata(profile_dir)
    return {
        "display_name": str(meta.get("displayName") or ("Flowly" if is_default else name)).strip(),
        "description": str(meta.get("description") or "").strip(),
        "mark_text": str(meta.get("markText") or "").strip(),
        "mark_tone": str(meta.get("markTone") or "").strip(),
        "created_at": str(meta.get("createdAt") or "").strip(),
        "updated_at": str(meta.get("updatedAt") or "").strip(),
    }


def _runtime_summary(profile_dir: Path) -> dict:
    """Return non-secret provider/model fields for roster clients.

    Profile lists must remain cheap and must never initialize a provider or
    touch credential stores. Reading the two explicit config fields gives the
    desktop enough information to label stopped profiles without starting all
    of their gateways.
    """
    try:
        config = _load_config_object(profile_dir / "config.json")
    except ValueError:
        return {"provider": "", "model": ""}
    agents = config.get("agents") if isinstance(config.get("agents"), dict) else {}
    defaults = agents.get("defaults") if isinstance(agents.get("defaults"), dict) else {}
    providers = config.get("providers") if isinstance(config.get("providers"), dict) else {}
    return {
        "provider": str(providers.get("active") or "").strip(),
        "model": str(defaults.get("model") or "").strip(),
    }


def list_profiles() -> list[ProfileInfo]:
    """List all profiles (default + named)."""
    profiles = []

    # Default profile
    default_meta = _metadata_for("default", _DEFAULT_HOME, is_default=True)
    default_runtime = _runtime_summary(_DEFAULT_HOME)
    profiles.append(ProfileInfo(
        name="default",
        path=_DEFAULT_HOME,
        is_default=True,
        has_config=(_DEFAULT_HOME / "config.json").exists(),
        **default_meta,
        **default_runtime,
    ))

    # Named profiles
    if _PROFILES_ROOT.exists():
        for d in sorted(_PROFILES_ROOT.iterdir()):
            if d.is_dir() and _PROFILE_NAME_RE.match(d.name):
                skill_count = 0
                skills_dir = d / "skills"
                if skills_dir.exists():
                    skill_count = sum(1 for s in skills_dir.iterdir() if s.is_dir())
                meta = _metadata_for(d.name, d, is_default=False)
                runtime = _runtime_summary(d)
                profiles.append(ProfileInfo(
                    name=d.name,
                    path=d,
                    is_default=False,
                    has_config=(d / "config.json").exists(),
                    skill_count=skill_count,
                    **meta,
                    **runtime,
                ))

    return profiles


# ── CRUD ──────────────────────────────────────────────────────────

def create_profile(
    name: str,
    clone_from: str | None = None,
    clone_all: bool = False,
    *,
    display_name: str = "",
    description: str = "",
    local_runtime: bool = False,
    provider: str | None = None,
    model: str | None = None,
    soul: str | None = None,
    mark_text: str = "",
    mark_tone: str = "",
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

    _PROFILES_ROOT.mkdir(parents=True, exist_ok=True)
    # Stage outside ``~/.flowly``. A full clone of the default profile must not
    # recursively copy its own ``profiles/`` directory (including this staging
    # directory) into itself. The default home's parent is on the same volume,
    # so the final ``os.replace`` remains atomic.
    staging_parent = _DEFAULT_HOME.parent
    staging_parent.mkdir(parents=True, exist_ok=True)
    temp_dir = Path(tempfile.mkdtemp(prefix=f".flowly-profile-{name}.", dir=staging_parent))

    try:
        if clone_all and source_dir:
            ignore = shutil.ignore_patterns("profiles", "active_profile") if source_dir == _DEFAULT_HOME else None
            shutil.copytree(source_dir, temp_dir, dirs_exist_ok=True, ignore=ignore)
            # Strip runtime files
            for stale in _CLONE_ALL_STRIP:
                p = temp_dir / stale
                if p.is_file() or p.is_symlink():
                    p.unlink(missing_ok=True)
                elif p.is_dir():
                    shutil.rmtree(p, ignore_errors=True)
        else:
            # Bootstrap directory structure
            for subdir in _PROFILE_SUBDIRS:
                (temp_dir / subdir).mkdir(parents=True, exist_ok=True)

            # Clone config files
            if source_dir:
                for f in _CLONE_CONFIG_FILES + _CLONE_WORKSPACE_FILES:
                    src = source_dir / f
                    if src.exists() and src.is_file():
                        dst = temp_dir / f
                        dst.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(src, dst)

                # Clone persona files
                src_personas = source_dir / "workspace" / "personas"
                if src_personas.is_dir():
                    dst_personas = temp_dir / "workspace" / "personas"
                    shutil.copytree(src_personas, dst_personas, dirs_exist_ok=True)

        if local_runtime:
            _sanitize_local_runtime_clone(temp_dir, profile_dir / "workspace")

        if provider is not None or model is not None:
            config_path = temp_dir / "config.json"
            config = _load_config_object(config_path)
            if provider is not None:
                _set_profile_provider(config, provider)
            if model is not None:
                _set_profile_model(config, model)
            _atomic_write_json(config_path, config)

        if soul is not None:
            _atomic_write_text(temp_dir / "workspace" / "SOUL.md", _validate_soul(soul))

        now = _utc_now()
        _atomic_write_json(temp_dir / _PROFILE_METADATA_FILE, {
            "version": 1,
            "displayName": display_name.strip() or name,
            "description": description.strip(),
            "markText": _validate_mark_text(mark_text),
            "markTone": _validate_mark_tone(mark_tone),
            "createdAt": now,
            "updatedAt": now,
            "localRuntime": bool(local_runtime),
        })

        # Publishing the completed directory is the commit point. A crash
        # before this line leaves only a hidden temp dir, never a half-profile.
        os.replace(temp_dir, profile_dir)
    except BaseException:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise

    return profile_dir


def _sanitize_local_runtime_clone(profile_dir: Path, workspace: Path) -> None:
    """Remove transport identity from a Desktop-managed local profile clone.

    Provider credentials stay intact so the new profile can use the selected
    model immediately. Messaging-channel credentials are not copied into the
    runnable config, and legacy hosted-provider relay credentials are removed;
    the account-scoped provider key remains valid without a relay registration.
    """
    config_path = profile_dir / "config.json"
    if config_path.exists():
        try:
            raw = _load_config_object(config_path)
        except ValueError as exc:
            raise ValueError(str(exc).replace("Invalid profile", "Cannot clone invalid")) from exc

        channels = raw.get("channels")
        if isinstance(channels, dict):
            for channel in channels.values():
                if isinstance(channel, dict):
                    channel["enabled"] = False
            channels["web"] = {"enabled": False}

        gateway = raw.get("gateway")
        if not isinstance(gateway, dict):
            gateway = {}
            raw["gateway"] = gateway
        gateway.update({"host": "127.0.0.1", "token": ""})

        providers = raw.get("providers")
        if isinstance(providers, dict):
            hosted = providers.get("flowlyHosted")
            if not isinstance(hosted, dict):
                hosted = providers.get("flowly_hosted")
            if isinstance(hosted, dict):
                hosted.pop("serverId", None)
                hosted.pop("server_id", None)
                hosted.pop("authToken", None)
                hosted.pop("auth_token", None)

        _set_profile_workspace(raw, workspace)
        _atomic_write_json(config_path, raw)
    else:
        raw = {}
        _set_profile_workspace(raw, workspace)
        _atomic_write_json(config_path, raw)

    env_path = profile_dir / ".env"
    if env_path.exists():
        try:
            lines = env_path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError) as exc:
            raise ValueError(f"Cannot clone invalid .env: {exc}") from exc
        retained = []
        for line in lines:
            key = line.split("=", 1)[0].strip() if "=" in line else ""
            if key not in _LOCAL_RUNTIME_ENV_DROP:
                retained.append(line)
        env_path.write_text("\n".join(retained) + ("\n" if retained else ""), encoding="utf-8")


def describe_profile(name: str) -> ProfileInfo:
    """Return one profile descriptor or raise ``FileNotFoundError``."""
    if name == "default":
        return list_profiles()[0]
    validate_profile_name(name)
    for profile in list_profiles()[1:]:
        if profile.name == name:
            return profile
    raise FileNotFoundError(f"Profile '{name}' does not exist.")


def update_profile_metadata(
    name: str,
    *,
    display_name: str | None = None,
    description: str | None = None,
    mark_text: str | None = None,
    mark_tone: str | None = None,
) -> ProfileInfo:
    """Atomically update renderer-facing metadata for one profile."""
    profile = describe_profile(name)
    current = _profile_metadata(profile.path)
    now = _utc_now()
    current.update({
        "version": 1,
        "displayName": (
            str(display_name).strip() if display_name is not None
            else str(current.get("displayName") or profile.display_name or name).strip()
        ),
        "description": (
            str(description).strip() if description is not None
            else str(current.get("description") or profile.description).strip()
        ),
        "markText": (
            _validate_mark_text(mark_text) if mark_text is not None
            else _validate_mark_text(str(current.get("markText") or profile.mark_text))
        ),
        "markTone": (
            _validate_mark_tone(mark_tone) if mark_tone is not None
            else _validate_mark_tone(str(current.get("markTone") or profile.mark_tone))
        ),
        "createdAt": str(current.get("createdAt") or profile.created_at or now),
        "updatedAt": now,
    })
    _atomic_write_json(profile.path / _PROFILE_METADATA_FILE, current)
    return describe_profile(name)


def read_profile_settings(name: str) -> dict:
    """Return the non-secret settings exposed in Desktop's profile editor."""
    profile = describe_profile(name)
    config = _load_config_object(profile.path / "config.json")
    agents = config.get("agents") if isinstance(config.get("agents"), dict) else {}
    defaults = agents.get("defaults") if isinstance(agents.get("defaults"), dict) else {}
    providers = config.get("providers") if isinstance(config.get("providers"), dict) else {}
    soul_path = profile.path / "workspace" / "SOUL.md"
    try:
        soul = soul_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        soul = ""
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError(f"Cannot read profile persona instructions: {exc}") from exc
    return {
        "name": profile.name,
        "provider": str(providers.get("active") or "").strip(),
        "model": str(defaults.get("model") or "").strip(),
        "soul": soul,
        "workspace": str(profile.path / "workspace"),
    }


def update_profile_settings(
    name: str,
    *,
    provider: str | None = None,
    model: str | None = None,
    soul: str | None = None,
) -> dict:
    """Safely update profile-local model and persona files."""
    if provider is None and model is None and soul is None:
        raise ValueError("At least one profile setting is required.")
    validated_provider = _validate_provider(provider) if provider is not None else None
    validated_model = _validate_model(model) if model is not None else None
    validated_soul = _validate_soul(soul) if soul is not None else None
    profile = describe_profile(name)
    lease = read_runtime_lease(profile.path)
    if lease:
        raise RuntimeError(
            f"Profile runtime is active (pid {lease.get('pid')}). Stop it before changing settings."
        )

    if validated_provider is not None or validated_model is not None:
        config_path = profile.path / "config.json"
        config = _load_config_object(config_path)
        if bool(_profile_metadata(profile.path).get("localRuntime")):
            _set_profile_workspace(config, profile.path / "workspace")
        if validated_provider is not None:
            _set_profile_provider(config, validated_provider)
        if validated_model is not None:
            _set_profile_model(config, validated_model)
        _atomic_write_json(config_path, config)
    if validated_soul is not None:
        _atomic_write_text(profile.path / "workspace" / "SOUL.md", validated_soul)
    return read_profile_settings(name)


def _pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def read_runtime_lease(profile_dir: Path) -> dict | None:
    """Return a live Desktop-runtime lease, pruning a stale lease."""
    path = profile_dir / _RUNTIME_LEASE_FILE
    try:
        lease = json.loads(path.read_text(encoding="utf-8"))
        pid = int(lease.get("pid") or 0) if isinstance(lease, dict) else 0
    except (FileNotFoundError, OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return None
    if _pid_is_alive(pid):
        return lease
    path.unlink(missing_ok=True)
    return None


def claim_runtime_lease(instance_id: str) -> Path:
    """Exclusively claim the current profile for one managed runtime."""
    if not instance_id:
        raise ValueError("runtime instance id is required")
    path = get_flowly_home() / _RUNTIME_LEASE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = read_runtime_lease(path.parent)
    if existing:
        raise RuntimeError(
            f"Profile runtime is already active (pid {existing.get('pid')})."
        )
    path.unlink(missing_ok=True)
    payload = {
        "version": 1,
        "instanceId": instance_id,
        "pid": os.getpid(),
        "port": 0,
        "startedAt": _utc_now(),
    }
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(path, flags, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return path


def update_runtime_lease(instance_id: str, *, port: int) -> None:
    """Publish the bound port without changing lease ownership."""
    path = get_flowly_home() / _RUNTIME_LEASE_FILE
    lease = read_runtime_lease(path.parent)
    if not lease or lease.get("instanceId") != instance_id or lease.get("pid") != os.getpid():
        raise RuntimeError("Profile runtime lease ownership was lost.")
    lease["port"] = int(port)
    lease["readyAt"] = _utc_now()
    _atomic_write_json(path, lease)


def release_runtime_lease(instance_id: str) -> None:
    """Release only the lease owned by this runtime instance."""
    path = get_flowly_home() / _RUNTIME_LEASE_FILE
    try:
        lease = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, UnicodeDecodeError, json.JSONDecodeError):
        return
    if isinstance(lease, dict) and lease.get("instanceId") == instance_id:
        path.unlink(missing_ok=True)


def delete_profile(name: str) -> None:
    """Delete a named profile."""
    validate_profile_name(name)
    if name == "default":
        raise ValueError("Cannot delete the default profile.")

    profile_dir = _PROFILES_ROOT / name
    if not profile_dir.exists():
        raise FileNotFoundError(f"Profile '{name}' does not exist.")

    lease = read_runtime_lease(profile_dir)
    if lease:
        raise RuntimeError(
            f"Profile '{name}' is running (pid {lease.get('pid')}). Stop it before deletion."
        )

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
