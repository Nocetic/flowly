"""Bundled skills sync — copy bundled skills to user directory with manifest tracking.

Manifest-based sync with user modification detection.
- New skills: copied, origin hash recorded
- Unmodified skills: updated if bundled changed
- User-modified skills: preserved (not overwritten)
- User-deleted skills: respected (not re-added)
"""

from __future__ import annotations

import hashlib
import os
import shutil
from pathlib import Path

from loguru import logger


def _get_bundled_dir() -> Path:
    """Get bundled skills directory (ships with package)."""
    return Path(__file__).parent


def _get_user_dir() -> Path:
    """Get user skills directory (~/.flowly/skills/)."""
    from flowly.profile import get_flowly_home
    return get_flowly_home() / "skills"


def _manifest_path() -> Path:
    """Path to bundled manifest file."""
    return _get_user_dir() / ".bundled_manifest"


def _dir_hash(directory: Path) -> str:
    """MD5 hash of all file contents in a directory."""
    h = hashlib.md5()
    for f in sorted(directory.rglob("*")):
        if f.is_file() and f.name != ".bundled_manifest":
            h.update(f.read_bytes())
    return h.hexdigest()


def _read_manifest() -> dict[str, str]:
    """Read manifest: {skill_name: origin_hash}."""
    path = _manifest_path()
    if not path.exists():
        return {}

    manifest: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        if ":" in line:
            name, _, hash_val = line.partition(":")
            manifest[name.strip()] = hash_val.strip()
        else:
            # v1 format (name only) — needs baseline
            manifest[line] = ""
    return manifest


def _write_manifest(entries: dict[str, str]) -> None:
    """Write manifest atomically."""
    path = _manifest_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    content = "\n".join(f"{name}:{hash_val}" for name, hash_val in sorted(entries.items()))
    tmp = path.with_suffix(f".tmp.{os.getpid()}")
    tmp.write_text(content, encoding="utf-8")
    os.replace(str(tmp), str(path))


def _discover_bundled() -> list[tuple[str, Path]]:
    """Find all bundled skills (directories with SKILL.md)."""
    bundled_dir = _get_bundled_dir()
    skills = []
    for item in sorted(bundled_dir.iterdir()):
        if item.is_dir() and (item / "SKILL.md").exists():
            skills.append((item.name, item))
    return skills


def sync_skills(quiet: bool = True) -> dict[str, list[str] | int]:
    """Sync bundled skills to user directory.

    Returns summary: {copied, updated, skipped, user_modified, cleaned, total_bundled}
    """
    user_dir = _get_user_dir()
    user_dir.mkdir(parents=True, exist_ok=True)

    manifest = _read_manifest()
    bundled = _discover_bundled()
    bundled_names = {name for name, _ in bundled}

    result: dict[str, list[str] | int] = {
        "copied": [],
        "updated": [],
        "skipped": 0,
        "user_modified": [],
        "cleaned": [],
        "total_bundled": len(bundled),
    }

    new_manifest: dict[str, str] = {}

    for name, bundled_path in bundled:
        dest = user_dir / name
        bundled_hash = _dir_hash(bundled_path)

        if name not in manifest:
            # New skill
            if dest.exists():
                # User already has it (manually created) — don't overwrite
                new_manifest[name] = _dir_hash(dest)
                result["skipped"] += 1  # type: ignore
            else:
                # Copy to user dir
                shutil.copytree(str(bundled_path), str(dest))
                new_manifest[name] = bundled_hash
                result["copied"].append(name)  # type: ignore
                if not quiet:
                    logger.info(f"[SkillSync] Copied: {name}")
        else:
            origin_hash = manifest[name]

            if not origin_hash:
                # v1 migration — set baseline from user's current copy
                if dest.exists():
                    new_manifest[name] = _dir_hash(dest)
                else:
                    new_manifest[name] = bundled_hash
                result["skipped"] += 1  # type: ignore
                continue

            if not dest.exists():
                # User deleted it — respect
                result["skipped"] += 1  # type: ignore
                continue

            user_hash = _dir_hash(dest)

            if user_hash == origin_hash:
                # User hasn't modified
                if bundled_hash != origin_hash:
                    # Bundled updated — copy new version
                    bak = dest.with_suffix(".bak")
                    if bak.exists():
                        shutil.rmtree(str(bak))
                    shutil.copytree(str(dest), str(bak))
                    try:
                        shutil.rmtree(str(dest))
                        shutil.copytree(str(bundled_path), str(dest))
                        new_manifest[name] = bundled_hash
                        result["updated"].append(name)  # type: ignore
                        if not quiet:
                            logger.info(f"[SkillSync] Updated: {name}")
                    except Exception:
                        # Rollback
                        if bak.exists():
                            if dest.exists():
                                shutil.rmtree(str(dest))
                            shutil.copytree(str(bak), str(dest))
                        new_manifest[name] = origin_hash
                    finally:
                        if bak.exists():
                            shutil.rmtree(str(bak))
                else:
                    # No changes
                    new_manifest[name] = origin_hash
                    result["skipped"] += 1  # type: ignore
            else:
                # User modified — preserve
                new_manifest[name] = origin_hash  # Keep original hash
                result["user_modified"].append(name)  # type: ignore
                if not quiet:
                    logger.info(f"[SkillSync] Preserved user-modified: {name}")

    # Clean removed bundled skills from manifest
    for name in manifest:
        if name not in bundled_names and name not in new_manifest:
            result["cleaned"].append(name)  # type: ignore

    _write_manifest(new_manifest)

    if not quiet:
        logger.info(
            f"[SkillSync] Done: {len(result['copied'])} copied, {len(result['updated'])} updated, "
            f"{result['skipped']} skipped, {len(result['user_modified'])} preserved"
        )

    return result


_SYNCED_THIS_PROCESS = False


def _manifest_is_fresh() -> bool:
    """True when the manifest is newer than every bundled skill file — i.e. a
    prior sync already reflects the current bundle.

    Lets short-lived entry points (``flowly agent``) skip the full hash-compare
    on every invocation, while a fresh install / new release (newer bundled
    mtimes) still trips a re-sync.
    """
    manifest = _manifest_path()
    if not manifest.exists():
        return False
    manifest_mtime = manifest.stat().st_mtime
    for f in _get_bundled_dir().rglob("*"):
        if f.is_file() and f.stat().st_mtime > manifest_mtime:
            return False
    return True


def ensure_synced(quiet: bool = True) -> dict[str, list[str] | int] | None:
    """Entry-point-safe bundled-skill sync into ``~/.flowly/skills``.

    Copies newly-bundled skills and refreshes unmodified ones, preserving user
    edits (see :func:`sync_skills`). Guarded to run at most once per process and
    to short-circuit when the manifest already reflects the current bundle, so
    it's cheap enough to call from every launch path. Never raises — a failure
    is logged and swallowed (skills still load in place from the package).
    """
    global _SYNCED_THIS_PROCESS
    if _SYNCED_THIS_PROCESS or _manifest_is_fresh():
        return None
    try:
        result = sync_skills(quiet=quiet)
        _SYNCED_THIS_PROCESS = True
        copied, updated = result.get("copied", []), result.get("updated", [])
        if copied or updated:
            logger.info(f"[SkillSync] {len(copied)} new, {len(updated)} updated skills")
        return result
    except Exception as e:
        logger.warning(f"[SkillSync] Failed (non-critical): {e}")
        return None
