"""Artifact management tool — create, update, list, and version interactive content."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable, Awaitable

from loguru import logger

from flowly.artifacts.context import (
    DEFAULT_GET_LIMIT_CHARS,
    INTERNAL_CONTEXT_TAGS,
    is_internal_context_artifact,
)
from flowly.agent.tools.base import Tool


# Filename extensions per artifact type, used by the export action so the
# LLM can hand us a directory and still get a sensible file on disk.
_EXPORT_EXTENSIONS: dict[str, str] = {
    "html": ".html",
    "svg": ".svg",
    "markdown": ".md",
    "csv": ".csv",
    "json": ".json",
    "code": ".txt",
    "mermaid": ".mmd",
    "latex": ".tex",
    "form": ".json",
    "chart": ".json",
}


def _slugify(text: str, max_len: int = 80) -> str:
    """Lower-cased, safe-on-disk filename stem."""
    text = text.strip().lower()
    text = re.sub(r"[^\w\s.-]+", "", text, flags=re.UNICODE)
    text = re.sub(r"[\s_]+", "-", text).strip("-.")
    return (text or "artifact")[:max_len]


def _export_path_allowed(target: Path) -> bool:
    """Restrict artifact export to the user's own folders.

    Mirrors the WriteFileTool sandbox but is intentionally narrower: an
    export is a user-facing deliverable, so we don't surface ``~/.flowly``
    or arbitrary workspace paths here. If the model needs to write inside
    a workspace it can keep using ``write_file``.
    """
    home = Path.home()
    allowed_roots = (
        home / "Downloads",
        home / "Desktop",
        home / "Documents",
    )
    for root in allowed_roots:
        try:
            target.resolve().relative_to(root.resolve())
            return True
        except (ValueError, OSError):
            continue
    return False


class ArtifactTool(Tool):
    """Action-based tool for managing versioned artifacts."""

    def __init__(
        self,
        store: Any,
        on_change: Callable[[str, dict], Awaitable[None]] | None = None,
    ):
        self._store = store
        self._on_change = on_change

    def set_on_change(self, callback: Callable[[str, dict], Awaitable[None]]) -> None:
        """Set the broadcast callback (wired by CLI after gateway creation)."""
        self._on_change = callback

    # ── Tool interface ────────────────────────────────────────────────────────

    @property
    def name(self) -> str:
        return "artifact"

    @property
    def description(self) -> str:
        return (
            "Create and manage versioned artifacts (HTML, SVG, Markdown, CSV, JSON, "
            "code, Mermaid diagrams, LaTeX, forms, charts).\n\n"
            "Actions:\n"
            "- create: Create a new artifact (type, title, content required)\n"
            "- update: Update existing artifact (artifact_id required; "
            "content change creates version snapshot)\n"
            "- get: Get a single artifact by ID\n"
            "- list: List artifacts with filters (type, pinned, search, limit)\n"
            "- export: Save an artifact to disk (artifact_id required, optional path). "
            "Streams content directly from storage to file — does NOT re-emit content "
            "through the model. ALWAYS prefer this over `get` + `write_file` when the "
            "user asks to copy / save / export / download an artifact: it is faster, "
            "cheaper, and preserves the original bytes exactly. Default destination is "
            "~/Downloads.\n"
            "- promote: Make an internal context artifact user-visible\n"
            "- delete: Delete an artifact permanently\n"
            "- pin: Pin/unpin artifact to dashboard\n"
            "- get_versions: Get version history for an artifact\n\n"
            "Artifact types: html, svg, markdown, csv, json, code, mermaid, latex, form, chart\n"
            "Artifacts persist across sessions and are served to client apps. "
            "Internal context artifacts are hidden from list by default but can "
            "be read by ID and promoted when the user asks to save/show them."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "description": "The action to perform",
                    "enum": [
                        "create", "update", "get", "list", "export",
                        "promote", "delete", "pin", "get_versions",
                    ],
                },
                "artifact_id": {
                    "type": "string",
                    "description": "Artifact ID (for get/update/delete/pin/get_versions)",
                },
                "type": {
                    "type": "string",
                    "description": "Artifact content type",
                    "enum": [
                        "html", "svg", "markdown", "csv", "json",
                        "code", "mermaid", "latex", "form", "chart",
                    ],
                },
                "title": {
                    "type": "string",
                    "description": "Artifact title (max 200 chars)",
                },
                "content": {
                    "type": "string",
                    "description": "Full renderable content (HTML, SVG, Markdown, etc.)",
                },
                "pinned": {
                    "type": "boolean",
                    "description": "Pin to dashboard (for create/update/pin)",
                },
                "dashboard_size": {
                    "type": "string",
                    "description": "Dashboard card size when pinned",
                    "enum": ["small", "medium", "large", "full"],
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Tags for categorization",
                },
                "language": {
                    "type": "string",
                    "description": "Programming language for code type (python, javascript, sql, etc.)",
                },
                "search": {
                    "type": "string",
                    "description": "Full-text search query (for list action)",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max results for list, or max content chars for get",
                },
                "offset": {
                    "type": "integer",
                    "description": "Content character offset for get",
                },
                "include_internal": {
                    "type": "boolean",
                    "description": "Include hidden internal context artifacts in list results",
                },
                "path": {
                    "type": "string",
                    "description": (
                        "Destination for the export action. Accepts a directory "
                        "(e.g. '~/Downloads') — a filename will be derived from the "
                        "artifact title and type — or a full file path. Restricted "
                        "to ~/Downloads, ~/Desktop, ~/Documents. Defaults to "
                        "~/Downloads when omitted."
                    ),
                },
                "overwrite": {
                    "type": "boolean",
                    "description": "Replace an existing file at the destination (default false: a numeric suffix is appended)",
                },
            },
            "required": ["action"],
        }

    async def execute(self, action: str = "", **kwargs: Any) -> str:
        handlers = {
            "create": self._create,
            "update": self._update,
            "get": self._get,
            "list": self._list,
            "export": self._export,
            "promote": self._promote,
            "delete": self._delete,
            "pin": self._pin,
            "get_versions": self._get_versions,
        }
        handler = handlers.get(action)
        if not handler:
            return json.dumps({"error": f"Unknown action: {action}. Valid: {list(handlers)}"})

        try:
            return await handler(**kwargs)
        except Exception as exc:
            logger.error("Artifact {} error: {}", action, exc)
            return json.dumps({"error": str(exc), "action": action})

    # ── Actions ───────────────────────────────────────────────────────────────

    async def _create(self, **kw: Any) -> str:
        art_type = kw.get("type", "")
        title = kw.get("title", "")
        content = kw.get("content", "")

        if not art_type:
            return json.dumps({"error": "type is required (html, svg, markdown, form, chart)"})
        if not title:
            return json.dumps({"error": "title is required"})
        if not content:
            return json.dumps({"error": "content is required"})

        # Build metadata from format-specific params
        metadata = kw.get("metadata") or {}
        if kw.get("language"):
            metadata["language"] = kw["language"]

        artifact = self._store.create(
            type=art_type,
            title=title[:200],
            content=content,
            metadata=metadata,
            data_bindings=kw.get("data_bindings"),
            pinned=kw.get("pinned", False),
            dashboard_size=kw.get("dashboard_size", "medium"),
            tags=kw.get("tags"),
            session_key=kw.get("session_key"),
        )

        await self._notify("artifact.created", artifact)

        return json.dumps({
            "action": "create",
            "artifact": _summarize(artifact),
            "message": f"Artifact '{title[:50]}' created (id: {artifact['id']})",
        })

    async def _update(self, **kw: Any) -> str:
        artifact_id = kw.get("artifact_id", "")
        if not artifact_id:
            return json.dumps({"error": "artifact_id is required"})

        old = self._store.get(artifact_id)
        if not old:
            return json.dumps({"error": f"Artifact not found: {artifact_id}"})

        metadata = kw.get("metadata")
        if kw.get("language"):
            metadata = metadata or {}
            metadata["language"] = kw["language"]

        updated = self._store.update(
            artifact_id,
            title=kw.get("title"),
            content=kw.get("content"),
            metadata=metadata,
            data_bindings=kw.get("data_bindings"),
            pinned=kw.get("pinned"),
            dashboard_size=kw.get("dashboard_size"),
            tags=kw.get("tags"),
        )

        version_created = updated and updated["version"] > old["version"]
        await self._notify("artifact.updated", updated or old)

        return json.dumps({
            "action": "update",
            "artifact": _summarize(updated or old),
            "version_created": version_created,
            "message": f"Artifact updated (v{updated['version'] if updated else old['version']})",
        })

    async def _get(self, **kw: Any) -> str:
        artifact_id = kw.get("artifact_id", "")
        if not artifact_id:
            return json.dumps({"error": "artifact_id is required"})

        artifact = self._store.get(artifact_id)
        if not artifact:
            return json.dumps({"error": f"Artifact not found: {artifact_id}"})

        content = artifact.get("content", "")
        offset = max(0, int(kw.get("offset", 0) or 0))
        limit_raw = kw.get("limit")
        limit = int(limit_raw) if limit_raw is not None else None
        if limit is None and is_internal_context_artifact(artifact):
            limit = DEFAULT_GET_LIMIT_CHARS
        if limit is not None:
            limit = max(1, min(limit, 50_000))
            end = min(len(content), offset + limit)
            sliced = content[offset:end]
            metadata = artifact.get("metadata") or {}
            source_tool = metadata.get("tool_name")
            if source_tool in ("web_fetch", "web_extract", "web_search", "browser_tab"):
                from flowly.agent.tools.content_guard import wrap_external_content
                sliced = wrap_external_content(sliced, source=str(source_tool))
            artifact = {
                **artifact,
                "content": sliced,
                "content_range": {
                    "offset": offset,
                    "limit": limit,
                    "end": end,
                    "total": len(content),
                    "has_more": end < len(content),
                },
            }

        return json.dumps({"action": "get", "artifact": artifact})

    async def _export(self, **kw: Any) -> str:
        """Save an artifact's content directly to disk without round-tripping it
        through the model. Use this whenever the user asks to copy / save /
        export / download an artifact — it is dramatically cheaper than
        ``get`` + ``write_file`` and guarantees the bytes are unchanged.
        """
        artifact_id = kw.get("artifact_id", "")
        if not artifact_id:
            return json.dumps({"error": "artifact_id is required"})

        artifact = self._store.get(artifact_id)
        if not artifact:
            return json.dumps({"error": f"Artifact not found: {artifact_id}"})

        content = artifact.get("content", "")
        art_type = (artifact.get("type") or "markdown").lower()
        title = artifact.get("title") or artifact_id

        raw_path = (kw.get("path") or "~/Downloads").strip()
        target = Path(raw_path).expanduser()

        # Resolve directory-vs-file: if the path doesn't have a recognisable
        # file suffix and either exists as a directory or ends with a
        # separator, treat it as a directory and synthesize a filename from
        # the artifact's title + type.
        ext = _EXPORT_EXTENSIONS.get(art_type, ".txt")
        looks_like_dir = (
            raw_path.endswith("/")
            or (target.exists() and target.is_dir())
            or target.suffix == ""
        )
        if looks_like_dir:
            stem = _slugify(title)
            target = target / f"{stem}{ext}"

        # Sandbox: keep artifact exports inside the user's own folders.
        if not _export_path_allowed(target):
            return json.dumps({
                "error": (
                    f"Export path not allowed: {target}. "
                    "Use a path under ~/Downloads, ~/Desktop, or ~/Documents."
                )
            })

        target.parent.mkdir(parents=True, exist_ok=True)

        # Avoid silent overwrite unless the caller asked for it.
        overwrite = bool(kw.get("overwrite", False))
        final = target
        if final.exists() and not overwrite:
            stem, suffix = final.stem, final.suffix
            n = 1
            while final.exists() and n < 1000:
                final = target.with_name(f"{stem}-{n}{suffix}")
                n += 1

        try:
            final.write_text(content, encoding="utf-8")
        except OSError as exc:
            return json.dumps({"error": f"Write failed: {exc}"})

        return json.dumps({
            "action": "export",
            "artifact_id": artifact_id,
            "path": str(final),
            "bytes": len(content.encode("utf-8")),
            "type": art_type,
            "message": f"Exported artifact to {final}",
        })

    async def _list(self, **kw: Any) -> str:
        include_internal = bool(kw.get("include_internal", False))
        limit = int(kw.get("limit", 50) or 50)
        fetch_limit = limit if include_internal else max(limit * 5, 100)
        results = self._store.list(
            type=kw.get("type"),
            pinned=kw.get("pinned"),
            search=kw.get("search"),
            tags=kw.get("tags"),
            limit=fetch_limit,
        )
        if not include_internal:
            results = [a for a in results if not is_internal_context_artifact(a)]
        results = results[:limit]

        # Return summaries (no full content) for list view
        summaries = [_summarize(a) for a in results]

        return json.dumps({
            "action": "list",
            "count": len(summaries),
            "artifacts": summaries,
        })

    async def _promote(self, **kw: Any) -> str:
        artifact_id = kw.get("artifact_id", "")
        if not artifact_id:
            return json.dumps({"error": "artifact_id is required"})

        old = self._store.get(artifact_id)
        if not old:
            return json.dumps({"error": f"Artifact not found: {artifact_id}"})

        metadata = dict(old.get("metadata") or {})
        for key in (
            "flowly_internal",
            "hidden",
            "internal",
            "context_persisted",
            "internal_reason",
        ):
            metadata.pop(key, None)
        metadata["visibility"] = "user"
        metadata["promoted_from_internal"] = True

        tags = [
            t for t in (old.get("tags") or [])
            if t not in INTERNAL_CONTEXT_TAGS
        ]
        if "promoted" not in tags:
            tags.append("promoted")

        updated = self._store.update(
            artifact_id,
            title=kw.get("title"),
            metadata=metadata,
            pinned=kw.get("pinned"),
            dashboard_size=kw.get("dashboard_size"),
            tags=tags,
        )
        await self._notify("artifact.updated", updated or old)

        return json.dumps({
            "action": "promote",
            "artifact": _summarize(updated or old),
            "message": f"Artifact {artifact_id} is now user-visible",
        })

    async def _delete(self, **kw: Any) -> str:
        artifact_id = kw.get("artifact_id", "")
        if not artifact_id:
            return json.dumps({"error": "artifact_id is required"})

        deleted = self._store.delete(artifact_id)
        if not deleted:
            return json.dumps({"error": f"Artifact not found: {artifact_id}"})

        await self._notify("artifact.deleted", {"id": artifact_id})

        return json.dumps({
            "action": "delete",
            "deleted": True,
            "message": f"Artifact {artifact_id} deleted",
        })

    async def _pin(self, **kw: Any) -> str:
        artifact_id = kw.get("artifact_id", "")
        if not artifact_id:
            return json.dumps({"error": "artifact_id is required"})

        pinned = kw.get("pinned", True)
        artifact = self._store.pin(artifact_id, pinned)
        if not artifact:
            return json.dumps({"error": f"Artifact not found: {artifact_id}"})

        await self._notify("artifact.updated", artifact)

        return json.dumps({
            "action": "pin",
            "artifact": _summarize(artifact),
            "message": f"Artifact {'pinned' if pinned else 'unpinned'}",
        })

    async def _get_versions(self, **kw: Any) -> str:
        artifact_id = kw.get("artifact_id", "")
        if not artifact_id:
            return json.dumps({"error": "artifact_id is required"})

        versions = self._store.get_versions(artifact_id)
        # Truncate content in version list for readability
        for v in versions:
            if len(v.get("content", "")) > 200:
                v["content"] = v["content"][:200] + "..."

        return json.dumps({
            "action": "get_versions",
            "artifact_id": artifact_id,
            "count": len(versions),
            "versions": versions,
        })

    # ── Helpers ───────────────────────────────────────────────────────────────

    async def _notify(self, event_name: str, data: dict) -> None:
        """Broadcast event to connected clients (if callback set)."""
        if self._on_change:
            try:
                await self._on_change(event_name, data)
            except Exception as exc:
                logger.debug("Artifact broadcast error: {}", exc)


def _summarize(artifact: dict) -> dict:
    """Return artifact summary without full content (for list views)."""
    summary = {
        "id": artifact["id"],
        "type": artifact.get("type"),
        "title": artifact.get("title"),
        "version": artifact.get("version"),
        "pinned": artifact.get("pinned"),
        "dashboard_size": artifact.get("dashboard_size"),
        "tags": artifact.get("tags", []),
        "created_at": artifact.get("created_at"),
        "updated_at": artifact.get("updated_at"),
    }
    metadata = artifact.get("metadata", {})
    if metadata:
        summary["metadata"] = metadata
    return summary
