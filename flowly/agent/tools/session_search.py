"""Session search tool — recall and exact retrieval over durable history.

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
     the returned window. The current session is intentionally allowed:
     compacted events are durable archive, not necessarily in the prompt.

  3. BROWSE — no args. Returns recent sessions chronologically (titles,
     previews, timestamps).

  4. EXACT — pass ``exact_mode`` (``first_user_message``, ``by_id``, or
     ``range``) and its stable event-id arguments. This bypasses fuzzy FTS so
     questions such as "what was my first message?" are deterministic.

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
        "event_id": m.get("event_id"),
        "role": m.get("role"),
        "content": m.get("content"),
        "state": m.get("state", "active"),
    }
    if m.get("timestamp"):
        entry["timestamp"] = _format_ts(m["timestamp"])
    if anchor_id is not None and m.get("id") == anchor_id:
        entry["anchor"] = True
    return entry


class SessionSearchTool(Tool):
    """Search durable conversations — discover / scroll / browse / exact."""

    def __init__(self, indexer: Any):  # SessionIndexer
        self._indexer = indexer

    @property
    def name(self) -> str:
        return "session_search"

    @property
    def description(self) -> str:
        return (
            "Search past conversations and drill into them without any LLM cost. "
            "Four modes, inferred from args: "
            "(1) pass `query` to keyword-search across all sessions — returns "
            "snippets, context, and the session's opening/closing messages so "
            "you can judge relevance instantly; "
            "(2) pass `session_key` + `around_message_id` to scroll into a hit "
            "and read the surrounding window — re-anchor to scroll further; "
            "(3) pass nothing to browse recent sessions chronologically; "
            "(4) pass `exact_mode` for deterministic first-message, stable-id, "
            "or event-range retrieval. The active conversation is searchable "
            "because compacted events may no longer be in the prompt. "
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
                "exact_mode": {
                    "type": "string",
                    "enum": ["first_user_message", "by_id", "range"],
                    "description": (
                        "Deterministic archive lookup. `first_user_message` "
                        "needs only target_session (or uses the current one); "
                        "`by_id` needs event_id; `range` needs start_event_id "
                        "and end_event_id."
                    ),
                },
                "event_id": {
                    "type": "string",
                    "description": "Exact by_id mode: stable event identity.",
                },
                "start_event_id": {
                    "type": "string",
                    "description": "Exact range mode: inclusive first event.",
                },
                "end_event_id": {
                    "type": "string",
                    "description": "Exact range mode: inclusive last event.",
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
        exact_mode: str = "",
        event_id: str = "",
        start_event_id: str = "",
        end_event_id: str = "",
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
            if exact_mode:
                return self._exact(
                    exact_mode.strip(),
                    target_session.strip() or current_session,
                    event_id.strip(),
                    start_event_id.strip(),
                    end_event_id.strip(),
                    limit=max(1, min(int(limit), 100)),
                    current_session=current_session,
                )
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

    def _discover(self, query: str, limit: int, current_session: str) -> str:
        results = self._indexer.search(
            query=query,
            limit=limit,
            exclude_session=None,
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
                "current_session": bool(
                    current_session and r["session_key"] == current_session
                ),
                "state": r.get("state", "active"),
                "event_id": r.get("event_id"),
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
                "Scroll into a hit by calling with `target_session` + "
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
            "current_session": bool(current_session and session_key == current_session),
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

    def _browse(self, limit: int, current_session: str) -> str:
        results = self._indexer.list_recent(
            limit=limit,
            exclude_session=None,
        )
        items = []
        for r in results:
            items.append({
                "session_key": r["key"],
                "date": _format_ts(r.get("created_at")),
                "last_active": _format_ts(r.get("updated_at")),
                "messages": r.get("msg_count", 0),
                "preview": r.get("preview", ""),
                "current_session": bool(
                    current_session and r["key"] == current_session
                ),
            })
        return json.dumps({
            "mode": "browse",
            "results": items,
            "count": len(items),
            "next_action": "Pass a `query` to search, or `target_session` + `around_message_id` to scroll into one.",
        }, ensure_ascii=False)

    # ── EXACT ───────────────────────────────────────────────────────

    def _exact(
        self,
        mode: str,
        session_key: str,
        event_id: str,
        start_event_id: str,
        end_event_id: str,
        *,
        limit: int,
        current_session: str,
    ) -> str:
        if mode not in {"first_user_message", "by_id", "range"}:
            return json.dumps({"mode": "exact", "error": "unknown exact_mode"})
        if not session_key:
            return json.dumps({
                "mode": "exact",
                "error": "target_session is required when there is no current session",
            })

        if mode == "first_user_message":
            message = self._indexer.first_user_message(session_key)
            messages = [message] if message else []
        elif mode == "by_id":
            if not event_id:
                return json.dumps({
                    "mode": "exact", "exact_mode": mode,
                    "error": "event_id is required",
                })
            message = self._indexer.message_by_event_id(session_key, event_id)
            messages = [message] if message else []
        else:
            if not start_event_id or not end_event_id:
                return json.dumps({
                    "mode": "exact", "exact_mode": mode,
                    "error": "start_event_id and end_event_id are required",
                })
            messages = self._indexer.messages_by_event_range(
                session_key,
                start_event_id,
                end_event_id,
                limit=limit,
            )

        return json.dumps({
            "mode": "exact",
            "exact_mode": mode,
            "session_key": session_key,
            "current_session": bool(
                current_session and session_key == current_session
            ),
            "messages": [_shape_msg(message) for message in messages],
            "count": len(messages),
        }, ensure_ascii=False)
