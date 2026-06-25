"""Agent-facing Obsidian tools.

All tools operate strictly inside the configured vault root via
:func:`flowly.obsidian.vault.safe_resolve`. Paths are vault-relative; absolute
paths, ``..`` traversal and symlink escapes are rejected. Read/search results
are untrusted vault content — the agent loop wraps them with the external
content guard (see ``_sanitize_tool_result``).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from flowly.agent.tools.base import Tool
from flowly.obsidian.index import ObsidianIndex
from flowly.obsidian.vault import (
    VaultError,
    VaultNotConfigured,
    VaultPermissionDenied,
    iter_notes,
    read_note,
    resolve_vault_path,
    safe_resolve,
)

logger = logging.getLogger(__name__)


class ObsidianRuntime:
    """Shared, lazily-initialised vault state for the Obsidian tools.

    Resolution is deferred so that a misconfigured vault yields a clean
    per-call error instead of crashing tool registration at boot.
    """

    def __init__(self, cfg: Any, state_dir: Path) -> None:
        self._cfg = cfg
        self._state_dir = Path(state_dir)
        self._root: Path | None = None
        self._index: ObsidianIndex | None = None

    @property
    def include_globs(self) -> list[str]:
        return list(getattr(self._cfg, "include_globs", None) or ["**/*.md"])

    @property
    def exclude_globs(self) -> list[str]:
        return list(getattr(self._cfg, "exclude_globs", None) or [])

    @property
    def max_note_bytes(self) -> int:
        return int(getattr(self._cfg, "max_note_bytes", 1_000_000))

    def root(self) -> Path:
        """Resolve (and cache) the vault root. Raises VaultNotConfigured."""
        if self._root is None:
            self._root = resolve_vault_path(getattr(self._cfg, "vault_path", "") or "")
        return self._root

    def index(self) -> ObsidianIndex:
        if self._index is None:
            self._index = ObsidianIndex(
                self._state_dir / "obsidian_index.sqlite",
                self.root(),
                include_globs=self.include_globs,
                exclude_globs=self.exclude_globs,
                max_note_bytes=self.max_note_bytes,
            )
        return self._index


def _err(msg: str, *, code: str = "error") -> str:
    return json.dumps({"ok": False, "error": code, "detail": msg})


class _ObsidianTool(Tool):
    """Common base holding the shared runtime."""

    def __init__(self, rt: ObsidianRuntime) -> None:
        self._rt = rt
        self._last_err: tuple[str, str] = ("not_configured", "Obsidian vault is not configured or not found.")

    def _guard_root(self) -> Path | None:
        try:
            return self._rt.root()
        except VaultPermissionDenied as exc:
            self._last_err = ("permission_denied", str(exc))
            return None
        except VaultNotConfigured as exc:
            self._last_err = ("not_configured", str(exc))
            return None
        except Exception as exc:  # noqa: BLE001
            logger.debug("[obsidian] root resolution failed: %s", exc)
            self._last_err = ("error", f"{type(exc).__name__}: {exc}")
            return None

    def _not_ready(self) -> str:
        return _err(self._last_err[1], code=self._last_err[0])


class ObsidianSearchTool(_ObsidianTool):
    @property
    def name(self) -> str:
        return "obsidian_search"

    @property
    def description(self) -> str:
        return (
            "Search the user's Obsidian vault (their personal Markdown notes) and "
            "return ranked snippets with file paths and line ranges for citation. "
            "Use when the user asks about their notes/vault/journal or about people, "
            "projects or facts they may have written down."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Free-text search query."},
                "max_results": {
                    "type": "integer",
                    "description": "Max snippets to return (default 6).",
                    "default": 6,
                },
            },
            "required": ["query"],
        }

    async def execute(self, **kwargs: Any) -> str:
        if self._guard_root() is None:
            return self._not_ready()
        query = (kwargs.get("query") or "").strip()
        if not query:
            return _err("query is required")
        try:
            n = int(kwargs.get("max_results") or 6)
        except (TypeError, ValueError):
            n = 6
        n = max(1, min(n, 20))
        results = self._rt.index().search(query, max_results=n)
        return json.dumps({"ok": True, "query": query, "results": results})


class ObsidianReadTool(_ObsidianTool):
    @property
    def name(self) -> str:
        return "obsidian_read"

    @property
    def description(self) -> str:
        return (
            "Read a note from the Obsidian vault by its vault-relative path "
            "(e.g. 'People/Ada.md'). Optionally read a line range."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Vault-relative path, e.g. 'People/Ada.md'."},
                "from_line": {"type": "integer", "description": "1-based start line (default 1).", "default": 1},
                "lines": {"type": "integer", "description": "Number of lines to read (default 200).", "default": 200},
            },
            "required": ["path"],
        }

    async def execute(self, **kwargs: Any) -> str:
        root = self._guard_root()
        if root is None:
            return self._not_ready()
        rel = (kwargs.get("path") or "").strip()
        try:
            text = read_note(root, rel, max_note_bytes=self._rt.max_note_bytes)
        except VaultError as exc:
            return _err(str(exc))
        try:
            frm = max(1, int(kwargs.get("from_line") or 1))
            count = max(1, int(kwargs.get("lines") or 200))
        except (TypeError, ValueError):
            frm, count = 1, 200
        all_lines = text.splitlines()
        chunk = all_lines[frm - 1: frm - 1 + count]
        return json.dumps({
            "ok": True,
            "path": rel,
            "from_line": frm,
            "to_line": min(len(all_lines), frm - 1 + count),
            "total_lines": len(all_lines),
            "content": "\n".join(chunk),
        })


class ObsidianListTool(_ObsidianTool):
    @property
    def name(self) -> str:
        return "obsidian_list"

    @property
    def description(self) -> str:
        return "List notes in the Obsidian vault, optionally under a subfolder."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "folder": {"type": "string", "description": "Vault-relative subfolder (default: whole vault).", "default": ""},
                "max_results": {"type": "integer", "description": "Max paths to return (default 200).", "default": 200},
            },
        }

    async def execute(self, **kwargs: Any) -> str:
        root = self._guard_root()
        if root is None:
            return self._not_ready()
        folder = (kwargs.get("folder") or "").strip().strip("/")
        if folder:
            try:
                safe_resolve(root, folder)  # validate it doesn't escape
            except VaultError as exc:
                return _err(str(exc))
        try:
            cap = max(1, min(int(kwargs.get("max_results") or 200), 2000))
        except (TypeError, ValueError):
            cap = 200
        prefix = (folder + "/") if folder else ""
        out: list[str] = []
        for note in iter_notes(
            root,
            include_globs=self._rt.include_globs,
            exclude_globs=self._rt.exclude_globs,
            max_note_bytes=self._rt.max_note_bytes,
        ):
            if prefix and not note.rel_path.startswith(prefix):
                continue
            out.append(note.rel_path)
            if len(out) >= cap:
                break
        out.sort()
        return json.dumps({"ok": True, "folder": folder, "count": len(out), "notes": out})


class ObsidianWriteTool(_ObsidianTool):
    @property
    def name(self) -> str:
        return "obsidian_write"

    @property
    def description(self) -> str:
        return (
            "Create or overwrite a note in the Obsidian vault. Writes are confined "
            "to the vault root. Use if_exists='error' (default) to avoid clobbering."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Vault-relative path, e.g. 'Inbox/Note.md'."},
                "content": {"type": "string", "description": "Full Markdown content."},
                "if_exists": {
                    "type": "string",
                    "enum": ["error", "overwrite"],
                    "description": "What to do if the note exists (default 'error').",
                    "default": "error",
                },
            },
            "required": ["path", "content"],
        }

    async def execute(self, **kwargs: Any) -> str:
        root = self._guard_root()
        if root is None:
            return self._not_ready()
        rel = (kwargs.get("path") or "").strip()
        content = kwargs.get("content")
        if content is None:
            return _err("content is required")
        if_exists = (kwargs.get("if_exists") or "error").strip()
        try:
            abs_path = safe_resolve(root, rel)
        except VaultError as exc:
            return _err(str(exc))
        if not rel.lower().endswith(".md"):
            return _err("only .md notes can be written")
        if abs_path.exists() and if_exists != "overwrite":
            return _err(f"note already exists: {rel} (pass if_exists='overwrite')", code="exists")
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        abs_path.write_text(str(content), encoding="utf-8")
        return json.dumps({"ok": True, "path": rel, "bytes": len(str(content).encode("utf-8"))})


class ObsidianAppendTool(_ObsidianTool):
    @property
    def name(self) -> str:
        return "obsidian_append"

    @property
    def description(self) -> str:
        return "Append text to an existing (or new) note in the Obsidian vault."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Vault-relative path, e.g. 'Daily/2026-06-16.md'."},
                "content": {"type": "string", "description": "Markdown text to append."},
            },
            "required": ["path", "content"],
        }

    async def execute(self, **kwargs: Any) -> str:
        root = self._guard_root()
        if root is None:
            return self._not_ready()
        rel = (kwargs.get("path") or "").strip()
        content = kwargs.get("content")
        if content is None:
            return _err("content is required")
        if not rel.lower().endswith(".md"):
            return _err("only .md notes can be appended to")
        try:
            abs_path = safe_resolve(root, rel)
        except VaultError as exc:
            return _err(str(exc))
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        existing = abs_path.read_text(encoding="utf-8", errors="replace") if abs_path.exists() else ""
        sep = "" if (not existing or existing.endswith("\n")) else "\n"
        with abs_path.open("a", encoding="utf-8") as fh:
            fh.write(sep + str(content))
        return json.dumps({"ok": True, "path": rel, "appended_bytes": len(str(content).encode("utf-8"))})


def build_obsidian_tools(cfg: Any, state_dir: Path, *, facade: Any = None) -> list[Tool]:
    """Construct the Obsidian tool set sharing one runtime.

    When *facade* (a MemoryGovernance coordinator) is supplied, the
    review-gated ``obsidian_ingest`` tool is included too. It is imported
    lazily to avoid a circular import (ingest depends on this module).
    """
    rt = ObsidianRuntime(cfg, state_dir)
    tools: list[Tool] = [
        ObsidianSearchTool(rt),
        ObsidianReadTool(rt),
        ObsidianListTool(rt),
        ObsidianWriteTool(rt),
        ObsidianAppendTool(rt),
    ]
    if facade is not None:
        from flowly.obsidian.ingest import ObsidianIngestTool
        policy = getattr(cfg, "ingestion_policy", "review_gated")
        tools.append(ObsidianIngestTool(rt, facade, policy=policy))
    return tools
