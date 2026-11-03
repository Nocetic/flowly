"""One agent's choices about its skills: which built-ins it removed, which it turned off.

Built-in skills ship in this package and are copied into the agent's own
skills folder (``sync.py``, tracked in ``.bundled_manifest``). Once that copy
exists the agent's folder is the truth: a built-in whose copy the owner
removed stays removed, even though the package still carries it, and
``restore_builtin`` brings it back. Before the first sync (no manifest yet) the
package copy is what the agent loads.

Turning a skill off keeps it on disk but out of everything the agent reads
(the prompt's skill index, ``skill_view``, the count apps show). The list
lives next to the skills, in ``.state.json``, so each agent (profile) has its
own and nothing writes the agent's config while its gateway runs.

Every function takes the agent's skills folder explicitly: the profile host
reads other agents' folders without rebinding ``FLOWLY_HOME``.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import threading
from pathlib import Path

BUNDLED_DIR = Path(__file__).parent
MANIFEST_FILE = ".bundled_manifest"
STATE_FILE = ".state.json"
HUB_META_FILE = ".flowly-skill.json"

BUNDLED = "bundled"
MARKETPLACE = "marketplace"
LOCAL = "local"

_WRITE_LOCK = threading.Lock()


def tracked_builtins(managed: Path) -> dict[str, str] | None:
    """The built-ins synced into this folder (name → origin hash); None before the first sync."""
    path = managed / MANIFEST_FILE
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError:
        return None
    entries: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if line:
            name, _, digest = line.partition(":")
            entries[name.strip()] = digest.strip()
    return entries


def builtin_loads_from_package(managed: Path, name: str) -> bool:
    """Whether the package's own copy of a built-in stands in for the agent's.

    Only for a built-in the agent's folder has not taken over yet: never
    synced, or shipped after the last sync. A tracked built-in is the agent's
    copy or nothing (the owner removed it).
    """
    tracked = tracked_builtins(managed)
    return tracked is None or name not in tracked


def removed_builtins(managed: Path) -> list[str]:
    """Built-ins the owner removed that this Flowly still ships (restorable)."""
    tracked = tracked_builtins(managed) or {}
    return sorted(
        name for name in tracked
        if not (managed / name / "SKILL.md").exists() and (BUNDLED_DIR / name / "SKILL.md").exists()
    )


def provenance(managed: Path, name: str, skill_dir: Path) -> str:
    """Where a skill the loader found came from: bundled, marketplace or local."""
    try:
        if skill_dir.resolve().is_relative_to(BUNDLED_DIR.resolve()):
            return BUNDLED
        in_managed = skill_dir.resolve().parent == managed.resolve()
    except OSError:
        return LOCAL
    if not in_managed:
        return LOCAL
    if (skill_dir / HUB_META_FILE).exists():
        return MARKETPLACE
    if name in (tracked_builtins(managed) or {}):
        return BUNDLED
    return LOCAL


def disabled(managed: Path) -> frozenset[str]:
    """The skills this agent has turned off."""
    try:
        raw = json.loads((managed / STATE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return frozenset()
    names = raw.get("disabled") if isinstance(raw, dict) else None
    if not isinstance(names, list):
        return frozenset()
    return frozenset(name for name in names if isinstance(name, str) and name)


def set_enabled(managed: Path, name: str, enabled: bool) -> frozenset[str]:
    """Turn one skill on or off; returns the agent's off list."""
    with _WRITE_LOCK:
        current = set(disabled(managed))
        if enabled:
            current.discard(name)
        else:
            current.add(name)
        _write_state(managed, {"version": 1, "disabled": sorted(current)})
        return frozenset(current)


def restore_builtin(managed: Path, name: str) -> bool:
    """Copy a removed built-in back from the package; False when it is not one."""
    with _WRITE_LOCK:
        if name not in removed_builtins(managed):
            return False
        source = BUNDLED_DIR / name
        staging = Path(tempfile.mkdtemp(prefix=f".restore-{name}-", dir=managed))
        try:
            shutil.copytree(source, staging / name)
            os.replace(staging / name, managed / name)
        finally:
            shutil.rmtree(staging, ignore_errors=True)
        tracked = tracked_builtins(managed) or {}
        # The copy is the shipped one, so the next sync keeps updating it.
        tracked[name] = dir_hash(managed / name)
        _write_manifest(managed, tracked)
        return True


def dir_hash(directory: Path) -> str:
    """The hash ``sync.py`` tracks a built-in copy by (every file's bytes, sorted by path)."""
    digest = hashlib.md5()
    for path in sorted(directory.rglob("*")):
        if path.is_file() and path.name != MANIFEST_FILE:
            digest.update(path.read_bytes())
    return digest.hexdigest()


def cache_key(managed: Path) -> list:
    """What, besides the SKILL.md files, changes the skills an agent sees (JSON-safe)."""
    try:
        stat = (managed / MANIFEST_FILE).stat()
        manifest: list[int] | None = [stat.st_mtime_ns, stat.st_size]
    except OSError:
        manifest = None
    return [manifest, sorted(disabled(managed))]


def _write_manifest(managed: Path, entries: dict[str, str]) -> None:
    text = "\n".join(f"{name}:{digest}" for name, digest in sorted(entries.items()))
    _atomic_write(managed / MANIFEST_FILE, text)


def _write_state(managed: Path, payload: dict) -> None:
    _atomic_write(managed / STATE_FILE, json.dumps(payload, ensure_ascii=False, indent=2))


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(temp, path)
    except BaseException:
        Path(temp).unlink(missing_ok=True)
        raise
