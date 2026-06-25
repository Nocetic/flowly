"""Session search tool — three calling modes inferred from args.

One tool, three shapes, zero LLM cost. Rather than expose three separate
tools (and burn schema budget), the mode is inferred from which arguments
the agent supplies:

  1. DISCOVER — pass ``query``. FTS5 across all past sessions, dedupes
     hits by session, returns top N with snippet, ±3 context messages,
     plus bookends (first 3 + last 3 user/assistant messages of each
     session) so the agent can judge relevance without follow-ups.
     Each hit carries an ``anchor_id`` for the scroll shape below.

  2. SCROLL — pass ``target_session`` + ``around_message_id``. Returns a
     window of ±``window`` messages centered on the anchor. To scroll
     forward / backward, re-anchor on the last / first message id of
     the returned window. Refuses to scroll inside the current session
     (those messages are already in context).

  3. BROWSE — no args. Returns recent sessions chronologically (titles,
     previews, timestamps).

The runtime injects the active conversation's id as ``session_key``
kwarg — that name is reserved, which is why the scroll-mode parameter
is ``target_session`` to avoid collision.

All three operate on the SQLite FTS5 index in
``flowly/session/indexer.py``. No LLM round-trips.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from flowly.agent.tools.base import Tool


def _format_ts(ts: float | None) -> str:
    """Convert Unix timestamp to readable string."""
    if not ts:
        return ""
    try:
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
    except (ValueError, OSError):
        return ""


def _shape_msg(m: dict[str, Any], anchor_id: int | None = None) -> dict[str, Any]:
    """Slim a message row for the JSON payload."""
    entry = {
        "id": m.get("id"),
        "role": m.get("role"),
        "content": m.get("content"),
    }
    if m.get("timestamp"):
        entry["timestamp"] = _format_ts(m["timestamp"])
    if anchor_id is not None and m.get("id") == anchor_id:
        entry["anchor"] = True
    return entry


class SessionSearchTool(Tool):
    """Search past conversations via FTS5 — discover / scroll / browse."""

    def __init__(self, indexer: Any):  # SessionIndexer
        self._indexer = indexer

    @property
    def name(self) -> str:
        return "session_search"

    @property
    def description(self) -> str:
        return (
            "Search past conversations and drill into them without any LLM cost. "
            "Three modes, inferred from args: "
            "(1) pass `query` to keyword-search across all sessions — returns "
            "snippets, context, and the session's opening/closing messages so "
            "you can judge relevance instantly; "
            "(2) pass `session_key` + `around_message_id` to scroll into a hit "
            "and read the surrounding window — re-anchor to scroll further; "
            "(3) pass nothing to browse recent sessions chronologically. "
            "Use PROACTIVELY when the user references past work ('we did this "
            "before', 'remember when', 'last time', a name/topic/file from a "
            "previous session)."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "Search keywords. Use OR for broad recall "
                        "('docker OR kubernetes'), quotes for exact phrases "
                        "('\"docker build\"'). Omit for browse or scroll mode."
                    ),
                },
                "target_session": {
                    "type": "string",
                    "description": (
                        "Scroll mode: the session to read from. Pair with "
                        "`around_message_id`. Get this from a previous "
                        "discover-mode result (the `session_key` field)."
                    ),
                },
                "around_message_id": {
                    "type": "integer",
                    "description": (
                        "Scroll mode: the anchor message id. Returns ±`window` "
                        "messages centered on this id. Use `anchor_id` from "
                        "a discover result, or the first/last id of a prior "
                        "scroll window to paginate."
                    ),
                },
                "window": {
                    "type": "integer",
                    "description": (
                        "Scroll mode: messages on each side of the anchor. "
                        "Default 5, max 20."
                    ),
                    "default": 5,
                },
                "limit": {
                    "type": "integer",
                    "description": "Discover / browse mode: max results (default 5, max 10).",
                    "default": 5,
                },
            },
            "required": [],
        }

    async def execute(
        self,
        query: str = "",
        target_session: str = "",
        around_message_id: int | None = None,
        window: int = 5,
        limit: int = 5,
        **kwargs: Any,
    ) -> str:
        # ``session_key`` is injected by the agent runtime as the active
        # conversation id — it is NOT a tool parameter (the scroll-mode
        # equivalent is ``target_session``). Keep them strictly separate
        # so the agent can't accidentally scroll into its own session.
        current_session = kwargs.get("session_key") or kwargs.get("session_key_current") or ""

        scroll_intent = (
            isinstance(target_session, str)
            and target_session.strip()
            and around_message_id is not None
        )

        try:
            if scroll_intent:
                return self._scroll(
                    target_session.strip(),
                    int(around_message_id),
                    window,
                    current_session,
                )
            if query and query.strip():
                return self._discover(query.strip(), min(max(limit, 1), 10), current_session)
            return self._browse(min(max(limit, 1), 10), current_session)
        except Exception as e:
            return json.dumps({"error": str(e), "results": []})

    # ── DISCOVER ─────────────────────────────────────────────────────

    def _discover(self, query: str, limit: int, exclude: str) -> str:
        results = self._indexer.search(
            query=query,
            limit=limit,
            exclude_session=exclude or None,
        )
        if not results:
            return json.dumps({
                "mode": "discover",
                "query": query,
                "results": [],
                "count": 0,
                "message": "No matching sessions found.",
            })

        items = []
        for r in results:
            item = {
                "session_key": r["session_key"],
                "anchor_id": r.get("anchor_id"),
                "date": _format_ts(r.get("session_created")),
                "role": r["role"],
                "snippet": r["snippet"],
                "context": r.get("context", []),
                "messages_in_session": r.get("msg_count", 0),
            }
            # Bookends — session's opening + closing turns. Saves the
            # agent a second tool call to fetch context.
            if r.get("bookend_start") or r.get("bookend_end"):
                item["bookend_start"] = r.get("bookend_start", [])
                item["bookend_end"] = r.get("bookend_end", [])
            items.append(item)

        return json.dumps({
            "mode": "discover",
            "query": query,
            "results": items,
            "count": len(items),
            "next_action": (
                "Scroll into a hit by calling with `session_key` + "
                "`around_message_id` (use the `anchor_id` from a result)."
            ),
        }, ensure_ascii=False)

    # ── SCROLL ───────────────────────────────────────────────────────

    def _scroll(
        self,
        session_key: str,
        anchor_id: int,
        window: int,
        current_session: str,
    ) -> str:
        # Reject scrolling inside the active session — those messages are
        # already in the agent's context.
        if current_session and session_key == current_session:
            return json.dumps({
                "error": "scroll rejected: anchor is in the current session — already in context",
                "mode": "scroll",
            })

        window = max(1, min(int(window), 20))

        meta = self._indexer.get_session_meta(session_key)
        if not meta:
            return json.dumps({
                "error": f"session_key not found: {session_key}",
                "mode": "scroll",
            })

        view = self._indexer.messages_around(session_key, anchor_id, window=window)
        messages = view.get("window") or []
        if not messages:
            return json.dumps({
                "error": f"around_message_id {anchor_id} not in session_key {session_key}",
                "mode": "scroll",
            })

        return json.dumps({
            "mode": "scroll",
            "session_key": session_key,
            "around_message_id": anchor_id,
            "window": window,
            "session_meta": {
                "created_at": _format_ts(meta.get("created_at")),
                "updated_at": _format_ts(meta.get("updated_at")),
                "msg_count": meta.get("msg_count", 0),
            },
            "messages": [_shape_msg(m, anchor_id=anchor_id) for m in messages],
            "messages_before": view.get("messages_before", 0),
            "messages_after": view.get("messages_after", 0),
            "next_action": (
                f"Scroll further: re-call with around_message_id="
                f"{messages[-1]['id']} (forward) or {messages[0]['id']} (back)."
            ),
        }, ensure_ascii=False)

    # ── BROWSE ───────────────────────────────────────────────────────

    def _browse(self, limit: int, exclude: str) -> str:
        results = self._indexer.list_recent(
            limit=limit,
            exclude_session=exclude or None,
        )
        items = []
        for r in results:
            items.append({
                "session_key": r["key"],
                "date": _format_ts(r.get("created_at")),
                "last_active": _format_ts(r.get("updated_at")),
                "messages": r.get("msg_count", 0),
                "preview": r.get("preview", ""),
            })
        return json.dumps({
            "mode": "browse",
            "results": items,
            "count": len(items),
            "next_action": "Pass a `query` to search, or `session_key` + `around_message_id` to scroll into one.",
        }, ensure_ascii=False)
