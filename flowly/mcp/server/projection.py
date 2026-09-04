"""Public conversation projection shared by history, attachments and events.

Archive identities survive compaction and are the only message handles exposed
to MCP clients. Internal rows and local file paths never enter the projection.
"""

from __future__ import annotations

import hashlib
import json
import math
import mimetypes
from pathlib import PurePath
from typing import Any
from urllib.parse import urlsplit

from flowly.session.archive import snapshot_from_rows

CONTENT_LIMIT = 4000
ATTACHMENT_LIMIT = 100


def attachments(message: dict[str, Any]) -> list[dict[str, Any]]:
    """Describe stored attachments without opening paths or returning secrets."""
    candidates: list[dict[str, Any]] = []
    for field in ("media", "media_assets", "attachments"):
        values = message.get(field)
        if isinstance(values, list):
            for value in values[:ATTACHMENT_LIMIT]:
                if isinstance(value, str):
                    candidates.append({"path": value})
                elif isinstance(value, dict):
                    candidates.append(value)
    content = message.get("content")
    if isinstance(content, list):
        for block in content[:ATTACHMENT_LIMIT]:
            if isinstance(block, dict) and block.get("type") in {
                "image", "image_url", "audio", "input_audio", "file", "video",
                "resource", "resource_link",
            }:
                candidates.append(block)

    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for candidate in candidates:
        path = candidate.get("path")
        identity = str(path) if isinstance(path, str) else json.dumps(
            candidate, sort_keys=True, default=str,
        )
        digest = hashlib.sha256(identity.encode()).hexdigest()[:24]
        if digest in seen:
            continue
        seen.add(digest)
        name = candidate.get("file_name") or candidate.get("fileName") or candidate.get("name")
        if not isinstance(name, str) or not name:
            try:
                name = PurePath(urlsplit(path).path).name if isinstance(path, str) else "attachment"
            except ValueError:
                name = "attachment"
        # Even a supplied name may be a path. Publish just the basename.
        name = PurePath(name).name[:256]
        mime = candidate.get("mime_type") or candidate.get("mimeType")
        if not isinstance(mime, str) or not mime:
            mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
        item: dict[str, Any] = {
            "id": f"att_{digest}",
            "fileName": name,
            "mimeType": mime[:128],
            "type": str(candidate.get("kind") or candidate.get("type") or "file")[:32],
        }
        for field in ("size", "width", "height", "duration_ms"):
            value = candidate.get(field)
            if (isinstance(value, (int, float)) and not isinstance(value, bool)
                    and math.isfinite(value) and value >= 0):
                item[field] = value
        result.append(item)
        if len(result) >= ATTACHMENT_LIMIT:
            break
    return result


def messages_from_rows(
    rows: list[dict[str, Any]], *, content_limit: int | None = CONTENT_LIMIT,
) -> list[dict[str, Any]]:
    result = []
    for event in snapshot_from_rows(rows).events:
        message = event.message
        role = message.get("role")
        if (
            event.state not in {"active", "compacted"}
            or message.get("_display_hidden")
            or message.get("_archive_summary")
            or role not in {"user", "assistant"}
        ):
            continue
        content = message.get("content")
        if isinstance(content, list):
            content = "\n".join(
                block if isinstance(block, str) else block["text"] for block in content
                if isinstance(block, str) or (
                    isinstance(block, dict) and isinstance(block.get("text"), str)
                    and block.get("type") == "text"
                )
            )
        text = content if isinstance(content, str) else ""
        media = attachments(message)
        if not text and not media:
            continue
        item = {
            "message_id": event.event_id,
            "eventId": event.event_id,
            "index": len(result),
            "role": role,
            "content": text[:content_limit],
            "timestamp": message.get("timestamp", ""),
        }
        if media:
            item["attachments"] = media
        if content_limit is not None and len(text) > content_limit:
            item.update(contentTruncated=True, originalChars=len(text))
        result.append(item)
    return result
