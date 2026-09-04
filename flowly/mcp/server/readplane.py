"""Read-plane: standalone readers over Flowly's session + channel state.

These functions open the JSONL sessions, the SQLite FTS index, and the
config directly — no running gateway required. They return plain
JSON-serializable dicts so the MCP server layer (and tests) can use them
without any MCP dependency.

Session keys are ``channel:chat_id`` (e.g. ``telegram:123``). "platform"
in the tool API means the channel prefix.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import threading
from functools import lru_cache
from typing import Any


logger = logging.getLogger(__name__)

# Per-message content cap so a serve client can't pull unbounded text.
_CONTENT_CAP = 4000
_MAX_CONVERSATION_LIMIT = 200
_MAX_MESSAGE_LIMIT = 200
_MAX_SEARCH_LIMIT = 100
_MAX_CONVERSATION_SCAN = 10_000
_MAX_CURSOR_CHARS = 1024
_MAX_QUERY_CHARS = 1000
_MAX_SESSION_KEY_CHARS = 512


def _bounded_limit(value: Any, *, default: int, maximum: int) -> tuple[int | None, str | None]:
    if value is None:
        value = default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None, "limit must be an integer"
    if parsed < 1:
        return None, "limit must be at least 1"
    return min(parsed, maximum), None


def _cursor_scope(kind: str, **filters: Any) -> str:
    canonical = json.dumps(
        {"kind": kind, **filters},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def _encode_cursor(offset: int, scope: str) -> str:
    payload = json.dumps(
        {"v": 1, "offset": offset, "scope": scope},
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_cursor(cursor: str | None, scope: str) -> tuple[int | None, str | None]:
    if not cursor:
        return 0, None
    if not isinstance(cursor, str) or len(cursor) > _MAX_CURSOR_CHARS:
        return None, "cursor is invalid"
    try:
        padding = "=" * (-len(cursor) % 4)
        raw = base64.b64decode(cursor + padding, altchars=b"-_", validate=True)
        data = json.loads(raw)
        offset = int(data["offset"])
        if data.get("v") != 1 or data.get("scope") != scope or offset < 0:
            raise ValueError
    except (KeyError, TypeError, ValueError, UnicodeError, json.JSONDecodeError):
        return None, "cursor is invalid or belongs to a different query"
    return offset, None


def _pagination(*, offset: int, count: int, total: int, limit: int, scope: str) -> dict:
    next_offset = offset + count
    has_more = next_offset < total
    result: dict[str, Any] = {
        "limit": limit,
        "offset": offset,
        "hasMore": has_more,
    }
    if has_more:
        result["nextCursor"] = _encode_cursor(next_offset, scope)
    return result


def _valid_session_key(session_key: Any) -> str | None:
    if not isinstance(session_key, str):
        return None
    value = session_key.strip()
    if not value or len(value) > _MAX_SESSION_KEY_CHARS or "\x00" in value:
        return None
    return value


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
        # MCPServer executes synchronous tools in a worker pool. The SQLite
        # connection therefore permits cross-thread use, while this lock keeps
        # each compound read/rebuild operation serialized.
        self._lock = threading.RLock()

    def _ensure(self) -> None:
        with self._lock:
            if self._manager is not None:
                return
            from flowly.profile import get_flowly_home
            from flowly.session.indexer import SessionIndexer
            from flowly.session.manager import SessionManager

            # The manager's sessions_dir derives from $FLOWLY_HOME; workspace
            # is only used for non-session paths here, so the home is enough.
            home = get_flowly_home()
            self._manager = SessionManager(workspace=home)
            try:
                indexer = SessionIndexer(check_same_thread=False)
                indexer.rebuild_from_sessions_dir(self._manager.sessions_dir)
                self._manager._indexer = indexer
                self._indexer = indexer
            except Exception as exc:  # FTS optional — degrade to manager-only
                logger.debug("MCP serve: session indexer unavailable: %s", exc)
                self._indexer = None

    # -- tools ----------------------------------------------------------

    def conversations_list(
        self,
        platform: str | None = None,
        limit: int = 50,
        search: str | None = None,
        cursor: str | None = None,
    ) -> dict:
        bounded, error = _bounded_limit(
            limit, default=50, maximum=_MAX_CONVERSATION_LIMIT,
        )
        if error:
            return {"error": error}
        assert bounded is not None
        normalized_platform = (platform or "").strip().lower()
        normalized_search = (search or "").strip().lower()
        if len(normalized_search) > _MAX_QUERY_CHARS:
            return {"error": f"search must be at most {_MAX_QUERY_CHARS} characters"}
        scope = _cursor_scope(
            "conversations", platform=normalized_platform, search=normalized_search,
        )
        offset, error = _decode_cursor(cursor, scope)
        if error:
            return {"error": error}
        assert offset is not None

        with self._lock:
            self._ensure()
            rows: list[dict[str, Any]]
            if self._indexer is not None:
                rows = self._indexer.list_recent(limit=_MAX_CONVERSATION_SCAN)
            else:
                rows = self._manager.list_sessions()  # type: ignore[union-attr]

            out: list[dict[str, Any]] = []
            for row in rows:
                key = row.get("key", "")
                plat = _platform_of(key)
                if normalized_platform and plat.lower() != normalized_platform:
                    continue
                if normalized_search and normalized_search not in key.lower():
                    preview = str(row.get("preview", ""))
                    if normalized_search not in preview.lower():
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
        total = len(out)
        page = out[offset:offset + bounded]
        return {
            "count": len(page),
            "total": total,
            "conversations": page,
            "pagination": _pagination(
                offset=offset,
                count=len(page),
                total=total,
                limit=bounded,
                scope=scope,
            ),
        }

    def conversation_get(self, session_key: str) -> dict:
        key = _valid_session_key(session_key)
        if key is None:
            return {"error": "session_key is required and must be a valid string"}
        with self._lock:
            self._ensure()
            meta = None
            if self._indexer is not None:
                meta = self._indexer.get_session_meta(key)
            if meta is None:
                session = self._manager._load(key)  # type: ignore[union-attr]
                if session is None:
                    return {"error": f"Conversation not found: {key}"}
                return {
                    "session_key": key,
                    "platform": _platform_of(key),
                    "created_at": session.created_at.isoformat() if session.created_at else "",
                    "updated_at": session.updated_at.isoformat() if session.updated_at else "",
                    "msg_count": len(session.messages),
                }
            return {"session_key": key, "platform": _platform_of(key), **meta}

    def messages_read(
        self,
        session_key: str,
        limit: int = 50,
        cursor: str | None = None,
    ) -> dict:
        key = _valid_session_key(session_key)
        if key is None:
            return {"error": "session_key is required and must be a valid string"}
        bounded, error = _bounded_limit(limit, default=50, maximum=_MAX_MESSAGE_LIMIT)
        if error:
            return {"error": error}
        assert bounded is not None
        scope = _cursor_scope("messages", session_key=key)
        offset, error = _decode_cursor(cursor, scope)
        if error:
            return {"error": error}
        assert offset is not None

        with self._lock:
            self._ensure()
            session = self._manager._load(key)  # type: ignore[union-attr]
        if session is None:
            return {"error": f"Conversation not found: {key}"}

        rendered: list[dict[str, Any]] = []
        for idx, msg in enumerate(session.messages):
            role = msg.get("role", "")
            if role not in {"user", "assistant"}:
                continue
            text = _extract_text(msg.get("content"))
            if not text:
                continue
            entry = {
                "index": idx,
                "role": role,
                "content": text[:_CONTENT_CAP],
                "timestamp": msg.get("timestamp", ""),
            }
            if len(text) > _CONTENT_CAP:
                entry["contentTruncated"] = True
                entry["originalChars"] = len(text)
            if msg.get("event_id"):
                entry["eventId"] = msg["event_id"]
            rendered.append(entry)

        total = len(rendered)
        end = max(0, total - offset)
        start = max(0, end - bounded)
        page = rendered[start:end]
        next_offset = offset + len(page)
        has_more = start > 0
        pagination: dict[str, Any] = {
            "limit": bounded,
            "offsetFromNewest": offset,
            "hasMore": has_more,
            "direction": "older",
        }
        if has_more:
            pagination["nextCursor"] = _encode_cursor(next_offset, scope)
        return {
            "session_key": key,
            "count": len(page),
            "total": total,
            "messages": page,
            "pagination": pagination,
        }

    def messages_search(
        self,
        query: str,
        limit: int = 20,
        cursor: str | None = None,
    ) -> dict:
        if not isinstance(query, str) or not query.strip():
            return {"error": "query is required"}
        normalized_query = query.strip()
        if len(normalized_query) > _MAX_QUERY_CHARS:
            return {"error": f"query must be at most {_MAX_QUERY_CHARS} characters"}
        bounded, error = _bounded_limit(limit, default=20, maximum=_MAX_SEARCH_LIMIT)
        if error:
            return {"error": error}
        assert bounded is not None
        scope = _cursor_scope("search", query=normalized_query)
        offset, error = _decode_cursor(cursor, scope)
        if error:
            return {"error": error}
        assert offset is not None

        with self._lock:
            self._ensure()
            if self._indexer is None:
                return {"error": "Full-text search unavailable (indexer not initialized)"}
            hits = self._indexer.search(normalized_query, limit=offset + bounded + 1)
        selected = hits[offset:offset + bounded]
        results = []
        for hit in selected:
            key = str(hit.get("session_key", ""))
            result = {
                "session_key": key,
                "platform": _platform_of(key),
                "snippet": hit.get("snippet", ""),
                "anchor_id": hit.get("anchor_id"),
            }
            for source, target in (
                ("event_id", "eventId"),
                ("role", "role"),
                ("timestamp", "timestamp"),
                ("context", "context"),
                ("bookend_start", "bookendStart"),
                ("bookend_end", "bookendEnd"),
            ):
                if hit.get(source) is not None:
                    result[target] = hit[source]
            results.append(result)
        has_more = len(hits) > offset + len(selected)
        pagination: dict[str, Any] = {
            "limit": bounded,
            "offset": offset,
            "hasMore": has_more,
        }
        if has_more:
            pagination["nextCursor"] = _encode_cursor(offset + len(selected), scope)
        return {
            "count": len(results),
            "query": normalized_query,
            "results": results,
            "pagination": pagination,
        }


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
