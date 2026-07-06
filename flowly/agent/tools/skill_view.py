"""skill_view tool — progressive disclosure for skill content and linked files.

Dedicated tool (not plain ``read_file``) so the agent gets parsed
frontmatter, body, linked files, and readiness status in one call
instead of having to walk the skill directory itself.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
from pathlib import Path
from typing import Any

from flowly.agent.tools.base import Tool


# Allowed linked file extensions by directory
_LINKED_FILE_EXTENSIONS: dict[str, set[str]] = {
    "references": {".md", ".txt", ".rst"},
    "templates": {".md", ".py", ".yaml", ".yml", ".json", ".sh", ".tex"},
    "scripts": {".py", ".sh", ".bash", ".js", ".ts", ".rb"},
    "assets": set(),  # All extensions allowed
}

_OS_MAP = {"darwin": "macos", "linux": "linux", "win32": "windows"}


def _parse_frontmatter(content: str) -> tuple[dict, str]:
    """Extract YAML frontmatter and body from SKILL.md content."""
    if not content.startswith("---"):
        return {}, content

    end = content.find("\n---", 3)
    if end == -1:
        return {}, content

    yaml_text = content[3:end].strip()
    body = content[end + 4:].strip()

    try:
        import yaml
        loader = getattr(yaml, "CSafeLoader", None) or yaml.SafeLoader
        frontmatter = yaml.load(yaml_text, Loader=loader) or {}
    except Exception:
        # Fallback: simple key: value parsing
        frontmatter = {}
        for line in yaml_text.split("\n"):
            if ":" in line:
                key, _, val = line.partition(":")
                frontmatter[key.strip()] = val.strip().strip('"').strip("'")

    return frontmatter, body


def _get_flowly_metadata(frontmatter: dict) -> dict:
    """Extract flowly-specific metadata from frontmatter."""
    raw = frontmatter.get("metadata", {})
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return {}
    if isinstance(raw, dict):
        return raw.get("flowly", {})
    return {}


def _check_readiness(meta: dict) -> tuple[str, list[str]]:
    """Check skill readiness. Returns (status, missing_requirements)."""
    missing = []

    # OS check
    supported_os = meta.get("os", [])
    if supported_os:
        current = _OS_MAP.get(platform.system().lower(), "")
        if current and current not in supported_os:
            return "unsupported", [f"OS: requires {supported_os}, current is {current}"]

    # Binary check
    requires = meta.get("requires", {})
    for b in requires.get("bins", []):
        if not shutil.which(b):
            missing.append(f"CLI: {b}")

    # Env var check
    for env in requires.get("env", []):
        if not os.environ.get(env):
            missing.append(f"ENV: {env}")

    if missing:
        return "setup_needed", missing
    return "available", []


def _discover_linked_files(skill_dir: Path) -> dict[str, list[str]]:
    """Discover linked files in skill subdirectories."""
    linked: dict[str, list[str]] = {}

    for subdir, allowed_exts in _LINKED_FILE_EXTENSIONS.items():
        sub_path = skill_dir / subdir
        if not sub_path.is_dir():
            continue
        files = []
        for f in sorted(sub_path.rglob("*")):
            if not f.is_file():
                continue
            if allowed_exts and f.suffix.lower() not in allowed_exts:
                continue
            rel = str(f.relative_to(skill_dir))
            files.append(rel)
        if files:
            linked[subdir] = files

    return linked


class SkillViewTool(Tool):
    """View skill content and linked files with progressive disclosure."""

    def __init__(self, workspace: Path):
        self._workspace = workspace
        # Where sync_skills() materializes bundled skills — honor FLOWLY_HOME /
        # the active profile, matching SkillsLoader (not a hardcoded ~/.flowly).
        from flowly.profile import get_flowly_home
        self._managed_dir = get_flowly_home() / "skills"
        self._builtin_dir = Path(__file__).parent.parent.parent / "skills"

    def _find_skill_dir(self, name: str) -> Path | None:
        """Find skill directory across all sources (priority order).

        Supports plugin-namespaced names (``"<plugin>:<skill>"``) — those
        resolve via :func:`PluginManager.find_plugin_skill` and are
        intentionally absent from the regular skill index.
        """
        if ":" in name:
            try:
                from flowly.plugins import get_plugin_manager
                mgr = get_plugin_manager()
            except RuntimeError:
                return None
            skill_md = mgr.find_plugin_skill(name)
            if skill_md is not None and skill_md.exists():
                return skill_md.parent
            return None

        for base in [self._workspace / "skills", self._managed_dir, self._builtin_dir]:
            skill_dir = base / name
            if (skill_dir / "SKILL.md").exists():
                return skill_dir
        return None

    @property
    def name(self) -> str:
        return "skill_view"

    @property
    def description(self) -> str:
        return (
            "Load a skill's full content or access its linked files. "
            "First call returns SKILL.md content plus available references/templates/scripts. "
            "To access linked files, call again with file_path parameter."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "The skill name",
                },
                "file_path": {
                    "type": "string",
                    "description": "Optional: path to a linked file (e.g. 'references/api.md')",
                },
            },
            "required": ["name"],
        }

    async def execute(self, name: str, file_path: str | None = None, **kwargs: Any) -> str:
        skill_dir = self._find_skill_dir(name)
        if not skill_dir:
            return json.dumps({"error": f"Skill '{name}' not found."})

        if file_path:
            return self._load_linked_file(skill_dir, name, file_path)
        return self._load_skill(skill_dir, name)

    def _load_skill(self, skill_dir: Path, name: str) -> str:
        """Tier 2: Load full SKILL.md with parsed metadata."""
        skill_md = skill_dir / "SKILL.md"
        if not skill_md.exists():
            return json.dumps({"error": f"SKILL.md not found for '{name}'"})

        try:
            content = skill_md.read_text(encoding="utf-8")
        except Exception as e:
            return json.dumps({"error": f"Failed to read SKILL.md: {e}"})

        frontmatter, body = _parse_frontmatter(content)
        meta = _get_flowly_metadata(frontmatter)
        readiness, missing = _check_readiness(meta)
        linked_files = _discover_linked_files(skill_dir)

        result = {
            "name": name,
            "description": frontmatter.get("description", ""),
            "content": body,
            "frontmatter": frontmatter,
            "skill_dir": str(skill_dir),
            "linked_files": linked_files,
            "readiness": readiness,
            "missing_requirements": missing,
        }
        # Scripts/templates referenced relatively in the body (e.g. ``scripts/x.py``)
        # live under ``skill_dir`` on disk — for builtin skills that is the package
        # directory, NOT the workspace. Tell the model to run them from there so a
        # bare ``python3 scripts/x.py`` from the default (workspace) cwd doesn't fail.
        if linked_files:
            result["run_hint"] = (
                f"Linked scripts/templates are on disk under skill_dir. Run a script "
                f"with the exec tool using working_dir=skill_dir, e.g. "
                f"exec(command=\"python3 scripts/<file>\", working_dir=\"{skill_dir}\"), "
                f"or invoke it by absolute path \"{skill_dir}/scripts/<file>\"."
            )
        return json.dumps(result, ensure_ascii=False)

    def _load_linked_file(self, skill_dir: Path, name: str, file_path: str) -> str:
        """Tier 3: Load a specific linked file."""
        # Security: prevent path traversal
        if ".." in file_path or file_path.startswith("/"):
            return json.dumps({"error": "Invalid file path — no traversal allowed."})

        # Validate subdirectory
        parts = file_path.split("/")
        if len(parts) < 2 or parts[0] not in _LINKED_FILE_EXTENSIONS:
            return json.dumps({
                "error": f"File must be under: {', '.join(_LINKED_FILE_EXTENSIONS.keys())}"
            })

        target = skill_dir / file_path
        if not target.exists():
            return json.dumps({"error": f"File not found: {file_path}"})
        if not target.is_file():
            return json.dumps({"error": f"Not a file: {file_path}"})

        try:
            content = target.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return json.dumps({"error": f"Binary file — cannot display: {file_path}"})
        except Exception as e:
            return json.dumps({"error": f"Failed to read: {e}"})

        return json.dumps({
            "skill": name,
            "file_path": file_path,
            "content": content,
        }, ensure_ascii=False)
