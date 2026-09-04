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
import shlex
import shutil
import signal
import stat
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from filelock import FileLock

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
_PROFILE_HOST_FILE = "profile-host.json"
_RUNTIME_LEASE_FILE = ".desktop-runtime.json"
_PROFILE_MUTATION_LOCK_FILE = ".profiles.lock"
MAX_NAMED_PROFILES = 15
_MAX_SOUL_BYTES = 64 * 1024
_MAX_MODEL_LENGTH = 256
_MAX_IMPORT_MEMBERS = 20_000
_MAX_IMPORT_BYTES = 512 * 1024 * 1024
_BACKUP_MAGIC = b"FLOWLY-BACKUP\x00\x01"
_BACKUP_SALT_BYTES = 16
_BACKUP_NONCE_BYTES = 12
_BACKUP_TAG_BYTES = 16
_BACKUP_CHUNK_BYTES = 1024 * 1024
_PROFILE_MARK_TONES = frozenset({
    "aqua", "violet", "rose", "amber", "lime", "sky", "slate",
})
_PROFILE_MARK_COLOR_RE = re.compile(r"^#[0-9a-f]{6}$")
_NAMED_PROFILE_CREDENTIAL_POLICY = "isolated"
# The only secrets a clone may inherit. Everything else in a parent's ``.env``
# is dropped, because ``.env`` is where identity lives and there is no way to
# tell an unknown key from a credential — ``FLOWLY_CHANNELS__TELEGRAM__BOT_TOKEN``
# is a perfectly ordinary-looking variable. Provider keys are the deliberate
# exception: a new bot has to be able to answer with the model it was given.
_CLONED_ENV_ALLOW = frozenset({
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "OPENROUTER_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "GROQ_API_KEY",
    "XAI_API_KEY",
    "ZAI_API_KEY",
    "ZHIPU_API_KEY",
    "ZHIPUAI_API_KEY",
    "VLLM_API_KEY",
})


class ProfileLimitError(ValueError):
    """Raised when an installation reached its named-profile product limit."""


class ProfileIdentityConflictError(ValueError):
    """Raised when a restore would publish a duplicate stable bot identity."""


def _assert_named_profile_capacity() -> None:
    # Deliberately not tolerant of an unreadable sibling: this counts against
    # a hard limit, and skipping what we cannot examine would undercount and
    # let the limit be passed. It only runs where creating a bot is allowed,
    # which is a context that may read every profile.
    count = sum(
        1
        for candidate in _PROFILES_ROOT.iterdir()
        if candidate.is_dir()
        and not candidate.is_symlink()
        and _PROFILE_NAME_RE.fullmatch(candidate.name)
    )
    if count >= MAX_NAMED_PROFILES:
        raise ProfileLimitError(
            f"This installation can contain at most {MAX_NAMED_PROFILES} bots. "
            "Delete one before creating another."
        )


def _profile_mutation_lock() -> FileLock:
    _PROFILES_ROOT.mkdir(parents=True, exist_ok=True)
    return FileLock(str(_PROFILES_ROOT / _PROFILE_MUTATION_LOCK_FILE))


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
    return _is_describable_profile_dir(_PROFILES_ROOT / name)


# ── Profile info ──────────────────────────────────────────────────

@dataclass
class ProfileInfo:
    """Summary information about a profile."""
    name: str
    path: Path
    is_default: bool
    has_config: bool = False
    # None means the directory could not be inspected. Clients must not turn an
    # unavailable measurement into a convincing but false zero.
    skill_count: int | None = None
    display_name: str = ""
    description: str = ""
    provider: str = ""
    model: str = ""
    mark_text: str = ""
    mark_tone: str = ""
    # Creation-order seed behind the profile's generated mark. None means a
    # profile older than the seed, which clients render from its name instead.
    mark_seed: int | None = None
    created_at: str = ""
    updated_at: str = ""
    bot_id: str = ""
    credential_policy: str = ""

    def to_dict(self) -> dict:
        """Return the stable, JSON-safe profile descriptor used by clients."""
        value = {
            "name": self.name,
            "path": str(self.path),
            "isDefault": self.is_default,
            "hasConfig": self.has_config,
            "displayName": self.display_name or self.name,
            "description": self.description,
            "provider": self.provider,
            "model": self.model,
            "markText": self.mark_text,
            "markTone": self.mark_tone,
            "markSeed": self.mark_seed,
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
            "botId": self.bot_id,
            "credentialPolicy": self.credential_policy,
        }
        if self.skill_count is not None:
            value["skillCount"] = self.skill_count
            value["skillCountVerified"] = True
        return value

    def to_public_dict(self) -> dict:
        """Return the remote-safe profile descriptor.

        ``path`` is intentionally omitted: remote Desktop/iOS clients need a
        stable bot identity and presentation metadata, never the host's local
        filesystem layout.
        """
        value = self.to_dict()
        value.pop("path", None)
        return value


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
    if value not in _PROFILE_MARK_TONES and not _PROFILE_MARK_COLOR_RE.fullmatch(value):
        raise ValueError(f"Unknown profile mark tone: {value}")
    return value


_MARK_SEED_MAX = 1_000_000


def _validate_mark_seed(mark_seed: object) -> int | None:
    """Strict check for a seed arriving from a client.

    Writes are validated; reads are not (see ``_read_mark_seed``). A caller
    handing us a bad seed should hear about it, but a hand-edited metadata
    file must never make a profile unlistable.
    """
    if mark_seed is None:
        return None
    # bool is a subclass of int, and True would silently become seed 1.
    if isinstance(mark_seed, bool):
        raise ValueError("Profile mark seed must be a whole number.")
    if isinstance(mark_seed, str):
        text = mark_seed.strip()
        if not text:
            return None
        if not text.isdigit():
            raise ValueError("Profile mark seed must be a whole number.")
        value = int(text)
    elif isinstance(mark_seed, int):
        value = mark_seed
    else:
        raise ValueError("Profile mark seed must be a whole number.")
    if value < 0 or value > _MARK_SEED_MAX:
        raise ValueError(
            f"Profile mark seed must be between 0 and {_MARK_SEED_MAX}."
        )
    return value


def _read_mark_seed(value: object) -> int | None:
    """Lenient read: anything unusable degrades to 'no seed assigned'."""
    try:
        return _validate_mark_seed(value)
    except ValueError:
        return None


def _path_exists(path: Path) -> bool:
    """``path.exists()`` for a path we may not be allowed to look at.

    ``Path.exists()`` only swallows the errors that mean "no such file";
    a sandbox denial arrives as ``EPERM`` from ``stat`` and is raised at
    the caller. A named bot runs under a policy that denies it the
    primary profile, so asking whether the primary's config exists is a
    normal question with an answer, not an error: unreadable is reported
    as absent, which is what an unreadable file is worth to a caller
    that cannot open it either.
    """
    try:
        return path.exists()
    except OSError:
        return False


def _load_config_object(path: Path) -> dict:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError(f"Invalid profile config.json: {exc}") from exc
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
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
        "mark_seed": _read_mark_seed(meta.get("markSeed")),
        "created_at": str(meta.get("createdAt") or "").strip(),
        "updated_at": str(meta.get("updatedAt") or "").strip(),
        "bot_id": str(meta.get("botId") or "").strip(),
        "credential_policy": (
            "primary"
            if is_default
            else _NAMED_PROFILE_CREDENTIAL_POLICY
        ),
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


def _installed_skill_count(profile_dir: Path) -> int | None:
    """Count the skills the profile runtime will actually enumerate.

    This deliberately mirrors ``SkillsLoader.list_skills(False)`` instead of
    counting arbitrary child directories. It includes workspace, profile-
    managed, and bundled skills with the loader's normal name de-duplication.
    A failed filesystem read is represented as unknown so remote clients never
    display a fabricated zero.
    """
    from flowly.agent.skills import SkillsLoader

    try:
        loader = SkillsLoader(
            workspace=profile_dir / "workspace",
            managed_skills_dir=profile_dir / "skills",
        )
        return len(loader.list_skills(filter_unavailable=False))
    except OSError:
        return None


def _is_describable_profile_dir(path: Path) -> bool:
    """Whether *path* is a profile directory we are allowed to look at.

    A bot is denied its siblings, and that denial arrives as ``EPERM``
    from ``stat`` rather than as an answer, so ``is_dir()`` raises where
    it would normally say False. A sibling we may not examine is left out
    of the listing: nothing true can be said about a directory we cannot
    read, and a policy refusing us is not a failure of the listing.
    """
    try:
        return path.is_dir() and not path.is_symlink()
    except OSError:
        return False


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
        has_config=_path_exists(_DEFAULT_HOME / "config.json"),
        skill_count=_installed_skill_count(_DEFAULT_HOME),
        **default_meta,
        **default_runtime,
    ))

    # Named profiles
    try:
        entries = sorted(_PROFILES_ROOT.iterdir())
    except OSError:
        entries = []
    for d in entries:
        if _is_describable_profile_dir(d) and _PROFILE_NAME_RE.match(d.name):
                meta = _metadata_for(d.name, d, is_default=False)
                runtime = _runtime_summary(d)
                profiles.append(ProfileInfo(
                    name=d.name,
                    path=d,
                    is_default=False,
                    has_config=_path_exists(d / "config.json"),
                    skill_count=_installed_skill_count(d),
                    **meta,
                    **runtime,
                ))

    return profiles


# ── CRUD ──────────────────────────────────────────────────────────

def _allocate_mark_seed() -> int:
    """Next free creation-order seed.

    Must be called while holding ``_profile_mutation_lock``: two processes
    creating a bot at once would otherwise read the same maximum and hand out
    the same seed, collapsing two bots onto one colour and one silhouette.

    Deleting a bot deliberately leaves a gap rather than renumbering. A seed is
    a profile's identity for life; compacting the range would repaint every bot
    that happened to be created after the deleted one.
    """
    highest = -1
    for profile in list_profiles():
        if profile.mark_seed is not None:
            highest = max(highest, profile.mark_seed)
    seed = highest + 1
    if seed > _MARK_SEED_MAX:
        # Only reachable if a client pinned an absurd seed by hand. Clamping
        # would hand the same seed out twice, so fail instead.
        raise ValueError("Profile mark seeds are exhausted.")
    return seed


def backfill_mark_seeds() -> dict[str, int]:
    """Give every seedless profile a seed, oldest first.

    Profiles created before marks were generated have no seed and fall back to
    a name hash, which spreads no better than chance. Assigning in ``createdAt``
    order reproduces the sequence they would have had, so the roster lands on
    the same evenly separated palette a fresh install gets.

    Returns the seeds assigned, keyed by profile name. Already-seeded profiles
    are left exactly as they are.
    """
    assigned: dict[str, int] = {}
    with _profile_mutation_lock():
        pending = [
            profile for profile in list_profiles()
            if profile.mark_seed is None
        ]
        # Undated profiles sort last but keep a stable order among themselves.
        pending.sort(key=lambda profile: (not profile.created_at, profile.created_at, profile.name))
        for profile in pending:
            seed = _allocate_mark_seed()
            metadata_path = profile.path / _PROFILE_METADATA_FILE
            current = _profile_metadata(profile.path)
            current["markSeed"] = seed
            _atomic_write_json(metadata_path, current)
            assigned[profile.name] = seed
    return assigned


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
    mark_seed: int | None = None,
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
    requested_seed = _validate_mark_seed(mark_seed)

    profile_dir = _PROFILES_ROOT / name
    with _profile_mutation_lock():
        if profile_dir.exists() or profile_dir.is_symlink():
            raise FileExistsError(f"Profile '{name}' already exists at {profile_dir}")
        _assert_named_profile_capacity()

    # Resolve clone source
    source_dir = None
    if clone_from is not None or clone_all:
        if clone_from is None or clone_from == "default":
            source_dir = _DEFAULT_HOME
        else:
            validate_profile_name(clone_from)
            source_dir = _PROFILES_ROOT / clone_from
        if not source_dir.is_dir() or source_dir.is_symlink():
            raise FileNotFoundError(f"Source profile does not exist at {source_dir}")

    _PROFILES_ROOT.mkdir(parents=True, exist_ok=True)
    try:
        _PROFILES_ROOT.chmod(0o700)
    except OSError:
        pass
    # Stage outside ``~/.flowly``. A full clone of the default profile must not
    # recursively copy its own ``profiles/`` directory (including this staging
    # directory) into itself. The default home's parent is on the same volume,
    # so the final ``os.replace`` remains atomic.
    staging_parent = _DEFAULT_HOME.parent
    staging_parent.mkdir(parents=True, exist_ok=True)
    temp_dir = Path(tempfile.mkdtemp(prefix=f".flowly-profile-{name}.", dir=staging_parent))

    try:
        if clone_all and source_dir:
            _assert_tree_no_symlinks(
                source_dir,
                ignored_top_level={"profiles", "active_profile"}
                if source_dir == _DEFAULT_HOME else set(),
            )
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
                        if src.is_symlink():
                            raise ValueError(f"Cannot clone symbolic link: {src}")
                        dst = temp_dir / f
                        dst.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(src, dst)

                # Clone persona files
                src_personas = source_dir / "workspace" / "personas"
                if src_personas.is_dir():
                    _assert_tree_no_symlinks(src_personas)
                    dst_personas = temp_dir / "workspace" / "personas"
                    shutil.copytree(src_personas, dst_personas, dirs_exist_ok=True)

        # A clone never inherits who the source was, whoever manages it.
        if source_dir:
            _strip_inherited_identity(temp_dir)
        if local_runtime:
            _bind_local_runtime(temp_dir, profile_dir / "workspace")

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
            "botId": str(uuid.uuid4()),
            "displayName": display_name.strip() or name,
            "description": description.strip(),
            "markText": _validate_mark_text(mark_text),
            "markTone": _validate_mark_tone(mark_tone),
            # markSeed is written under the mutation lock below, so allocation
            # and publication are one atomic step.
            "createdAt": now,
            "updatedAt": now,
            "localRuntime": bool(local_runtime),
            "credentialPolicy": _NAMED_PROFILE_CREDENTIAL_POLICY,
        })

        # Publishing the completed directory is the commit point. A crash
        # before this line leaves only a hidden temp dir, never a half-profile.
        # Count and publication share a cross-process lock. Desktop and CLI
        # can both create profiles, so checking only before staging would let
        # concurrent processes race beyond the installation limit.
        with _profile_mutation_lock():
            if profile_dir.exists() or profile_dir.is_symlink():
                raise FileExistsError(f"Profile '{name}' already exists at {profile_dir}")
            _assert_named_profile_capacity()
            staged_metadata_path = temp_dir / _PROFILE_METADATA_FILE
            staged_metadata = json.loads(
                staged_metadata_path.read_text(encoding="utf-8")
            )
            staged_metadata["markSeed"] = (
                requested_seed if requested_seed is not None else _allocate_mark_seed()
            )
            _atomic_write_json(staged_metadata_path, staged_metadata)
            os.replace(temp_dir, profile_dir)
        try:
            profile_dir.chmod(0o700)
        except OSError:
            pass
    except BaseException:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise

    return profile_dir


def _assert_tree_no_symlinks(
    root: Path, *, ignored_top_level: set[str] | None = None
) -> None:
    """Reject clone sources that could escape their profile via a link."""
    if root.is_symlink():
        raise ValueError(f"Cannot clone symbolic link: {root}")
    for current, directories, files in os.walk(root, followlinks=False):
        current_path = Path(current)
        if current_path == root and ignored_top_level:
            directories[:] = [name for name in directories if name not in ignored_top_level]
            files = [name for name in files if name not in ignored_top_level]
        for name in [*directories, *files]:
            candidate = current_path / name
            if candidate.is_symlink():
                raise ValueError(f"Cannot clone symbolic link: {candidate}")


def _strip_inherited_identity(profile_dir: Path) -> None:
    """Remove the source profile's identity from a clone.

    A clone inherits a setup, never a self. Transport credentials are the
    clearest case: a bot that starts life holding a copy of the account's
    Telegram token is one command away from answering as the account —
    ``flowly --profile <name> gateway`` boots that config with channels
    enabled, and two processes then reply to the same message.

    Channel sections are DROPPED rather than disabled. They were only
    disabled before, which left every token on disk in as many copies as
    there were bots: rotating the original reached none of them, and the
    runtime's own safety boundary had to say out loud that it was covering
    for a config it could not clean. Every channel defaults to off, so
    removing the section is both stricter and simpler than rewriting it.

    Runs for every clone, not only Desktop-managed ones. There is no kind of
    profile that legitimately needs the identity of the one it was copied
    from.
    """
    config_path = profile_dir / "config.json"
    if config_path.exists():
        try:
            raw = _load_config_object(config_path)
        except ValueError as exc:
            raise ValueError(str(exc).replace("Invalid profile", "Cannot clone invalid")) from exc

        removed = raw.pop("channels", None) is not None

        gateway = raw.get("gateway")
        if isinstance(gateway, dict) and gateway.get("token"):
            # Two gateways answering to one token is not a shared secret, it
            # is two processes each believing they are the installation.
            gateway["token"] = ""
            removed = True

        providers = raw.get("providers")
        if isinstance(providers, dict):
            hosted = providers.get("flowlyHosted")
            if not isinstance(hosted, dict):
                hosted = providers.get("flowly_hosted")
            if isinstance(hosted, dict):
                # A relay registration names one install. The account-scoped
                # provider key keeps working without it.
                for key in ("serverId", "server_id", "authToken", "auth_token"):
                    removed = hosted.pop(key, None) is not None or removed

        # Nothing to strip means nothing to write. An import that rewrote a
        # config it had not changed would reformat a file the owner brought
        # with them, for no reason anybody could point at.
        if removed:
            _atomic_write_json(config_path, raw)

    env_path = profile_dir / ".env"
    if env_path.exists():
        try:
            lines = env_path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError) as exc:
            raise ValueError(f"Cannot clone invalid .env: {exc}") from exc
        retained = [
            line for line in lines
            if (line.split("=", 1)[0].strip() if "=" in line else "") in _CLONED_ENV_ALLOW
        ]
        if len(retained) != len(lines):
            _atomic_write_text(env_path, "\n".join(retained) + ("\n" if retained else ""))


def _bind_local_runtime(profile_dir: Path, workspace: Path) -> None:
    """Point a Desktop-managed profile at its own workspace, on loopback.

    Identity stripping is :func:`_strip_inherited_identity`'s job and happens
    for every clone. What is left here is what makes a profile a MANAGED
    runtime rather than an installation: it answers on loopback, and it works
    in its own directory. A profile created from nothing still needs both,
    which is why this also runs when there was no config to clone.
    """
    config_path = profile_dir / "config.json"
    raw: dict = {}
    if config_path.exists():
        try:
            raw = _load_config_object(config_path)
        except ValueError as exc:
            raise ValueError(str(exc).replace("Invalid profile", "Cannot clone invalid")) from exc

    gateway = raw.get("gateway")
    if not isinstance(gateway, dict):
        gateway = {}
        raw["gateway"] = gateway
    gateway.update({"host": "127.0.0.1", "token": ""})

    _set_profile_workspace(raw, workspace)
    _atomic_write_json(config_path, raw)


def profile_display_names(names: list[str] | None = None) -> dict[str, str]:
    """Map profile ids to the names their owner actually reads.

    A bot is created with an id it keeps forever and a display name its owner
    changes at will, and every surface shows the second one. So the id is the
    one name nobody recognises — which is fine until something has to be told
    which bot to talk to, and the only name it was ever given is the one it
    cannot resolve.

    Ids without a display name, or whose display name is just the id again,
    are left out: there is nothing to add for those.
    """
    wanted = set(names) if names is not None else None
    mapping: dict[str, str] = {}
    for profile in list_profiles():
        if wanted is not None and profile.name not in wanted:
            continue
        label = (profile.display_name or "").strip()
        if label and label != profile.name:
            mapping[profile.name] = label
    return mapping


def resolve_profile_reference(reference: str) -> str | None:
    """Resolve an id or a display name to a profile id, or None.

    Display names are not unique — nothing stops two bots being called Friday —
    so a name matching more than one profile resolves to nothing rather than to
    a coin flip. Messaging the wrong bot is worse than saying the name was
    ambiguous.
    """
    candidate = str(reference or "").strip()
    if not candidate:
        return None
    profiles = list_profiles()
    for profile in profiles:
        if profile.name == candidate:
            return profile.name
    folded = candidate.casefold()
    matches = {
        profile.name
        for profile in profiles
        if (profile.display_name or "").strip().casefold() == folded
    }
    return next(iter(matches)) if len(matches) == 1 else None


def describe_profile(name: str) -> ProfileInfo:
    """Return one profile descriptor or raise ``FileNotFoundError``."""
    if name == "default":
        return list_profiles()[0]
    validate_profile_name(name)
    for profile in list_profiles()[1:]:
        if profile.name == name:
            return profile
    raise FileNotFoundError(f"Profile '{name}' does not exist.")


def ensure_profile_bot_id(name: str) -> ProfileInfo:
    """Backfill and return the stable public UUID for one profile.

    Older profiles predate remote bot hosts and therefore have no durable
    identity beyond their renameable directory name. The host calls this at
    its public boundary so existing profiles migrate lazily without moving any
    data or rewriting their sessions.
    """
    profile = describe_profile(name)
    current = _profile_metadata(profile.path)
    raw = str(current.get("botId") or "").strip()
    try:
        parsed = uuid.UUID(raw)
    except (ValueError, AttributeError):
        parsed = uuid.uuid4()
    bot_id = str(parsed)
    if raw != bot_id:
        now = _utc_now()
        current.update({
            "version": 1,
            "botId": bot_id,
            "displayName": str(current.get("displayName") or profile.display_name or name),
            "description": str(current.get("description") or profile.description),
            "markText": str(current.get("markText") or profile.mark_text),
            "markTone": str(current.get("markTone") or profile.mark_tone),
            "createdAt": str(current.get("createdAt") or profile.created_at or now),
            "updatedAt": str(current.get("updatedAt") or profile.updated_at or now),
            "credentialPolicy": (
                "primary" if profile.is_default else _NAMED_PROFILE_CREDENTIAL_POLICY
            ),
        })
        _atomic_write_json(profile.path / _PROFILE_METADATA_FILE, current)
        profile = describe_profile(name)
    return profile


def get_or_create_profile_host_id() -> str:
    """Return the installation's stable, opaque remote bot-host UUID."""
    path = _DEFAULT_HOME / _PROFILE_HOST_FILE
    try:
        current = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, UnicodeDecodeError, json.JSONDecodeError):
        current = {}
    raw = str(current.get("hostId") or "") if isinstance(current, dict) else ""
    try:
        parsed = uuid.UUID(raw)
    except (ValueError, AttributeError):
        parsed = uuid.uuid4()
    host_id = str(parsed)
    if raw != host_id:
        _atomic_write_json(path, {"version": 1, "hostId": host_id})
    return host_id


def update_profile_metadata(
    name: str,
    *,
    display_name: str | None = None,
    description: str | None = None,
    mark_text: str | None = None,
    mark_tone: str | None = None,
    mark_seed: int | None = None,
) -> ProfileInfo:
    """Atomically update renderer-facing metadata for one profile."""
    profile = describe_profile(name)
    current = _profile_metadata(profile.path)
    now = _utc_now()
    current.update({
        "version": 1,
        "botId": str(current.get("botId") or profile.bot_id or uuid.uuid4()),
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
        # A seed is identity: once assigned it is never cleared by an edit that
        # simply does not mention it.
        "markSeed": (
            _validate_mark_seed(mark_seed) if mark_seed is not None
            else _read_mark_seed(current.get("markSeed"))
        ),
        "createdAt": str(current.get("createdAt") or profile.created_at or now),
        "updatedAt": now,
        "credentialPolicy": (
            "primary" if profile.is_default else _NAMED_PROFILE_CREDENTIAL_POLICY
        ),
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
        "credentialPolicy": (
            "primary" if profile.is_default else _NAMED_PROFILE_CREDENTIAL_POLICY
        ),
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
    lease = reconcile_runtime_lease(profile.path, profile_name=name)
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


def _process_identity(pid: int) -> str | None:
    """Best-effort process birth identity used to reject PID reuse."""
    if pid <= 0:
        return None
    proc_stat = Path(f"/proc/{pid}/stat")
    try:
        # Field 22 is process starttime. The command field may contain spaces,
        # so split only after its final closing parenthesis.
        tail = proc_stat.read_text(encoding="utf-8").rsplit(") ", 1)[1].split()
        return f"linux:{tail[19]}"
    except (FileNotFoundError, OSError, IndexError, UnicodeDecodeError):
        pass
    try:
        value = subprocess.check_output(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=2,
        ).strip()
        return f"ps:{value}" if value else None
    except (OSError, subprocess.SubprocessError):
        return None


def _process_command(pid: int) -> list[str] | None:
    """Return one process argv without invoking a shell."""
    if pid <= 0:
        return None
    proc_cmdline = Path(f"/proc/{pid}/cmdline")
    try:
        raw = proc_cmdline.read_bytes()
        argv = [
            part.decode("utf-8", errors="replace")
            for part in raw.split(b"\0")
            if part
        ]
        return argv or None
    except (FileNotFoundError, OSError):
        pass
    try:
        value = subprocess.check_output(
            ["ps", "-ww", "-o", "command=", "-p", str(pid)],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=2,
        ).strip()
        return shlex.split(value) if value else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def _process_parent_pid(pid: int) -> int | None:
    """Return a process parent PID, or ``None`` when it cannot be verified."""
    if pid <= 0:
        return None
    proc_stat = Path(f"/proc/{pid}/stat")
    try:
        tail = proc_stat.read_text(encoding="utf-8").rsplit(") ", 1)[1].split()
        return int(tail[1])
    except (FileNotFoundError, OSError, ValueError, IndexError, UnicodeDecodeError):
        pass
    try:
        value = subprocess.check_output(
            ["ps", "-o", "ppid=", "-p", str(pid)],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=2,
        ).strip()
        return int(value.split()[0]) if value else None
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        return None


def _option_values(argv: list[str], option: str) -> list[str]:
    values: list[str] = []
    prefix = f"{option}="
    for index, token in enumerate(argv):
        if token == option and index + 1 < len(argv):
            values.append(argv[index + 1])
        elif token.startswith(prefix):
            values.append(token[len(prefix):])
    return values


def _is_managed_profile_runtime_command(pid: int, profile_name: str) -> bool:
    """Match only the ephemeral named-profile runtime spawn shape."""
    argv = _process_command(pid)
    if not argv or "serve" not in argv:
        return False
    serve_index = argv.index("serve")
    basenames = [Path(token).name.lower() for token in argv[:serve_index]]
    executable_shape = bool(basenames) and basenames[0] in {"flowly", "flowly.exe"}
    if len(basenames) >= 2 and basenames[0].startswith(("python", "pythonw")):
        executable_shape = basenames[1] in {"flowly", "flowly.exe"}
    if basenames and basenames[0] in {"uv", "uv.exe"} and "run" in argv[:serve_index]:
        executable_shape = "flowly" in basenames or "flowly.exe" in basenames
    if not executable_shape:
        return False
    profiles = _option_values(argv, "--profile")
    ports = _option_values(argv, "--port")
    return (
        bool(profiles)
        and set(profiles) == {profile_name}
        and bool(ports)
        and set(ports) == {"0"}
    )


def _desktop_manager_context() -> tuple[int, str, str | None] | None:
    raw_pid = os.environ.get("FLOWLY_DESKTOP_MANAGER_PID", "").strip()
    instance = os.environ.get("FLOWLY_DESKTOP_MANAGER_INSTANCE", "").strip()
    expected_identity = os.environ.get("FLOWLY_DESKTOP_MANAGER_IDENTITY", "").strip()
    try:
        pid = int(raw_pid)
    except ValueError:
        return None
    if pid <= 0 or not instance or len(instance) > 128 or not _pid_is_alive(pid):
        return None
    actual_identity = _process_identity(pid)
    if expected_identity and actual_identity and expected_identity != actual_identity:
        return None
    return pid, instance, expected_identity or actual_identity


def _lease_owned_by_manager(
    lease: dict,
    manager: tuple[int, str, str | None],
) -> bool:
    pid, instance, identity = manager
    try:
        lease_pid = int(lease.get("managerPid") or 0)
    except (TypeError, ValueError):
        return False
    if lease_pid != pid or str(lease.get("managerInstance") or "") != instance:
        return False
    expected_identity = str(lease.get("managerIdentity") or "")
    return not expected_identity or (identity is not None and identity == expected_identity)


def _legacy_runtime_is_recoverable(
    lease: dict,
    profile_name: str,
    manager: tuple[int, str, str | None],
) -> bool:
    """Recognize a legacy child owned by this Desktop or reparented to init."""
    try:
        current = int(lease.get("pid") or 0)
    except (TypeError, ValueError):
        return False
    manager_pid = manager[0]
    for _ in range(12):
        if not _is_managed_profile_runtime_command(current, profile_name):
            return False
        parent = _process_parent_pid(current)
        if parent in (0, 1):
            return True
        if parent is None or parent == current:
            return False
        if parent == manager_pid:
            return True
        current = parent
    return False


def _process_matches_identity(pid: int, identity: str) -> bool:
    return bool(identity) and _pid_is_alive(pid) and _process_identity(pid) == identity


def _unlink_matching_runtime_lease(path: Path, lease: dict) -> None:
    try:
        current = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, UnicodeDecodeError, json.JSONDecodeError):
        return
    if not isinstance(current, dict):
        return
    if (
        current.get("pid") == lease.get("pid")
        and current.get("instanceId") == lease.get("instanceId")
    ):
        path.unlink(missing_ok=True)


def _terminate_runtime_lease(path: Path, lease: dict) -> bool:
    """Stop the exact leased process with bounded TERM/KILL escalation."""
    try:
        pid = int(lease.get("pid") or 0)
    except (TypeError, ValueError):
        return False
    identity = str(lease.get("processIdentity") or "") or (_process_identity(pid) or "")
    if not _process_matches_identity(pid, identity):
        _unlink_matching_runtime_lease(path, lease)
        return True
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        _unlink_matching_runtime_lease(path, lease)
        return True
    except OSError:
        return False
    for _ in range(40):
        if not _process_matches_identity(pid, identity):
            _unlink_matching_runtime_lease(path, lease)
            return True
        time.sleep(0.05)
    if not _process_matches_identity(pid, identity):
        _unlink_matching_runtime_lease(path, lease)
        return True
    try:
        os.kill(pid, getattr(signal, "SIGKILL", signal.SIGTERM))
    except ProcessLookupError:
        _unlink_matching_runtime_lease(path, lease)
        return True
    except OSError:
        return False
    for _ in range(20):
        if not _process_matches_identity(pid, identity):
            _unlink_matching_runtime_lease(path, lease)
            return True
        time.sleep(0.05)
    return False


def _lease_process_matches(lease: dict) -> bool:
    try:
        pid = int(lease.get("pid") or 0)
    except (TypeError, ValueError):
        return False
    if not _pid_is_alive(pid):
        return False
    expected = str(lease.get("processIdentity") or "")
    if not expected:
        return True  # v1 compatibility
    return _process_identity(pid) == expected


def _lease_manager_dead(lease: dict) -> bool:
    try:
        pid = int(lease.get("managerPid") or 0)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if not _pid_is_alive(pid):
        return True
    expected = str(lease.get("managerIdentity") or "")
    return bool(expected) and _process_identity(pid) != expected


def read_runtime_lease(profile_dir: Path) -> dict | None:
    """Return a live Desktop-runtime lease, pruning a stale lease."""
    path = profile_dir / _RUNTIME_LEASE_FILE
    try:
        if path.is_symlink():
            return None
        lease = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return None
    if isinstance(lease, dict) and _lease_process_matches(lease):
        return lease
    path.unlink(missing_ok=True)
    return None


def reconcile_runtime_lease(
    profile_dir: Path,
    *,
    profile_name: str | None = None,
) -> dict | None:
    """Retire a runtime provably owned by this Desktop or left by a dead one."""
    path = profile_dir / _RUNTIME_LEASE_FILE
    lease = read_runtime_lease(profile_dir)
    if not lease:
        if path.exists() or path.is_symlink():
            lease = read_runtime_lease(profile_dir)
        if not lease and (path.exists() or path.is_symlink()):
            raise RuntimeError(
                "Profile runtime ownership data is unreadable. "
                "Restart Flowly Desktop before trying again."
            )
    if not lease:
        return None
    manager = _desktop_manager_context()
    if manager is None:
        return lease

    has_manager = bool(lease.get("managerPid") or lease.get("managerInstance"))
    recoverable = (
        _lease_owned_by_manager(lease, manager)
        or (has_manager and _lease_manager_dead(lease))
    )
    if not has_manager:
        recoverable = _legacy_runtime_is_recoverable(
            lease,
            profile_name or profile_dir.name,
            manager,
        )
    if recoverable and _terminate_runtime_lease(path, lease):
        return read_runtime_lease(profile_dir)
    return lease


def claim_runtime_lease(instance_id: str) -> Path:
    """Exclusively claim the current profile for one managed runtime."""
    if not instance_id:
        raise ValueError("runtime instance id is required")
    path = get_flowly_home() / _RUNTIME_LEASE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = reconcile_runtime_lease(
        path.parent,
        profile_name=current_profile_name(),
    )
    if existing:
        raise RuntimeError(
            f"Profile runtime is already active (pid {existing.get('pid')})."
        )
    path.unlink(missing_ok=True)
    payload = {
        "version": 2,
        "instanceId": instance_id,
        "pid": os.getpid(),
        "processIdentity": _process_identity(os.getpid()),
        "port": 0,
        "startedAt": _utc_now(),
    }
    manager = _desktop_manager_context()
    if manager is not None:
        manager_pid, manager_instance, manager_identity = manager
        payload.update({
            "managerPid": manager_pid,
            "managerIdentity": manager_identity,
            "managerInstance": manager_instance,
        })
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
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


def update_runtime_lease(
    instance_id: str,
    *,
    port: int,
    auth_token: str,
    capabilities: tuple[str, ...] | list[str] = (),
) -> None:
    """Publish the owner-only endpoint used by sibling Flowly managers.

    Desktop and the primary gateway can coexist in separate processes.  The
    runtime remains owned by the process that claimed the lease, but another
    Flowly manager running as the same OS user may attach as an authenticated
    client instead of attempting to spawn a duplicate gateway.  The lease is
    atomically written with mode ``0600`` and is excluded from profile exports.
    """
    token = auth_token.strip() if isinstance(auth_token, str) else ""
    if (
        not isinstance(port, int)
        or isinstance(port, bool)
        or not 1 <= port <= 65_535
        or not 32 <= len(token) <= 512
        or any(ord(char) < 0x21 or ord(char) == 0x7F for char in token)
    ):
        raise ValueError("Profile runtime endpoint is invalid.")
    capability_list = list(dict.fromkeys(capabilities))
    if (
        len(capability_list) > 64
        or any(
            not isinstance(capability, str)
            or not capability
            or len(capability) > 128
            or "\x00" in capability
            for capability in capability_list
        )
    ):
        raise ValueError("Profile runtime capabilities are invalid.")
    path = get_flowly_home() / _RUNTIME_LEASE_FILE
    lease = read_runtime_lease(path.parent)
    if not lease or lease.get("instanceId") != instance_id or lease.get("pid") != os.getpid():
        raise RuntimeError("Profile runtime lease ownership was lost.")
    lease["port"] = port
    lease["authToken"] = token
    lease["capabilities"] = capability_list
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
    if profile_dir.is_symlink() or _PROFILES_ROOT.resolve() not in profile_dir.resolve().parents:
        raise ValueError("Profile directory failed containment validation.")

    lease = reconcile_runtime_lease(profile_dir, profile_name=name)
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
    """Export a complete *sensitive* profile to an unencrypted tar archive.

    Runtime leases are machine/process credentials, not profile data. The
    default home also owns the named-profile directory and active CLI pointer;
    neither may be nested into a default-profile export. Symlinks are rejected
    rather than dereferenced so an archive can never pull in data from outside
    the isolated profile tree.
    """
    if name != "default":
        validate_profile_name(name)
    profile_dir = _PROFILES_ROOT / name if name != "default" else _DEFAULT_HOME
    if not profile_dir.is_dir() or profile_dir.is_symlink():
        raise FileNotFoundError(f"Profile '{name}' does not exist.")

    lease = read_runtime_lease(profile_dir)
    if lease:
        raise RuntimeError(
            f"Profile '{name}' is running (pid {lease.get('pid')}). Stop it before export."
        )

    raw_output = str(output_path)
    target = Path(
        raw_output if raw_output.endswith((".tar.gz", ".tgz")) else f"{raw_output}.tar.gz"
    ).expanduser()
    if not target.parent.is_dir():
        raise FileNotFoundError(f"Export directory does not exist: {target.parent}")
    resolved_profile = profile_dir.resolve()
    resolved_target = target.resolve(strict=False)
    if resolved_target == resolved_profile or resolved_profile in resolved_target.parents:
        raise ValueError("Profile exports must be written outside the profile directory.")

    excluded_roots = {_RUNTIME_LEASE_FILE}
    if name == "default":
        excluded_roots.update({"profiles", "active_profile"})

    return _write_profile_archive(name, profile_dir, target, excluded_roots)


def _write_profile_archive(
    name: str,
    profile_dir: Path,
    target: Path,
    excluded_roots: set[str] | None = None,
) -> Path:
    import tarfile

    excluded = excluded_roots or set()
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=target.parent,
    )
    os.close(fd)
    temporary = Path(temporary_name)
    try:
        with tempfile.TemporaryDirectory(prefix=".flowly-archive-sqlite-") as raw_stage:
            sqlite_source = profile_dir / "profile-rooms.sqlite3"
            sqlite_snapshot: Path | None = None
            if sqlite_source.exists():
                if sqlite_source.is_symlink() or not sqlite_source.is_file():
                    raise ValueError("Profile room database is not a regular file.")
                from flowly.profile_room_store import SQLiteRoomStore

                sqlite_snapshot = Path(raw_stage) / "profile-rooms.sqlite3"
                SQLiteRoomStore(sqlite_source).backup_to(sqlite_snapshot)
            with tarfile.open(temporary, "w:gz") as bundle:
                root_info = tarfile.TarInfo(name)
                root_info.type = tarfile.DIRTYPE
                root_info.mode = 0o700
                root_info.mtime = int(time.time())
                bundle.addfile(root_info)
                for candidate in sorted(profile_dir.rglob("*")):
                    relative = candidate.relative_to(profile_dir)
                    if relative.parts[0] in excluded:
                        continue
                    if relative.as_posix() in {
                        "profile-rooms.sqlite3-wal",
                        "profile-rooms.sqlite3-shm",
                        ".profile-rooms.sqlite3.migration.lock",
                    }:
                        continue
                    metadata = candidate.lstat()
                    if stat.S_ISLNK(metadata.st_mode):
                        raise ValueError(
                            f"Profile export contains a symbolic link: {relative.as_posix()}"
                        )
                    if not (stat.S_ISDIR(metadata.st_mode) or stat.S_ISREG(metadata.st_mode)):
                        raise ValueError(
                            f"Profile export contains an unsupported entry: {relative.as_posix()}"
                        )
                    archive_source = (
                        sqlite_snapshot
                        if relative.as_posix() == "profile-rooms.sqlite3"
                        and sqlite_snapshot is not None
                        else candidate
                    )
                    bundle.add(
                        archive_source,
                        arcname=(Path(name) / relative).as_posix(),
                        recursive=False,
                        filter=lambda info: _portable_profile_tar_info(info),
                    )
        os.replace(temporary, target)
        try:
            target.chmod(0o600)
        except OSError:
            pass
        return target
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


_TEMPLATE_EXCLUDED_ROOTS = frozenset({
    _RUNTIME_LEASE_FILE,
    ".env",
    "credentials",
    "mcp-tokens",
    "sessions",
    "logs",
    "audit",
    "trajectories",
    "subagents",
    "screenshots",
    "media",
    "cron",
    "session_index.sqlite",
    "session_index.sqlite-wal",
    "session_index.sqlite-shm",
    "profile-rooms.json",
    "profile-rooms.sqlite3",
    "profile-rooms.sqlite3-wal",
    "profile-rooms.sqlite3-shm",
    ".profile-rooms.sqlite3.migration.lock",
    "profiles",
    "active_profile",
})
_SECRET_CONFIG_KEY_RE = re.compile(
    r"(?:api[_-]?key|account[_-]?key|access[_-]?token|refresh[_-]?token|"
    r"auth[_-]?token|token|"
    r"client[_-]?secret|password|secret|private[_-]?key)$",
    re.IGNORECASE,
)
_TEXT_SECRET_PATTERNS = (
    re.compile(
        r"(?im)^([A-Z][A-Z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD)[A-Z0-9_]*)"
        r"\s*=\s*[^\r\n]+$"
    ),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\b(?:sk|xai|ghp|github_pat)_[A-Za-z0-9_-]{20,}\b"),
)


def export_profile_template(name: str, output_path: str) -> Path:
    """Export a credential-free profile template safe for deliberate sharing."""
    if name != "default":
        validate_profile_name(name)
    profile_dir = _PROFILES_ROOT / name if name != "default" else _DEFAULT_HOME
    if not profile_dir.is_dir() or profile_dir.is_symlink():
        raise FileNotFoundError(f"Profile '{name}' does not exist.")
    lease = read_runtime_lease(profile_dir)
    if lease:
        raise RuntimeError(
            f"Profile '{name}' is running (pid {lease.get('pid')}). Stop it before export."
        )
    raw_output = str(output_path)
    target = Path(
        raw_output
        if raw_output.endswith((".tar.gz", ".tgz"))
        else f"{raw_output}.flowly-profile.tar.gz"
    ).expanduser()
    if not target.parent.is_dir():
        raise FileNotFoundError(f"Export directory does not exist: {target.parent}")
    resolved_profile = profile_dir.resolve()
    resolved_target = target.resolve(strict=False)
    if resolved_target == resolved_profile or resolved_profile in resolved_target.parents:
        raise ValueError("Profile exports must be written outside the profile directory.")
    _assert_tree_no_symlinks(
        profile_dir,
        ignored_top_level=set(_TEMPLATE_EXCLUDED_ROOTS),
    )
    with tempfile.TemporaryDirectory(prefix=".flowly-template-") as raw_stage:
        staged = Path(raw_stage) / name

        def ignore(source: str, children: list[str]) -> set[str]:
            relative = Path(source).relative_to(profile_dir)
            ignored: set[str] = set()
            if relative == Path("."):
                ignored.update(child for child in children if child in _TEMPLATE_EXCLUDED_ROOTS)
                ignored.update(
                    child for child in children if child.endswith("-credentials.json")
                )
            if relative == Path("workspace"):
                ignored.update(child for child in children if child in {"memory", "USER.md"})
            return ignored

        shutil.copytree(profile_dir, staged, ignore=ignore)
        _sanitize_template_tree(staged)
        return _write_profile_archive(name, staged, target)


def _sanitize_template_tree(root: Path) -> None:
    config_path = root / "config.json"
    if config_path.exists():
        config = _load_config_object(config_path)

        def redact(value: Any) -> Any:
            if isinstance(value, dict):
                return {
                    key: "" if _SECRET_CONFIG_KEY_RE.search(str(key)) else redact(item)
                    for key, item in value.items()
                }
            if isinstance(value, list):
                return [redact(item) for item in value]
            return value

        config = redact(config)
        channels = config.get("channels")
        if isinstance(channels, dict):
            for channel in channels.values():
                if isinstance(channel, dict):
                    channel["enabled"] = False
        gateway = config.get("gateway")
        if isinstance(gateway, dict):
            gateway.update({"host": "127.0.0.1", "token": ""})
        _atomic_write_json(config_path, config)
    metadata_path = root / _PROFILE_METADATA_FILE
    if metadata_path.exists():
        metadata = _profile_metadata(root)
        metadata.pop("botId", None)
        metadata["localRuntime"] = True
        _atomic_write_json(metadata_path, metadata)
    for candidate in root.rglob("*"):
        if not candidate.is_file() or candidate in {config_path, metadata_path}:
            continue
        try:
            if candidate.stat().st_size > 5 * 1024 * 1024:
                continue
            raw = candidate.read_bytes()
            if b"\x00" in raw:
                continue
            text = raw.decode("utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        redacted = text
        for pattern in _TEXT_SECRET_PATTERNS:
            redacted = pattern.sub(
                (lambda match: f"{match.group(1)}=" if match.lastindex else "[REDACTED]"),
                redacted,
            )
        if redacted != text:
            _atomic_write_text(candidate, redacted)


def export_profile_backup(name: str, output_path: str, password: str) -> Path:
    """Export a complete profile as an authenticated encrypted backup.

    The plaintext tar exists only in a private temporary directory and is
    removed before this function returns. AES-256-GCM authenticates both the
    payload and versioned header; scrypt derives the key from the user-supplied
    passphrase without storing it or a verifier.
    """
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

    if not isinstance(password, str) or len(password) < 12 or len(password) > 1024:
        raise ValueError("Backup password must contain 12–1,024 characters.")
    raw_output = str(output_path)
    target = Path(
        raw_output if raw_output.endswith(".flowly-backup")
        else f"{raw_output}.flowly-backup"
    ).expanduser()
    if not target.parent.is_dir():
        raise FileNotFoundError(f"Export directory does not exist: {target.parent}")
    if name != "default":
        validate_profile_name(name)
    profile_dir = _PROFILES_ROOT / name if name != "default" else _DEFAULT_HOME
    if not profile_dir.is_dir() or profile_dir.is_symlink():
        raise FileNotFoundError(f"Profile '{name}' does not exist.")
    resolved_profile = profile_dir.resolve()
    resolved_target = target.resolve(strict=False)
    if resolved_target == resolved_profile or resolved_profile in resolved_target.parents:
        raise ValueError("Profile exports must be written outside the profile directory.")

    salt = os.urandom(_BACKUP_SALT_BYTES)
    nonce = os.urandom(_BACKUP_NONCE_BYTES)
    header = _BACKUP_MAGIC + salt + nonce
    key = Scrypt(salt=salt, length=32, n=2**15, r=8, p=1).derive(
        password.encode("utf-8")
    )
    fd, raw_target = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=target.parent,
    )
    os.close(fd)
    temporary_target = Path(raw_target)
    try:
        with tempfile.TemporaryDirectory(prefix=".flowly-backup-") as raw_dir:
            plaintext = export_profile(name, str(Path(raw_dir) / "profile.tar.gz"))
            encryptor = Cipher(algorithms.AES(key), modes.GCM(nonce)).encryptor()
            encryptor.authenticate_additional_data(header)
            with plaintext.open("rb") as source, temporary_target.open("wb") as destination:
                destination.write(header)
                while chunk := source.read(_BACKUP_CHUNK_BYTES):
                    destination.write(encryptor.update(chunk))
                destination.write(encryptor.finalize())
                destination.write(encryptor.tag)
                destination.flush()
                os.fsync(destination.fileno())
        temporary_target.chmod(0o600)
        os.replace(temporary_target, target)
        return target
    except BaseException:
        temporary_target.unlink(missing_ok=True)
        raise


def _decrypt_profile_backup(archive: Path, password: str, output: Path) -> None:
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

    if not isinstance(password, str) or len(password) < 12 or len(password) > 1024:
        raise ValueError("Backup password must contain 12–1,024 characters.")
    minimum = len(_BACKUP_MAGIC) + _BACKUP_SALT_BYTES + _BACKUP_NONCE_BYTES + _BACKUP_TAG_BYTES
    size = archive.stat().st_size
    if size < minimum:
        raise ValueError("Encrypted profile backup is truncated.")
    with archive.open("rb") as source:
        header = source.read(len(_BACKUP_MAGIC) + _BACKUP_SALT_BYTES + _BACKUP_NONCE_BYTES)
        if not header.startswith(_BACKUP_MAGIC):
            raise ValueError("Encrypted profile backup header is invalid.")
        salt_start = len(_BACKUP_MAGIC)
        salt = header[salt_start:salt_start + _BACKUP_SALT_BYTES]
        nonce = header[-_BACKUP_NONCE_BYTES:]
        source.seek(-_BACKUP_TAG_BYTES, os.SEEK_END)
        tag = source.read(_BACKUP_TAG_BYTES)
        ciphertext_bytes = size - len(header) - _BACKUP_TAG_BYTES
        source.seek(len(header))
        key = Scrypt(salt=salt, length=32, n=2**15, r=8, p=1).derive(
            password.encode("utf-8")
        )
        decryptor = Cipher(algorithms.AES(key), modes.GCM(nonce, tag)).decryptor()
        decryptor.authenticate_additional_data(header)
        remaining = ciphertext_bytes
        try:
            with output.open("wb") as destination:
                while remaining:
                    chunk = source.read(min(_BACKUP_CHUNK_BYTES, remaining))
                    if not chunk:
                        raise ValueError("Encrypted profile backup is truncated.")
                    remaining -= len(chunk)
                    destination.write(decryptor.update(chunk))
                destination.write(decryptor.finalize())
                destination.flush()
                os.fsync(destination.fileno())
            output.chmod(0o600)
        except InvalidTag as exc:
            output.unlink(missing_ok=True)
            raise ValueError("Backup password is incorrect or the backup was modified.") from exc
        except BaseException:
            output.unlink(missing_ok=True)
            raise


def _portable_profile_tar_info(info: Any) -> Any:
    """Strip host ownership and unsafe mode bits from one exported entry."""
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    # A profile can contain provider credentials and private memory. Preserve
    # only owner permissions (including an executable owner's script bit).
    info.mode = (info.mode & 0o700) | (0o700 if info.isdir() else 0o600)
    return info


def import_profile(
    archive_path: str,
    name: str | None = None,
    *,
    local_runtime: bool = False,
    identity: str = "duplicate",
    password: str | None = None,
) -> Path:
    """Import a shared template, legacy archive, or encrypted backup."""
    archive = Path(archive_path)
    if not archive.exists():
        raise FileNotFoundError(f"Archive not found: {archive}")
    encrypted = is_encrypted_profile_backup(archive)
    if not encrypted:
        return _import_profile_archive(
            str(archive), name=name, local_runtime=local_runtime, identity=identity,
        )
    if password is None:
        raise ValueError("This encrypted profile backup requires a password.")
    with tempfile.TemporaryDirectory(prefix=".flowly-restore-") as raw_stage:
        decrypted = Path(raw_stage) / "profile.tar.gz"
        _decrypt_profile_backup(archive, password, decrypted)
        return _import_profile_archive(
            str(decrypted), name=name, local_runtime=local_runtime, identity=identity,
        )


def is_encrypted_profile_backup(archive_path: str | Path) -> bool:
    """Detect the versioned encrypted-backup envelope without parsing payload data."""
    with Path(archive_path).open("rb") as handle:
        return handle.read(len(_BACKUP_MAGIC)) == _BACKUP_MAGIC


def _import_profile_archive(
    archive_path: str,
    name: str | None = None,
    *,
    local_runtime: bool = False,
    identity: str = "duplicate",
) -> Path:
    """Validate and atomically publish one plaintext profile archive.

    ``local_runtime`` converts the imported profile into a Desktop-managed
    bot before publication: messaging transports and machine relay identity
    are removed while model-provider credentials remain profile-local.

    ``identity="duplicate"`` (the safe default) always assigns a fresh bot
    UUID. ``identity="restore"`` preserves a valid archived UUID, but refuses
    to publish it when that identity already exists on this installation.
    """
    import tarfile

    if identity not in {"duplicate", "restore"}:
        raise ValueError("Profile import identity must be 'duplicate' or 'restore'.")

    archive = Path(archive_path)
    if not archive.exists():
        raise FileNotFoundError(f"Archive not found: {archive}")

    with tarfile.open(archive, "r:gz") as tf:
        members = tf.getmembers()
        if len(members) > _MAX_IMPORT_MEMBERS:
            raise ValueError("Profile archive contains too many entries.")
        total_size = 0
        top_dirs: set[str] = set()
        for member in members:
            normalized = member.name.replace("\\", "/")
            parts = [part for part in normalized.split("/") if part not in ("", ".")]
            if (
                not parts
                or normalized.startswith("/")
                or "\\" in member.name
                or ".." in parts
                or member.issym()
                or member.islnk()
                or member.isdev()
                or not (member.isdir() or member.isfile())
            ):
                raise ValueError(f"Profile archive contains an unsafe entry: {member.name}")
            top_dirs.add(parts[0])
            if member.isfile():
                total_size += max(0, member.size)
                if total_size > _MAX_IMPORT_BYTES:
                    raise ValueError("Profile archive exceeds the safe extracted-size limit.")

    source_root = next(iter(top_dirs)) if len(top_dirs) == 1 else None
    inferred = name or source_root
    if not inferred:
        raise ValueError("Cannot determine profile name from archive. Specify --name.")

    validate_profile_name(inferred)
    profile_dir = _PROFILES_ROOT / inferred
    _PROFILES_ROOT.mkdir(parents=True, exist_ok=True)
    try:
        _PROFILES_ROOT.chmod(0o700)
    except OSError:
        pass
    with _profile_mutation_lock():
        if profile_dir.exists() or profile_dir.is_symlink():
            raise FileExistsError(f"Profile '{inferred}' already exists.")
        _assert_named_profile_capacity()
    staging = Path(tempfile.mkdtemp(prefix=".flowly-import-", dir=_DEFAULT_HOME.parent))
    try:
        with tarfile.open(archive, "r:gz") as tf:
            # Python's data filter rejects absolute paths, traversal, links,
            # devices and unsafe permission bits a second time while writing.
            tf.extractall(staging, filter="data")
        if source_root is None:
            raise ValueError("Profile archive must contain one top-level directory.")
        extracted = staging / source_root
        if not extracted.is_dir() or extracted.is_symlink():
            raise ValueError("Profile archive must contain one profile directory.")
        _assert_tree_no_symlinks(extracted)
        # An archive is a clone that travelled. It may have been written on
        # another machine by another person, so the identity in it is even
        # less this installation's than a local copy's would be.
        _strip_inherited_identity(extracted)
        if local_runtime:
            _bind_local_runtime(extracted, profile_dir / "workspace")
        metadata = _profile_metadata(extracted)
        now = _utc_now()
        archived_bot_id = str(metadata.get("botId") or "").strip()
        try:
            restored_bot_id = str(uuid.UUID(archived_bot_id))
        except (ValueError, AttributeError):
            restored_bot_id = str(uuid.uuid4())
        bot_id = restored_bot_id if identity == "restore" else str(uuid.uuid4())
        if identity == "restore":
            existing_ids = {
                profile.bot_id
                for profile in list_profiles()[1:]
                if profile.bot_id
            }
            if bot_id in existing_ids:
                raise ProfileIdentityConflictError(
                    "This bot identity already exists on this installation. "
                    "Import it as a duplicate or remove the existing bot first."
                )
        metadata.update({
            "version": 1,
            "botId": bot_id,
            "displayName": str(metadata.get("displayName") or inferred).strip(),
            "description": str(metadata.get("description") or "").strip(),
            "markText": _validate_mark_text(str(metadata.get("markText") or "")),
            "markTone": _validate_mark_tone(str(metadata.get("markTone") or "")),
            "createdAt": str(metadata.get("createdAt") or now),
            "updatedAt": now,
            "localRuntime": bool(local_runtime or metadata.get("localRuntime")),
            "credentialPolicy": _NAMED_PROFILE_CREDENTIAL_POLICY,
        })
        _atomic_write_json(extracted / _PROFILE_METADATA_FILE, metadata)
        _harden_profile_tree_permissions(extracted)
        with _profile_mutation_lock():
            if profile_dir.exists() or profile_dir.is_symlink():
                raise FileExistsError(f"Profile '{inferred}' already exists.")
            _assert_named_profile_capacity()
            if identity == "restore":
                existing_ids = {
                    profile.bot_id
                    for profile in list_profiles()[1:]
                    if profile.bot_id
                }
                if bot_id in existing_ids:
                    raise ProfileIdentityConflictError(
                        "This bot identity already exists on this installation. "
                        "Import it as a duplicate or remove the existing bot first."
                    )
            os.replace(extracted, profile_dir)
        try:
            profile_dir.chmod(0o700)
        except OSError:
            pass
        return profile_dir
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _harden_profile_tree_permissions(root: Path) -> None:
    """Make imported private data owner-only while preserving owner execute bits."""
    for candidate in [root, *sorted(root.rglob("*"))]:
        metadata = candidate.lstat()
        if stat.S_ISDIR(metadata.st_mode):
            candidate.chmod(0o700)
        elif stat.S_ISREG(metadata.st_mode):
            candidate.chmod(0o700 if metadata.st_mode & stat.S_IXUSR else 0o600)
        else:
            raise ValueError(
                f"Profile import contains an unsupported entry: "
                f"{candidate.relative_to(root).as_posix()}"
            )


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
