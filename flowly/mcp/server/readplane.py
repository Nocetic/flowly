"""Read-plane: standalone readers over Flowly's session + channel state.

These functions open the JSONL sessions, the SQLite FTS index, and the
config directly — no running gateway required. They return plain
JSON-serializable dicts so the FastMCP layer (and tests) can use them
without any MCP dependency.

Session keys are ``channel:chat_id`` (e.g. ``telegram:123``). "platform"
in the tool API means the channel prefix.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import Any


logger = logging.getLogger(__name__)

# Per-message content cap so a serve client can't pull unbounded text.
_CONTENT_CAP = 4000


def _platform_of(session_key: str) -> str:
    return session_key.split(":", 1)[0] if ":" in session_key else ""


def _extract_text(content: Any) -> str:
    """Flatten a message ``content`` (str or list-of-blocks) to text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text" and isinstance(block.get("text"), str):
                    parts.append(block["text"])
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(parts)
    return ""


class SessionReader:
    """Lazily builds a SessionManager + SessionIndexer for read access."""

    def __init__(self) -> None:
        self._manager: Any | None = None
        self._indexer: Any | None = None

    def _ensure(self) -> None:
        if self._manager is not None:
            return
        from flowly.profile import get_flowly_home
        from flowly.session.manager import SessionManager
        from flowly.session.indexer import SessionIndexer

        # The manager's sessions_dir derives from $FLOWLY_HOME; workspace
        # is only used for non-session paths here, so the home is enough.
        home = get_flowly_home()
        self._manager = SessionManager(workspace=home)
        try:
            indexer = SessionIndexer()
            indexer.rebuild_from_sessions_dir(self._manager.sessions_dir)
            self._manager._indexer = indexer
            self._indexer = indexer
        except Exception as exc:  # FTS optional — degrade to manager-only
            logger.debug("MCP serve: session indexer unavailable: %s", exc)
            self._indexer = None

    # -- tools ----------------------------------------------------------

    def conversations_list(
        self, platform: str | None = None, limit: int = 50, search: str | None = None,
    ) -> dict:
        self._ensure()
        rows: list[dict[str, Any]]
        if self._indexer is not None:
            rows = self._indexer.list_recent(limit=max(limit * 3, limit))
        else:
            rows = self._manager.list_sessions()  # type: ignore[union-attr]

        out: list[dict[str, Any]] = []
        for row in rows:
            key = row.get("key", "")
            plat = _platform_of(key)
            if platform and plat.lower() != platform.lower():
                continue
            if search and search.lower() not in key.lower():
                preview = str(row.get("preview", ""))
                if search.lower() not in preview.lower():
                    continue
            out.append({
                "session_key": key,
                "platform": plat,
                "updated_at": row.get("updated_at", ""),
                "created_at": row.get("created_at", ""),
                "msg_count": row.get("msg_count"),
                "preview": row.get("preview", ""),
            })
        out.sort(key=lambda c: c.get("updated_at", ""), reverse=True)
        out = out[:limit]
        return {"count": len(out), "conversations": out}

    def conversation_get(self, session_key: str) -> dict:
        self._ensure()
        meta = None
        if self._indexer is not None:
            meta = self._indexer.get_session_meta(session_key)
        if meta is None:
            session = self._manager._load(session_key)  # type: ignore[union-attr]
            if session is None:
                return {"error": f"Conversation not found: {session_key}"}
            return {
                "session_key": session_key,
                "platform": _platform_of(session_key),
                "created_at": session.created_at.isoformat() if session.created_at else "",
                "updated_at": session.updated_at.isoformat() if session.updated_at else "",
                "msg_count": len(session.messages),
            }
        return {"session_key": session_key, "platform": _platform_of(session_key), **meta}

    def messages_read(self, session_key: str, limit: int = 50) -> dict:
        self._ensure()
        session = self._manager._load(session_key)  # type: ignore[union-attr]
        if session is None:
            return {"error": f"Conversation not found: {session_key}"}

        rendered: list[dict[str, Any]] = []
        for idx, msg in enumerate(session.messages):
            role = msg.get("role", "")
            if role not in {"user", "assistant"}:
                continue
            text = _extract_text(msg.get("content"))
            if not text:
                continue
            rendered.append({
                "index": idx,
                "role": role,
                "content": text[:_CONTENT_CAP],
                "timestamp": msg.get("timestamp", ""),
            })

        total = len(rendered)
        rendered = rendered[-max(1, limit):]
        return {
            "session_key": session_key,
            "count": len(rendered),
            "total": total,
            "messages": rendered,
        }

    def messages_search(self, query: str, limit: int = 20) -> dict:
        self._ensure()
        if self._indexer is None:
            return {"error": "Full-text search unavailable (indexer not initialized)"}
        if not query or not query.strip():
            return {"error": "query is required"}
        hits = self._indexer.search(query, limit=limit)
        results = [
            {
                "session_key": h.get("session_key", ""),
                "platform": _platform_of(h.get("session_key", "")),
                "snippet": h.get("snippet", ""),
                "anchor_id": h.get("anchor_id"),
            }
            for h in hits
        ]
        return {"count": len(results), "query": query, "results": results}


@lru_cache(maxsize=1)
def get_session_reader() -> SessionReader:
    return SessionReader()


def channels_list(platform: str | None = None) -> dict:
    """Enumerate configured channels from config (no gateway needed)."""
    from flowly.config.loader import load_config

    config = load_config()
    channels = config.channels
    out: list[dict[str, Any]] = []
    for name in ("telegram", "discord", "slack", "whatsapp", "imessage", "web", "email", "teams"):
        cfg = getattr(channels, name, None)
        if cfg is None:
            continue
        if platform and name.lower() != platform.lower():
            continue
        out.append({"platform": name, "enabled": bool(getattr(cfg, "enabled", False))})
    return {"count": len(out), "channels": out}
