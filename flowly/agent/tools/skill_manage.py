"""Skill management tool — create, patch, edit, delete, and list skills.

Skills are the agent's procedural memory: reusable approaches saved as
markdown files (SKILL.md) under ``~/.flowly/skills/{name}/``.

All write operations:
  1. Validate the skill name (lowercase, hyphens/underscores, max 64 chars).
  2. Validate YAML frontmatter (``description`` field required for create/edit).
  3. Run ``content_guard.scan_content()`` to block prompt-injection payloads.
  4. Use atomic writes (temp file + ``os.replace``) to prevent corruption.
  5. Roll back on security-scan failure.
"""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path
from typing import Any

from flowly.agent.tools.base import Tool
from flowly.agent.tools.content_guard import scan_content

# Where agent-created skills live
def _skills_dir() -> Path:
    from flowly.profile import get_flowly_home
    return get_flowly_home() / "skills"


def _archive_dir() -> Path:
    # OUTSIDE skills/ so the skills scanner never sees archived skills.
    from flowly.profile import get_flowly_home
    return get_flowly_home() / "skills_archive"

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def _normalize_whitespace(text: str) -> str:
    """Normalize whitespace for fuzzy matching: strip each line, collapse spaces."""
    return "\n".join(line.strip() for line in text.split("\n"))


# ── Helpers ────────────────────────────────────────────────────────


def _validate_name(name: str) -> str | None:
    """Return error string if name is invalid, else None."""
    if not name:
        return "Skill name is required."
    if not _NAME_RE.match(name):
        return (
            f"Invalid skill name '{name}'. "
            "Use lowercase letters, digits, hyphens, underscores (max 64 chars). "
            "Must start with a letter or digit."
        )
    return None


def _validate_frontmatter(content: str) -> str | None:
    """Return error string if SKILL.md frontmatter is missing/broken."""
    if not content.startswith("---"):
        return "SKILL.md must start with YAML frontmatter (---). Example:\n---\ndescription: \"What the skill does\"\n---"
    match = re.match(r"^---\n(.*?)\n---", content, re.DOTALL)
    if not match:
        return "SKILL.md frontmatter is not closed (missing second ---)."
    fm = match.group(1)
    if "description" not in fm:
        return "SKILL.md frontmatter must include a 'description' field."
    return None


def _find_skill(name: str) -> Path | None:
    """Find an existing skill directory by name across all locations."""
    # Managed/agent-created skills
    candidate = _skills_dir() / name
    if candidate.is_dir() and (candidate / "SKILL.md").exists():
        return candidate
    # Workspace skills would be at workspace/skills/{name} but we don't
    # know workspace here — callers should check workspace separately.
    return None


def _atomic_write(path: Path, content: str) -> None:
    """Write content atomically via temp file + os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        dir=str(path.parent),
        prefix=f".{path.name}.tmp.",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp, str(path))
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ── Tool class ─────────────────────────────────────────────────────


class SkillManageTool(Tool):
    """Tool for creating, updating, and deleting agent skills."""

    def __init__(self, workspace: Path | None = None):
        self._workspace = workspace

    @property
    def name(self) -> str:
        return "skill_manage"

    @property
    def description(self) -> str:
        return (
            "Manage skills (create, patch, edit, delete, list). "
            "Use this only when the user explicitly asks to create, update, "
            "delete, or inspect skills."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["create", "patch", "edit", "delete", "list", "write_file", "remove_file", "archive", "restore"],
                    "description": "The action to perform.",
                },
                "name": {
                    "type": "string",
                    "description": (
                        "Skill name (lowercase, hyphens/underscores, max 64 chars). "
                        "Required for all actions except 'list'."
                    ),
                },
                "content": {
                    "type": "string",
                    "description": (
                        "Full SKILL.md content (YAML frontmatter + markdown body). "
                        "Required for 'create' and 'edit'."
                    ),
                },
                "old_string": {
                    "type": "string",
                    "description": "Text to find in SKILL.md (required for 'patch').",
                },
                "new_string": {
                    "type": "string",
                    "description": "Replacement text (required for 'patch').",
                },
                "file_path": {
                    "type": "string",
                    "description": "Path under skill dir for write_file/remove_file (e.g. 'references/api.md').",
                },
                "file_content": {
                    "type": "string",
                    "description": "Content for write_file action.",
                },
                "replace_all": {
                    "type": "boolean",
                    "description": "Replace all occurrences in patch (default: false).",
                },
            },
            "required": ["action"],
        }

    async def execute(self, action: str, **kwargs: Any) -> str:
        if action == "list":
            return self._list()
        if action == "create":
            return self._create(kwargs.get("name", ""), kwargs.get("content", ""))
        if action == "patch":
            return self._patch(
                kwargs.get("name", ""),
                kwargs.get("old_string", ""),
                kwargs.get("new_string", ""),
                kwargs.get("replace_all", False),
            )
        if action == "edit":
            return self._edit(kwargs.get("name", ""), kwargs.get("content", ""))
        if action == "delete":
            return self._delete(kwargs.get("name", ""))
        if action == "write_file":
            return self._write_supporting_file(
                kwargs.get("name", ""),
                kwargs.get("file_path", ""),
                kwargs.get("file_content", ""),
            )
        if action == "remove_file":
            return self._remove_supporting_file(
                kwargs.get("name", ""),
                kwargs.get("file_path", ""),
            )
        if action == "archive":
            return self._archive(kwargs.get("name", ""))
        if action == "restore":
            return self._restore(kwargs.get("name", ""))
        return f"Error: Unknown action '{action}'. Use: create, patch, edit, delete, list, write_file, remove_file, archive, restore."

    # ── Actions ────────────────────────────────────────────────────

    def _list(self) -> str:
        """List all agent-created skills."""
        _skills_dir().mkdir(parents=True, exist_ok=True)
        skills = []
        for d in sorted(_skills_dir().iterdir()):
            if d.name.startswith("."):   # skip .usage.json, .archive, etc.
                continue
            if d.is_dir() and (d / "SKILL.md").exists():
                # Read first line of description from frontmatter
                desc = ""
                try:
                    text = (d / "SKILL.md").read_text(encoding="utf-8")
                    m = re.search(r"description:\s*[\"']?(.+?)[\"']?\s*$", text, re.MULTILINE)
                    if m:
                        desc = m.group(1)
                except Exception:
                    pass
                skills.append(f"- {d.name}: {desc}" if desc else f"- {d.name}")

        if not skills:
            return "No agent-created skills found. Use action='create' to make one."
        return "Agent-created skills:\n" + "\n".join(skills)

    def _archive(self, name: str) -> str:
        """Move a skill to ~/.flowly/skills_archive/ (never delete; restorable)."""
        import shutil
        err = _validate_name(name)
        if err:
            return f"Error: {err}"
        src = _skills_dir() / name
        if not (src.is_dir() and (src / "SKILL.md").exists()):
            return f"Error: skill '{name}' not found."
        dest_root = _archive_dir()
        dest_root.mkdir(parents=True, exist_ok=True)
        dest = dest_root / name
        if dest.exists():
            from datetime import datetime, timezone
            dest = dest_root / f"{name}.{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}"
        try:
            shutil.move(str(src), str(dest))
        except Exception as exc:
            return f"Error: archive failed: {exc}"
        return f"Archived skill '{name}' → {dest}"

    def _restore(self, name: str) -> str:
        """Move an archived skill back into ~/.flowly/skills/."""
        import shutil
        err = _validate_name(name)
        if err:
            return f"Error: {err}"
        src = _archive_dir() / name
        if not (src.is_dir() and (src / "SKILL.md").exists()):
            return f"Error: archived skill '{name}' not found."
        dest = _skills_dir() / name
        if dest.exists():
            return f"Error: a live skill '{name}' already exists; will not overwrite."
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.move(str(src), str(dest))
        except Exception as exc:
            return f"Error: restore failed: {exc}"
        return f"Restored skill '{name}' from archive."

    def _create(self, name: str, content: str) -> str:
        """Create a new skill."""
        err = _validate_name(name)
        if err:
            return f"Error: {err}"

        if not content:
            return "Error: 'content' is required for create. Provide full SKILL.md content."

        err = _validate_frontmatter(content)
        if err:
            return f"Error: {err}"

        # Check name collision
        existing = _find_skill(name)
        if existing:
            return f"Error: Skill '{name}' already exists at {existing}. Use 'patch' or 'edit' to update."

        # Security scan
        violation = scan_content(content)
        if violation:
            return f"Error: {violation}"

        # Create
        skill_dir = _skills_dir() / name
        skill_md = skill_dir / "SKILL.md"
        _atomic_write(skill_md, content)

        return f"Skill '{name}' created at {skill_dir}."

    def _patch(self, name: str, old_string: str, new_string: str, replace_all: bool = False) -> str:
        """Patch a skill with fuzzy find-replace — indentation-flexible."""
        err = _validate_name(name)
        if err:
            return f"Error: {err}"

        if not old_string:
            return "Error: 'old_string' is required for patch."
        if new_string is None:
            return "Error: 'new_string' is required for patch (use empty string to delete text)."

        existing = _find_skill(name)
        if not existing:
            return f"Error: Skill '{name}' not found. Use 'list' to see available skills."

        skill_md = existing / "SKILL.md"
        original = skill_md.read_text(encoding="utf-8")

        # Try exact match first
        count = original.count(old_string)
        if count == 0:
            # Fuzzy match: normalize whitespace per line
            normalized_old = _normalize_whitespace(old_string)
            normalized_content = _normalize_whitespace(original)
            if normalized_old in normalized_content:
                # Find the original position by line matching
                old_lines = [l.strip() for l in old_string.strip().split("\n")]
                orig_lines = original.split("\n")
                match_start = None
                for i in range(len(orig_lines)):
                    if orig_lines[i].strip() == old_lines[0]:
                        if all(
                            i + j < len(orig_lines) and orig_lines[i + j].strip() == old_lines[j]
                            for j in range(len(old_lines))
                        ):
                            match_start = i
                            break
                if match_start is not None:
                    # Replace the matched lines preserving original indentation of first line
                    indent = len(orig_lines[match_start]) - len(orig_lines[match_start].lstrip())
                    prefix = orig_lines[match_start][:indent]
                    new_lines = new_string.split("\n")
                    indented_new = [prefix + l if j > 0 else prefix + l.lstrip() for j, l in enumerate(new_lines)]
                    orig_lines[match_start:match_start + len(old_lines)] = indented_new
                    new_content = "\n".join(orig_lines)
                    count = 1
                else:
                    count = 0
            else:
                count = 0

            if count == 0:
                preview = original[:400] + ("..." if len(original) > 400 else "")
                return f"Error: old_string not found in SKILL.md (tried exact and fuzzy match).\n\nFile preview:\n{preview}"
            new_content = "\n".join(orig_lines) if 'orig_lines' in dir() else original
        elif count > 1 and not replace_all:
            return f"Error: old_string matched {count} times. Use replace_all=true or provide more context."
        else:
            new_content = original.replace(old_string, new_string) if replace_all else original.replace(old_string, new_string, 1)

        err = _validate_frontmatter(new_content)
        if err:
            return f"Error: Patch would break SKILL.md structure: {err}"

        violation = scan_content(new_content)
        if violation:
            return f"Error: {violation}"

        _atomic_write(skill_md, new_content)
        return f"Skill '{name}' patched successfully."

    def _edit(self, name: str, content: str) -> str:
        """Full rewrite of a skill's SKILL.md."""
        err = _validate_name(name)
        if err:
            return f"Error: {err}"

        if not content:
            return "Error: 'content' is required for edit. Provide full SKILL.md content."

        err = _validate_frontmatter(content)
        if err:
            return f"Error: {err}"

        existing = _find_skill(name)
        if not existing:
            return f"Error: Skill '{name}' not found. Use 'create' for new skills."

        # Security scan
        violation = scan_content(content)
        if violation:
            return f"Error: {violation}"

        skill_md = existing / "SKILL.md"
        _atomic_write(skill_md, content)
        return f"Skill '{name}' updated."

    def _delete(self, name: str) -> str:
        """Delete a skill directory."""
        err = _validate_name(name)
        if err:
            return f"Error: {err}"

        existing = _find_skill(name)
        if not existing:
            return f"Error: Skill '{name}' not found."

        import shutil
        shutil.rmtree(existing)
        return f"Skill '{name}' deleted."

    # ── Supporting files (references/templates/scripts/assets) ────

    _ALLOWED_SUBDIRS = {"references", "templates", "scripts", "assets"}
    _MAX_FILE_BYTES = 1_048_576  # 1MB

    def _write_supporting_file(self, name: str, file_path: str, file_content: str) -> str:
        """Write a supporting file under a skill directory."""
        err = _validate_name(name)
        if err:
            return f"Error: {err}"
        if not file_path or not file_content:
            return "Error: 'file_path' and 'file_content' required for write_file."

        existing = _find_skill(name)
        if not existing:
            return f"Error: Skill '{name}' not found."

        # Path validation
        if ".." in file_path or file_path.startswith("/"):
            return "Error: Invalid path — no traversal allowed."
        parts = file_path.split("/")
        if len(parts) < 2 or parts[0] not in self._ALLOWED_SUBDIRS:
            return f"Error: File must be under: {', '.join(sorted(self._ALLOWED_SUBDIRS))}"

        if len(file_content.encode("utf-8")) > self._MAX_FILE_BYTES:
            return f"Error: File exceeds {self._MAX_FILE_BYTES // 1024}KB limit."

        violation = scan_content(file_content)
        if violation:
            return f"Error: {violation}"

        target = existing / file_path
        target.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(target, file_content)
        return f"File '{file_path}' written to skill '{name}'."

    def _remove_supporting_file(self, name: str, file_path: str) -> str:
        """Remove a supporting file from a skill directory."""
        err = _validate_name(name)
        if err:
            return f"Error: {err}"
        if not file_path:
            return "Error: 'file_path' required for remove_file."

        existing = _find_skill(name)
        if not existing:
            return f"Error: Skill '{name}' not found."

        if ".." in file_path or file_path.startswith("/"):
            return "Error: Invalid path — no traversal allowed."

        target = existing / file_path
        if not target.exists():
            return f"Error: File '{file_path}' not found in skill '{name}'."

        target.unlink()
        # Clean up empty parent dirs
        parent = target.parent
        if parent != existing and not any(parent.iterdir()):
            parent.rmdir()
        return f"File '{file_path}' removed from skill '{name}'."
