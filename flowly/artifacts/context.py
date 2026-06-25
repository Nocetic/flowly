"""Helpers for persisted context outputs."""

from __future__ import annotations

from typing import Any


PERSISTED_OUTPUT_TAG = "<persisted-output>"
PERSISTED_OUTPUT_CLOSING_TAG = "</persisted-output>"
INTERNAL_CONTEXT_TAGS = {"internal:context", "context:persisted"}
DEFAULT_PREVIEW_CHARS = 1_500
DEFAULT_GET_LIMIT_CHARS = 6_000


def is_internal_context_artifact(artifact: dict[str, Any]) -> bool:
    """Return True when an artifact is storage for context management."""
    metadata = artifact.get("metadata") or {}
    tags = set(artifact.get("tags") or [])
    return bool(
        metadata.get("flowly_internal")
        or metadata.get("hidden")
        or metadata.get("internal")
        or metadata.get("context_persisted")
        or metadata.get("visibility") == "internal"
        or (tags & INTERNAL_CONTEXT_TAGS)
    )


def generate_preview(content: str, max_chars: int = DEFAULT_PREVIEW_CHARS) -> tuple[str, bool]:
    """Return a newline-aware preview and whether content was truncated."""
    if len(content) <= max_chars:
        return content, False
    truncated = content[:max_chars]
    last_nl = truncated.rfind("\n")
    if last_nl > max_chars // 2:
        truncated = truncated[:last_nl + 1]
    return truncated, True


def build_persisted_output_message(
    *,
    artifact_id: str,
    original_size: int,
    preview: str,
    has_more: bool,
    source: str = "subagent result",
) -> str:
    """Build the compact block shown to the parent agent."""
    size_kb = original_size / 1024
    if size_kb >= 1024:
        size_str = f"{size_kb / 1024:.1f} MB"
    else:
        size_str = f"{size_kb:.1f} KB"

    msg = f"{PERSISTED_OUTPUT_TAG}\n"
    msg += f"This {source} was too large ({original_size:,} characters, {size_str}).\n"
    msg += f"Full output saved as internal artifact: {artifact_id}\n"
    msg += (
        "Use artifact(action='get', artifact_id='"
        f"{artifact_id}', offset=0, limit={DEFAULT_GET_LIMIT_CHARS}) "
        "to access specific sections.\n"
        "Use artifact(action='promote', artifact_id='"
        f"{artifact_id}') only if the user asks to save/show it as an artifact.\n\n"
    )
    msg += f"Preview (first {len(preview)} chars):\n"
    msg += preview
    if has_more:
        msg += "\n..."
    msg += f"\n{PERSISTED_OUTPUT_CLOSING_TAG}"
    return msg


def internal_context_metadata(
    *,
    source: str,
    original_chars: int,
    run_id: str | None = None,
    label: str | None = None,
    task: str | None = None,
) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "flowly_internal": True,
        "visibility": "internal",
        "context_persisted": True,
        "internal_reason": "context_window_protection",
        "source": source,
        "original_chars": original_chars,
    }
    if run_id:
        metadata["run_id"] = run_id
    if label:
        metadata["label"] = label
    if task:
        metadata["task"] = task[:500]
    return metadata
