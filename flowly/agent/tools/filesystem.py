"""File system tools: read, write, edit, memory_append."""

from datetime import datetime
from pathlib import Path
from typing import Any

from flowly.agent.tools.base import Tool
from flowly.agent.tools.content_guard import is_memory_path, scan_content
from flowly.agent.tools._arg_coercion import coerce_int as _coerce_int
from flowly.agent.tools._lint import check_delta as _lint_check_delta, is_lintable as _lint_is_lintable
from flowly.protected_paths import is_protected_path

def _get_allowed_prefixes() -> tuple[Path, ...]:
    """Allowed paths outside workspace.

    Includes the agent's data dir plus the user's own export-style folders
    (Downloads / Desktop / Documents). The agent shouldn't be able to roam
    the entire home directory, but it does need a place to put exports
    (artifacts, summaries, generated files) that the user can find. Sensitive
    paths under ``~/.flowly`` are still blocked via ``_get_denied_paths``.
    """
    from flowly.profile import get_flowly_home
    from flowly.agent.tool_result_spill import get_spill_dir
    home = Path.home()
    return (
        get_flowly_home(),
        # Oversized tool results spilled to temp — read_file must be able
        # to read them back. Only this one directory, not the whole temp.
        get_spill_dir(),
        home / "Downloads",
        home / "Desktop",
        home / "Documents",
    )


def _get_denied_paths() -> tuple[Path, ...]:
    """Sensitive paths that must never be readable by the agent."""
    from flowly.profile import get_flowly_home
    home = get_flowly_home()
    return (
        home / "config.json",
        home / "credentials",
        home / "electron-api.json",
        home / "sessions",
    )


def _is_path_allowed(resolved_path: Path, workspace: Path | None) -> bool:
    """Check if a resolved path is within workspace or allowed prefixes."""
    # Hardcoded protected paths — SSH keys, AWS creds, keychain, system
    # secrets. These come first and are not overridable by allowlists.
    # See ``flowly.protected_paths`` for the full list and rationale.
    if is_protected_path(resolved_path):
        return False

    # Deny sensitive files even within allowed prefixes
    for denied in _get_denied_paths():
        try:
            resolved_path.relative_to(denied.resolve())
            return False
        except ValueError:
            continue
        except OSError:
            continue

    # Always allow flowly data directory
    for prefix in _get_allowed_prefixes():
        try:
            resolved_path.relative_to(prefix.resolve())
            return True
        except ValueError:
            continue

    # Allow workspace and its children
    if workspace:
        try:
            resolved_path.relative_to(workspace.resolve())
            return True
        except ValueError:
            pass

    return False


def _is_read_allowed(resolved_path: Path, workspace: Path | None) -> bool:
    """Read-side policy: the write policy PLUS the user's home tree.

    Relaxation (2026-06): the agent may READ anything under
    the user's home directory — "inspect the project in ~/my-repo" used to
    fail with "Access denied — path outside workspace" and confuse the
    model into asking the user instead of just looking. Writes keep the
    strict workspace + export-folders policy (``_is_path_allowed``).

    The security floor is unchanged and checked FIRST: hardcoded protected
    paths (~/.ssh, ~/.aws, Keychain, browser credentials — see
    ``flowly.protected_paths``) and Flowly's own auth artifacts
    (``_get_denied_paths``) stay unreadable even though they live in home.
    """
    if is_protected_path(resolved_path):
        return False
    for denied in _get_denied_paths():
        try:
            resolved_path.relative_to(denied.resolve())
            return False
        except ValueError:
            continue
        except OSError:
            continue

    try:
        resolved_path.relative_to(Path.home().resolve())
        return True
    except ValueError:
        pass

    # Outside home: fall back to the write-side allowlist (covers the
    # workspace and the spill dir, which live outside home on some setups).
    return _is_path_allowed(resolved_path, workspace)


def _read_denied_error(path: str, resolved_path: Path) -> str:
    """Actionable denial message for read tools.

    Weak models lock up on a bare "Access denied" — they ask the user
    instead of recovering. Name the reason and the next step.
    """
    if is_protected_path(resolved_path) or any(
        _is_under(resolved_path, d) for d in _get_denied_paths()
    ):
        return (
            f"Error: Access denied — {path} is a protected credentials/secrets "
            "path. Access is never granted; do not retry. If the user needs "
            "this content they must open it themselves."
        )
    return (
        f"Error: Access denied — {path} is outside the readable area "
        "(home directory, workspace). If you need this system path, try the "
        "exec tool instead (it has its own approval flow), e.g. "
        f"exec: cat {path}"
    )


def _write_denied_error(path: str, workspace: Path | None) -> str:
    """Actionable denial message for write/edit tools."""
    ws = str(workspace) if workspace else "~/.flowly/workspace"
    return (
        f"Error: write access denied — {path}. Writable locations: the "
        f"workspace ({ws}), ~/Downloads, ~/Desktop, ~/Documents. Re-run "
        "with a path under one of those, or ask the user where to save."
    )


def _is_under(resolved_path: Path, root: Path) -> bool:
    try:
        resolved_path.relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


class ReadFileTool(Tool):
    """Tool to read file contents."""

    def __init__(self, workspace: Path | None = None):
        self.workspace = workspace

    @property
    def name(self) -> str:
        return "read_file"

    @property
    def description(self) -> str:
        return (
            "Read the contents of a file at the given path. Readable area: "
            "anywhere under the user's home directory (read-only) plus the "
            "workspace; credentials paths (~/.ssh etc.) stay blocked. For "
            "large files, pass offset (1-based start line) and limit (number "
            "of lines) to read a specific section instead of the whole file."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "The file path to read"
                },
                "offset": {
                    "type": "integer",
                    "description": "1-based line number to start reading from (default: 1)"
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of lines to return (default: whole file)"
                }
            },
            "required": ["path"]
        }

    async def execute(self, path: str, **kwargs: Any) -> str:
        try:
            file_path = Path(path).expanduser().resolve()

            if not _is_read_allowed(file_path, self.workspace):
                return _read_denied_error(path, file_path)

            if not file_path.exists():
                return f"Error: File not found: {path}"
            if not file_path.is_file():
                return f"Error: Not a file: {path}"

            content = file_path.read_text(encoding="utf-8")

            offset = _coerce_int(kwargs.get("offset"), default=1)
            limit = _coerce_int(kwargs.get("limit"), default=0)
            if offset > 1 or limit > 0:
                lines = content.splitlines(keepends=True)
                total = len(lines)
                start = max(offset, 1) - 1
                if start >= total:
                    return (
                        f"Error: offset {offset} is past the end of the file "
                        f"({total} lines total)."
                    )
                end = min(start + limit, total) if limit > 0 else total
                section = "".join(lines[start:end])
                if end < total:
                    section += (
                        f"\n[showing lines {start + 1}-{end} of {total} — "
                        f"continue with offset={end + 1}]"
                    )
                return section
            return content
        except PermissionError:
            return f"Error: Permission denied: {path}"
        except Exception as e:
            return f"Error reading file: {str(e)}"


class WriteFileTool(Tool):
    """Tool to write content to a file."""

    def __init__(self, workspace: Path | None = None):
        self.workspace = workspace

    @property
    def name(self) -> str:
        return "write_file"

    @property
    def description(self) -> str:
        return (
            "Write content to a file at the given path. Creates parent directories "
            "if needed. Allowed locations: the active workspace and the user's own "
            "folders (~/Downloads, ~/Desktop, ~/Documents). Use ~/Downloads when "
            "exporting files for the user to find."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "The file path to write to"
                },
                "content": {
                    "type": "string",
                    "description": "The content to write"
                }
            },
            "required": ["path", "content"]
        }

    async def execute(self, path: str, content: str, **kwargs: Any) -> str:
        try:
            file_path = Path(path).expanduser().resolve()

            if not _is_path_allowed(file_path, self.workspace):
                return _write_denied_error(path, self.workspace)

            if is_memory_path(file_path, self.workspace):
                violation = scan_content(content)
                if violation:
                    return f"Error: {violation}"

            pre_content: str | None = None
            if _lint_is_lintable(str(file_path)) and file_path.exists():
                try:
                    pre_content = file_path.read_text(encoding="utf-8")
                except Exception:
                    pre_content = None

            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_text(content, encoding="utf-8")

            result = f"Successfully wrote {len(content)} bytes to {path}"
            warning = _lint_check_delta(str(file_path), pre_content, content)
            if warning:
                result += f"\n⚠ {warning}"
            return result
        except PermissionError:
            return f"Error: Permission denied: {path}"
        except Exception as e:
            return f"Error writing file: {str(e)}"


class EditFileTool(Tool):
    """Tool to edit a file by replacing text."""

    def __init__(self, workspace: Path | None = None):
        self.workspace = workspace

    @property
    def name(self) -> str:
        return "edit_file"

    @property
    def description(self) -> str:
        return "Edit a file by replacing old_text with new_text. The old_text must exist exactly in the file."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "The file path to edit"
                },
                "old_text": {
                    "type": "string",
                    "description": "The exact text to find and replace"
                },
                "new_text": {
                    "type": "string",
                    "description": "The text to replace with"
                }
            },
            "required": ["path", "old_text", "new_text"]
        }

    async def execute(self, path: str, old_text: str, new_text: str, **kwargs: Any) -> str:
        try:
            file_path = Path(path).expanduser().resolve()

            if not _is_path_allowed(file_path, self.workspace):
                return _write_denied_error(path, self.workspace)

            if not file_path.exists():
                return f"Error: File not found: {path}"

            if is_memory_path(file_path, self.workspace):
                violation = scan_content(new_text)
                if violation:
                    return f"Error: {violation}"

            content = file_path.read_text(encoding="utf-8")

            if old_text not in content:
                return f"Error: old_text not found in file. Make sure it matches exactly."

            # Count occurrences
            count = content.count(old_text)
            if count > 1:
                return f"Warning: old_text appears {count} times. Please provide more context to make it unique."

            new_content = content.replace(old_text, new_text, 1)
            file_path.write_text(new_content, encoding="utf-8")

            result = f"Successfully edited {path}"
            warning = _lint_check_delta(str(file_path), content, new_content)
            if warning:
                result += f"\n⚠ {warning}"
            return result
        except PermissionError:
            return f"Error: Permission denied: {path}"
        except Exception as e:
            return f"Error editing file: {str(e)}"


class ListDirTool(Tool):
    """Tool to list directory contents."""

    def __init__(self, workspace: Path | None = None):
        self.workspace = workspace

    @property
    def name(self) -> str:
        return "list_dir"

    @property
    def description(self) -> str:
        return (
            "List the contents of a directory. Readable area: anywhere under "
            "the user's home directory (read-only) plus the workspace."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "The directory path to list"
                }
            },
            "required": ["path"]
        }

    async def execute(self, path: str, **kwargs: Any) -> str:
        try:
            dir_path = Path(path).expanduser().resolve()

            if not _is_read_allowed(dir_path, self.workspace):
                return _read_denied_error(path, dir_path)

            if not dir_path.exists():
                return f"Error: Directory not found: {path}"
            if not dir_path.is_dir():
                return f"Error: Not a directory: {path}"

            items = []
            for item in sorted(dir_path.iterdir()):
                # Per-entry stat can fail even when listing succeeds: the
                # agent's macOS sandbox denies file-read-metadata on
                # protected entries (~/.ssh, browser profiles, …), so
                # is_dir() raises PermissionError mid-listing. One blocked
                # entry must not kill the whole directory listing — mark
                # it inaccessible and keep going.
                try:
                    prefix = "d " if item.is_dir() else "f "
                except OSError:
                    prefix = "- "  # exists, but metadata is not accessible
                items.append(f"{prefix}{item.name}")

            if not items:
                return f"Directory {path} is empty"

            return "\n".join(items)
        except PermissionError:
            return (
                f"Error: Permission denied: {path} — the agent's OS sandbox "
                "blocks this directory. Try a more specific subdirectory, or "
                "use the exec tool (e.g. exec: ls <path>)."
            )
        except Exception as e:
            return f"Error listing directory: {str(e)}"


class MemoryAppendTool(Tool):
    """
    Safely append a note to memory/MEMORY.md with duplicate protection.

    Checks for exact hash match AND near-duplicate similarity before appending.
    Enforces a size cap — oldest entries evicted when limit reached.
    """

    # Max total MEMORY.md size in chars. Oldest entries removed when exceeded.
    MAX_MEMORY_CHARS = 12000

    # Similarity threshold (0-1). Above this = duplicate, rejected.
    SIMILARITY_THRESHOLD = 0.75

    def __init__(self, workspace: Path):
        self.workspace = workspace

    @property
    def name(self) -> str:
        return "memory_append"

    @property
    def description(self) -> str:
        return (
            "Append a note to long-term memory (memory/MEMORY.md). "
            "Use for general preferences, environment details, and free-form notes. "
            "For structured facts about people, projects, emails, and relationships, "
            "use knowledge_graph tool instead. "
            "Duplicate entries are automatically rejected."
        )

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "The note to append. Be concise and factual.",
                }
            },
            "required": ["content"],
        }

    @staticmethod
    def _parse_entries(text: str) -> list[str]:
        """Split MEMORY.md into timestamped entries."""
        import re
        blocks = re.split(r'(?=\n*<!-- \d{4}-\d{2}-\d{2})', text)
        return [b.strip() for b in blocks if b.strip()]

    @staticmethod
    def _entry_text(entry: str) -> str:
        """Extract text content from entry (strip timestamp comment)."""
        import re
        return re.sub(r'<!--.*?-->\s*', '', entry).strip()

    @staticmethod
    def _text_hash(text: str) -> str:
        """SHA256 hash of normalized text."""
        import hashlib
        normalized = ' '.join(text.lower().split())
        return hashlib.sha256(normalized.encode('utf-8')).hexdigest()

    @staticmethod
    def _similarity(a: str, b: str) -> float:
        """Fast trigram-based similarity (Jaccard index on character trigrams)."""
        if not a or not b:
            return 0.0
        a_norm = ' '.join(a.lower().split())
        b_norm = ' '.join(b.lower().split())
        if a_norm == b_norm:
            return 1.0
        a_trigrams = {a_norm[i:i+3] for i in range(max(len(a_norm) - 2, 1))}
        b_trigrams = {b_norm[i:i+3] for i in range(max(len(b_norm) - 2, 1))}
        if not a_trigrams or not b_trigrams:
            return 0.0
        intersection = len(a_trigrams & b_trigrams)
        union = len(a_trigrams | b_trigrams)
        return intersection / union if union else 0.0

    def _check_duplicate(self, new_content: str, existing_text: str) -> str | None:
        """Check if new_content is duplicate of any existing entry.

        Returns rejection reason or None if OK.
        """
        new_text = new_content.strip()
        new_hash = self._text_hash(new_text)
        entries = self._parse_entries(existing_text)

        for entry in entries:
            entry_text = self._entry_text(entry)
            if not entry_text:
                continue

            # Exact hash match
            if self._text_hash(entry_text) == new_hash:
                return "Exact duplicate — this entry already exists in memory."

            # Near-duplicate similarity check
            sim = self._similarity(new_text, entry_text)
            if sim >= self.SIMILARITY_THRESHOLD:
                preview = entry_text[:80] + ("..." if len(entry_text) > 80 else "")
                return f"Near-duplicate (similarity {sim:.0%}) of existing entry: \"{preview}\""

        return None

    def _enforce_size_cap(self, text: str) -> str:
        """Remove oldest entries until text is under MAX_MEMORY_CHARS."""
        if len(text) <= self.MAX_MEMORY_CHARS:
            return text

        entries = self._parse_entries(text)
        # Remove from the front (oldest) until under limit
        while entries and sum(len(e) for e in entries) > self.MAX_MEMORY_CHARS:
            entries.pop(0)

        return "\n\n".join(entries)

    async def execute(self, content: str, **kwargs: Any) -> str:
        try:
            violation = scan_content(content)
            if violation:
                return f"Error: {violation}"

            memory_dir = self.workspace / "memory"
            memory_dir.mkdir(parents=True, exist_ok=True)
            memory_file = memory_dir / "MEMORY.md"

            # Read existing memory for duplicate check
            existing = ""
            if memory_file.exists():
                existing = memory_file.read_text(encoding="utf-8")

            # Duplicate check (exact hash + near-duplicate similarity)
            dup_reason = self._check_duplicate(content, existing)
            if dup_reason:
                return f"Rejected: {dup_reason}"

            # Append new entry
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
            note = f"\n\n<!-- {timestamp} -->\n{content.strip()}"
            new_text = existing + note

            # Enforce size cap (evict oldest if over limit)
            new_text = self._enforce_size_cap(new_text)

            # Atomic write (not append, because size cap may have removed entries)
            memory_file.write_text(new_text, encoding="utf-8")

            return f"Appended to MEMORY.md ({len(content)} chars)"
        except Exception as e:
            return f"Error writing memory: {str(e)}"
