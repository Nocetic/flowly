"""``media_find`` — the pictures, clips and sounds this agent made or was sent.

The media library (flowly/media/library.py) keeps every generated file with
the prompt it came from, and every file the owner sent. Without a way to ask
it, an agent asked for "the image we made the other day" listed its media
folder and guessed, once pulling a frame from an unrelated video
(2026-11-01). This reads the library: newest first, by words of its prompt,
title or name, and answers with each file's path to give it in the chat.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from flowly.agent.tools.base import Tool

MAX_RESULTS = 20


class MediaFindTool(Tool):
    @property
    def name(self) -> str:
        return "media_find"

    @property
    def description(self) -> str:
        return (
            "Find pictures, videos and sounds you made (with the prompt they came from) or the person sent, "
            "newest first: by words of their prompt, title or file name, or just the latest. Use it when the "
            "person asks for something made or shared earlier, before guessing from folders. Each result has "
            "its path: give it in the chat with MEDIA:<path>."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "search": {"type": "string", "description": "Words of the prompt, title or file name (any language)."},
                "kind": {"type": "string", "enum": ["image", "video", "audio"], "description": "Only this kind."},
                "source": {"type": "string", "enum": ["generated", "received"],
                           "description": "Only what you made, or only what the person sent."},
                "limit": {"type": "integer", "minimum": 1, "maximum": MAX_RESULTS, "default": 8},
            },
        }

    async def execute(self, **kwargs: Any) -> str:
        import asyncio

        return await asyncio.to_thread(self._find, kwargs)

    def _find(self, kwargs: dict[str, Any]) -> str:
        from flowly.media.library import SOURCES, get_library
        from flowly.profile import get_flowly_home

        source = kwargs.get("source") or None
        if source is not None and source not in SOURCES:
            return "Error: source is generated or received."
        kind = kwargs.get("kind") or None
        if kind is not None and kind not in {"image", "video", "audio"}:
            return "Error: kind is image, video or audio."
        limit = max(1, min(int(kwargs.get("limit") or 8), MAX_RESULTS))
        library = get_library()
        try:
            library.reconcile(probe_new=False)  # files made since the library last looked
        except Exception:
            pass
        items, total = library.list(kind=kind, source=source, search=str(kwargs.get("search") or "").strip() or None,
                                    limit=limit)
        media_dir = get_flowly_home() / "media"
        found = []
        for item in items:
            entry = {
                "path": str(media_dir / item["mediaId"]),
                "kind": item["kind"],
                "fileName": item["fileName"],
                "source": item["source"],
                "made": datetime.fromtimestamp(item["createdAt"]).astimezone().isoformat(timespec="minutes")
                if item.get("createdAt") else None,
            }
            for key in ("prompt", "title", "model"):
                if item.get(key):
                    entry[key] = str(item[key])[:300]
            found.append(entry)
        return json.dumps({"found": found, "total": total}, ensure_ascii=False)
