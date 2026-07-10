"""Small, transport-friendly artifact records for lists and selectors."""

from __future__ import annotations

from typing import Any


def artifact_summary(artifact: dict[str, Any]) -> dict[str, Any]:
    """Return artifact metadata without the potentially large content body."""
    summary = {
        "id": artifact["id"],
        "type": artifact.get("type"),
        "title": artifact.get("title"),
        "version": artifact.get("version"),
        "pinned": artifact.get("pinned"),
        "dashboard_size": artifact.get("dashboard_size"),
        "tags": artifact.get("tags", []),
        "session_key": artifact.get("session_key"),
        "created_at": artifact.get("created_at"),
        "updated_at": artifact.get("updated_at"),
    }
    metadata = artifact.get("metadata") or {}
    if metadata:
        summary["metadata"] = metadata
    return summary
