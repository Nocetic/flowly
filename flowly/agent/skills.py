"""Skills loader for agent capabilities."""

import json
import logging
import os
import platform
import re
import shutil
import tempfile
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Default builtin skills directory (relative to this file)
BUILTIN_SKILLS_DIR = Path(__file__).parent.parent / "skills"


def _resolve_plugin_skill(qualified_name: str) -> Path | None:
    """Look up a plugin-namespaced skill (``"plugin:bare_name"``).

    Plugin skills are NOT enumerated in the regular skills index — they
    are explicit-load only via :func:`skill_view`.  Returns ``None`` if
    the plugin manager is not initialised (CLI/standalone usage) or if
    the qualified name is unknown.
    """
    try:
        from flowly.plugins import get_plugin_manager
        mgr = get_plugin_manager()
    except RuntimeError:
        return None
    return mgr.find_plugin_skill(qualified_name)

# Managed skills directory (installed via flowly-hub)
def _managed_skills_dir() -> Path:
    from flowly.profile import get_flowly_home
    return get_flowly_home() / "skills"

MANAGED_SKILLS_DIR = None  # resolved via _managed_skills_dir()


# ---------------------------------------------------------------------------
# Skills prompt cache — two tiers
# ---------------------------------------------------------------------------
#
# Tier 1 — In-process LRU on the rendered XML, keyed by (dirs +
# available_tools). Hot path; serves repeat calls within a session.
#
# Tier 2 — Disk snapshot of parsed skill metadata at
# ``~/.flowly/.skills_prompt_snapshot.json``. Cold path on the FIRST
# call of a session (LRU empty); reads + JSON parse + manifest match
# is much cheaper than re-walking the three skill directories and
# re-parsing every SKILL.md frontmatter. Survives Flowly restarts.
#
# Snapshot contains METADATA (skill_name, source, description,
# availability, flowly_meta), not the rendered XML — same metadata is
# valid regardless of ``available_tools``, so a single snapshot
# entry serves every LRU variant for the same on-disk skill set. The
# LRU then handles the filter+render layer on top.
#
# Cache invalidation:
#   - Any SKILL.md mtime or size change → manifest mismatch →
#     snapshot ignored, cold path runs, snapshot rewritten.
#   - Schema version bump (``_SKILLS_SNAPSHOT_VERSION``) → snapshot
#     ignored, rewritten with new shape. Use this when changing the
#     entry schema.
#
# Failure handling: every disk operation is best-effort. A corrupt /
# unreadable / unwriteable snapshot file degrades gracefully to the
# pre-snapshot cold-path behaviour. The Tier 1 LRU is unaffected.

_SKILLS_PROMPT_CACHE: OrderedDict = OrderedDict()
_SKILLS_PROMPT_CACHE_MAX = 8

_SKILLS_SNAPSHOT_VERSION = 1
_SKILLS_SNAPSHOT_LOCK = threading.Lock()


def _skills_snapshot_path() -> Path:
    """Resolve the disk snapshot location lazily.

    Resolved per-call (not cached at import time) because
    ``get_flowly_home()`` depends on the active profile, which the
    CLI may switch via ``-p`` after import.
    """
    from flowly.profile import get_flowly_home
    return get_flowly_home() / ".skills_prompt_snapshot.json"


def _atomic_json_write(path: Path, payload: Any) -> None:
    """Write *payload* as JSON to *path* via a temp-file + rename.

    Atomic in the POSIX sense: readers see either the old file or
    the new file, never a partial write. The temp file lives in the
    same directory so the final rename is on the same filesystem.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=path.name + ".",
        suffix=".tmp",
        dir=str(path.parent),
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp_path, path)
        try:
            from flowly.utils.file_security import secure_file
            secure_file(path)  # POSIX chmod; real owner-only ACL on Windows
        except OSError:
            pass
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def clear_skills_snapshot(*, clear_lru: bool = True) -> None:
    """Drop both cache tiers. Public helper for tests + ``flowly``
    debug commands.

    Args:
        clear_lru: When True (default), also wipe the in-process LRU
            so the next ``build_skills_summary`` rebuilds from
            filesystem. When False, only the disk snapshot is removed —
            useful when the LRU is known good and we just want to
            force a snapshot regeneration on next miss.
    """
    if clear_lru:
        _SKILLS_PROMPT_CACHE.clear()
    try:
        _skills_snapshot_path().unlink(missing_ok=True)
    except OSError as e:
        logger.debug("could not remove skills snapshot: %s", e)


class SkillsLoader:
    """
    Loader for agent skills.

    Skills are markdown files (SKILL.md) that teach the agent how to use
    specific tools or perform certain tasks.

    Loading priority (highest to lowest):
    1. Workspace skills (<workspace>/skills/)
    2. Managed skills (~/.flowly/skills/) - installed via flowly-hub
    3. Builtin skills (bundled with package)
    """

    def __init__(self, workspace: Path, builtin_skills_dir: Path | None = None):
        self.workspace = workspace
        self.workspace_skills = workspace / "skills"
        self.managed_skills = _managed_skills_dir()
        self.builtin_skills = builtin_skills_dir or BUILTIN_SKILLS_DIR
    
    def list_skills(self, filter_unavailable: bool = True) -> list[dict[str, str]]:
        """
        List all available skills.

        Args:
            filter_unavailable: If True, filter out skills with unmet requirements.

        Returns:
            List of skill info dicts with 'name', 'path', 'source'.
        """
        skills = []

        # Workspace skills (highest priority)
        if self.workspace_skills.exists():
            for skill_dir in self.workspace_skills.iterdir():
                if skill_dir.is_dir() and not skill_dir.name.startswith("."):
                    skill_file = skill_dir / "SKILL.md"
                    if skill_file.exists():
                        skills.append({"name": skill_dir.name, "path": str(skill_file), "source": "workspace"})

        # Managed skills (installed via flowly-hub)
        if self.managed_skills.exists():
            for skill_dir in self.managed_skills.iterdir():
                if skill_dir.is_dir() and not skill_dir.name.startswith("."):
                    skill_file = skill_dir / "SKILL.md"
                    if skill_file.exists() and not any(s["name"] == skill_dir.name for s in skills):
                        skills.append({"name": skill_dir.name, "path": str(skill_file), "source": "managed"})

        # Built-in skills
        if self.builtin_skills and self.builtin_skills.exists():
            for skill_dir in self.builtin_skills.iterdir():
                if skill_dir.is_dir() and not skill_dir.name.startswith("."):
                    skill_file = skill_dir / "SKILL.md"
                    if skill_file.exists() and not any(s["name"] == skill_dir.name for s in skills):
                        skills.append({"name": skill_dir.name, "path": str(skill_file), "source": "builtin"})

        # Filter by requirements
        if filter_unavailable:
            return [s for s in skills if self._check_requirements(self._get_skill_meta(s["name"]))]
        return skills
    
    def load_skill(self, name: str) -> str | None:
        """
        Load a skill by name.

        Args:
            name: Skill name (directory name) or qualified plugin skill
                  (``"<plugin>:<bare_name>"``).

        Returns:
            Skill content or None if not found.
        """
        # Plugin-namespaced lookup (highest precedence — explicit).
        if ":" in name:
            plugin_skill_path = _resolve_plugin_skill(name)
            if plugin_skill_path is not None:
                return plugin_skill_path.read_text(encoding="utf-8")
            return None

        # Check workspace first (highest priority)
        workspace_skill = self.workspace_skills / name / "SKILL.md"
        if workspace_skill.exists():
            return workspace_skill.read_text(encoding="utf-8")

        # Check managed skills (installed via flowly-hub)
        managed_skill = self.managed_skills / name / "SKILL.md"
        if managed_skill.exists():
            return managed_skill.read_text(encoding="utf-8")

        # Check built-in (lowest priority)
        if self.builtin_skills:
            builtin_skill = self.builtin_skills / name / "SKILL.md"
            if builtin_skill.exists():
                return builtin_skill.read_text(encoding="utf-8")

        return None
    
    def load_skills_for_context(self, skill_names: list[str]) -> str:
        """
        Load specific skills for inclusion in agent context.
        
        Args:
            skill_names: List of skill names to load.
        
        Returns:
            Formatted skills content.
        """
        parts = []
        for name in skill_names:
            content = self.load_skill(name)
            if content:
                content = self._strip_frontmatter(content)
                parts.append(f"### Skill: {name}\n\n{content}")
        
        return "\n\n---\n\n".join(parts) if parts else ""
    
    # ── Disk snapshot helpers ─────────────────────────────────────────

    def _skill_search_roots(self) -> list[tuple[str, Path]]:
        """Return the (source_tag, root_dir) pairs to scan, in priority order.

        Source tags double as namespace prefixes in the manifest so
        two SKILL.md files with the same relative path but in
        different source dirs (rare but possible) don't collide on
        the same manifest key. ``self.builtin_skills`` may be ``None``
        in some test constructions; filter those out.
        """
        candidates: list[tuple[str, Path | None]] = [
            ("workspace", self.workspace_skills),
            ("managed", self.managed_skills),
            ("builtin", self.builtin_skills),
        ]
        return [(tag, root) for tag, root in candidates if root is not None]

    def _build_skills_manifest(self) -> dict[str, list[int]]:
        """Build an mtime/size manifest across every SKILL.md the
        loader can see.

        Manifest keys are ``"<source>:<rel_path>"`` so the same
        relative path under workspace vs builtin produces distinct
        entries. Values are ``[mtime_ns, size]`` — the same shape as
        the underlying ``stat`` result, JSON-serialisable, and
        sufficient to detect any content change (or even a touch).
        Unreadable files are silently skipped (the cold path will
        rediscover them and either fail or fall back gracefully).
        """
        manifest: dict[str, list[int]] = {}
        for source, root in self._skill_search_roots():
            if not root or not root.exists():
                continue
            for skill_file in root.rglob("SKILL.md"):
                # Skip dot-dirs (.archive, .usage.json siblings, backups).
                if any(part.startswith(".") for part in skill_file.relative_to(root).parts):
                    continue
                try:
                    st = skill_file.stat()
                except OSError:
                    continue
                try:
                    rel = skill_file.relative_to(root)
                except ValueError:
                    continue
                manifest[f"{source}:{rel}"] = [st.st_mtime_ns, st.st_size]
        return manifest

    def _load_disk_snapshot(self) -> dict | None:
        """Read the disk snapshot if it exists and is still fresh.

        Returns the snapshot dict on a manifest match, or ``None``
        on any failure path (missing file, JSON corruption, version
        bump, manifest drift). Caller treats ``None`` as "cold path
        needed".
        """
        path = _skills_snapshot_path()
        if not path.exists():
            return None
        try:
            snapshot = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            logger.debug("skills snapshot read failed: %s", e)
            return None
        if not isinstance(snapshot, dict):
            return None
        if snapshot.get("version") != _SKILLS_SNAPSHOT_VERSION:
            return None
        current = self._build_skills_manifest()
        if snapshot.get("manifest") != current:
            return None
        return snapshot

    def _write_disk_snapshot(
        self,
        manifest: dict[str, list[int]],
        entries: list[dict],
    ) -> None:
        """Persist *entries* + *manifest* to the disk snapshot.

        Best-effort: a failed write (full disk, missing parent dir,
        permission denied) is logged at debug and swallowed. Wrapped
        in a process-wide lock to avoid two ``build_skills_summary``
        calls racing to write the same file from different threads.
        """
        payload = {
            "version": _SKILLS_SNAPSHOT_VERSION,
            "manifest": manifest,
            "entries": entries,
        }
        path = _skills_snapshot_path()
        with _SKILLS_SNAPSHOT_LOCK:
            try:
                _atomic_json_write(path, payload)
            except Exception as e:
                logger.debug("skills snapshot write failed: %s", e)

    def _scan_skill_entries(self) -> list[dict]:
        """Cold path — walk the filesystem and produce snapshot entries.

        One entry per visible skill (workspace > managed > builtin
        priority handled exactly as ``list_skills``). Each entry
        carries everything the renderer needs (name, source, path,
        description, availability, flowly metadata for the
        conditional-activation filter) so the renderer can run from
        cached entries without re-reading any SKILL.md.
        """
        home = os.path.expanduser("~")
        entries: list[dict] = []
        for skill in self.list_skills(filter_unavailable=False):
            name = skill["name"]
            raw_path = skill["path"]
            # Collapse home prefix to ~ so the snapshot — and the XML
            # rendered from it — is portable across machines and
            # doesn't leak the OS username into prompt caches.
            display_path = (
                "~" + raw_path[len(home):]
                if raw_path.startswith(home)
                else raw_path
            )
            skill_meta = self._get_skill_meta(name)
            available = self._check_requirements(skill_meta)
            missing = "" if available else self._get_missing_requirements(skill_meta)
            entries.append({
                "name": name,
                "source": skill["source"],
                "path": display_path,
                "description": self._get_skill_description(name),
                "available": available,
                "missing": missing,
                "skill_meta": skill_meta,
            })
        return entries

    def _render_skills_xml(
        self,
        entries: list[dict],
        available_tools: set[str] | None,
    ) -> str:
        """Render the ``<skills>`` XML from snapshot entries.

        Cheap — pure string assembly, no I/O, no parsing. Filter is
        applied here so a single snapshot serves every value of
        ``available_tools`` the LRU sees.
        """
        def escape_xml(s: str) -> str:
            return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

        def truncate_desc(desc: str, max_len: int = 60) -> str:
            if len(desc) <= max_len:
                return desc
            return desc[:max_len - 3] + "..."

        lines = ["<skills>"]
        for entry in entries:
            skill_meta = entry.get("skill_meta") or {}
            if available_tools and not self._skill_should_show(skill_meta, available_tools):
                continue
            available = bool(entry.get("available", True))
            name = escape_xml(entry["name"])
            desc = truncate_desc(escape_xml(entry.get("description") or name))
            path = escape_xml(entry["path"])
            lines.append(f"  <skill available=\"{str(available).lower()}\">")
            lines.append(f"    <name>{name}</name>")
            lines.append(f"    <description>{desc}</description>")
            lines.append(f"    <location>{path}</location>")
            if not available:
                missing = entry.get("missing") or ""
                if missing:
                    lines.append(f"    <requires>{escape_xml(missing)}</requires>")
            lines.append("  </skill>")
        lines.append("</skills>")
        return "\n".join(lines)

    def build_skills_summary(self, available_tools: set[str] | None = None) -> str:
        """Build XML summary of skills with caching and conditional activation.

        Two-tier cache (see module docstring at top of file):

          1. **In-process LRU** keyed by (dirs + available_tools)
             returns the fully-rendered XML in O(1).
          2. **Disk snapshot** at ``~/.flowly/.skills_prompt_snapshot.json``
             serves the cold path — saves a full filesystem walk +
             frontmatter parse on every Flowly restart, which used to
             cost ~100-200ms on the first turn of a session.

        Descriptions truncated to 60 chars to bound prompt size.
        Skills filtered by conditional activation (``requires_tools``,
        ``fallback_for_tools``).
        """
        # ── Tier 1: in-process LRU ────────────────────────────────────
        cache_key = (
            str(self.workspace_skills),
            str(self.managed_skills),
            str(self.builtin_skills),
            tuple(sorted(available_tools)) if available_tools else (),
        )
        if cache_key in _SKILLS_PROMPT_CACHE:
            _SKILLS_PROMPT_CACHE.move_to_end(cache_key)
            return _SKILLS_PROMPT_CACHE[cache_key]

        # ── Tier 2: disk snapshot ─────────────────────────────────────
        snapshot = self._load_disk_snapshot()
        if snapshot is not None:
            entries = snapshot.get("entries") or []
        else:
            # ── Cold path: filesystem scan ────────────────────────────
            entries = self._scan_skill_entries()
            if entries:
                manifest = self._build_skills_manifest()
                self._write_disk_snapshot(manifest, entries)

        if not entries:
            return ""

        result = self._render_skills_xml(entries, available_tools)

        # Save to LRU
        _SKILLS_PROMPT_CACHE[cache_key] = result
        if len(_SKILLS_PROMPT_CACHE) > _SKILLS_PROMPT_CACHE_MAX:
            _SKILLS_PROMPT_CACHE.popitem(last=False)

        return result

    @staticmethod
    def _skill_should_show(meta: dict, available_tools: set[str]) -> bool:
        """Check conditional activation rules.

        Skills can declare ``requires_tools`` (hide unless all listed
        tools are available) and ``fallback_for_tools`` (hide when
        any listed tool IS available — the skill is a fallback for
        when they aren't).
        """
        requires = meta.get("requires_tools", [])
        fallback = meta.get("fallback_for_tools", [])

        for t in requires:
            if t not in available_tools:
                return False
        for t in fallback:
            if t in available_tools:
                return False
        return True
    
    def _get_missing_requirements(self, skill_meta: dict) -> str:
        """Get a description of missing requirements."""
        missing = []
        requires = skill_meta.get("requires", {})
        for b in requires.get("bins", []):
            if not shutil.which(b):
                missing.append(f"CLI: {b}")
        for env in requires.get("env", []):
            if not os.environ.get(env):
                missing.append(f"ENV: {env}")
        return ", ".join(missing)
    
    def _get_skill_description(self, name: str) -> str:
        """Get the description of a skill from its frontmatter."""
        meta = self.get_skill_metadata(name)
        if meta and meta.get("description"):
            return meta["description"]
        return name  # Fallback to skill name
    
    def _strip_frontmatter(self, content: str) -> str:
        """Remove YAML frontmatter from markdown content."""
        if content.startswith("---"):
            match = re.match(r"^---\n.*?\n---\n", content, re.DOTALL)
            if match:
                return content[match.end():].strip()
        return content
    
    def _parse_flowly_metadata(self, raw: str) -> dict:
        """Parse flowly metadata JSON from frontmatter."""
        try:
            data = json.loads(raw)
            return data.get("flowly", {}) if isinstance(data, dict) else {}
        except (json.JSONDecodeError, TypeError):
            return {}
    
    def _check_requirements(self, skill_meta: dict) -> bool:
        """Check if skill requirements are met (bins, env vars, OS)."""
        # Check OS compatibility
        supported_os = skill_meta.get("os", [])
        if supported_os:
            current_os = platform.system().lower()
            os_map = {"windows": "windows", "darwin": "darwin", "linux": "linux"}
            if os_map.get(current_os, current_os) not in supported_os:
                return False

        requires = skill_meta.get("requires", {})
        for b in requires.get("bins", []):
            if not shutil.which(b):
                return False
        for env in requires.get("env", []):
            if not os.environ.get(env):
                return False
        return True
    
    def _get_skill_meta(self, name: str) -> dict:
        """Get flowly metadata for a skill (cached in frontmatter)."""
        meta = self.get_skill_metadata(name) or {}
        return self._parse_flowly_metadata(meta.get("metadata", ""))
    
    def get_always_skills(self) -> list[str]:
        """Get skills marked as always=true that meet requirements."""
        result = []
        for s in self.list_skills(filter_unavailable=True):
            meta = self.get_skill_metadata(s["name"]) or {}
            skill_meta = self._parse_flowly_metadata(meta.get("metadata", ""))
            if skill_meta.get("always") or meta.get("always"):
                result.append(s["name"])
        return result
    
    def get_skill_metadata(self, name: str) -> dict | None:
        """
        Get metadata from a skill's frontmatter.
        
        Args:
            name: Skill name.
        
        Returns:
            Metadata dict or None.
        """
        content = self.load_skill(name)
        if not content:
            return None
        
        if content.startswith("---"):
            match = re.match(r"^---\n(.*?)\n---", content, re.DOTALL)
            if match:
                # Simple YAML parsing
                metadata = {}
                for line in match.group(1).split("\n"):
                    if ":" in line:
                        key, value = line.split(":", 1)
                        metadata[key.strip()] = value.strip().strip('"\'')
                return metadata
        
        return None
