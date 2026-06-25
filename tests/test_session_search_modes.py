"""Regression suite for session_search's three calling modes.

One tool, three modes inferred from args (discover / scroll / browse),
zero LLM cost. These tests pin the dispatch logic + payload shape so
future indexer refactors don't silently break the agent's expected
response.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

import pytest

from flowly.agent.tools.session_search import SessionSearchTool
from flowly.session.indexer import SessionIndexer


@pytest.fixture
def indexer():
    """Seeded indexer with two distinct sessions for cross-session search."""
    with tempfile.TemporaryDirectory() as tmp:
        idx = SessionIndexer(db_path=Path(tmp) / "search.db")
        idx.index_session("docker-session", [
            {"role": "user", "content": "How do I deploy with docker?", "timestamp": "2026-05-01T10:00:00"},
            {"role": "assistant", "content": "Use docker compose up -d.", "timestamp": "2026-05-01T10:00:01"},
            {"role": "user", "content": "What about kubernetes?", "timestamp": "2026-05-01T10:01:00"},
            {"role": "assistant", "content": "For k8s use kubectl apply.", "timestamp": "2026-05-01T10:01:01"},
            {"role": "user", "content": "Got it, thanks.", "timestamp": "2026-05-01T10:02:00"},
            {"role": "assistant", "content": "Anytime.", "timestamp": "2026-05-01T10:02:01"},
        ])
        idx.index_session("react-session", [
            {"role": "user", "content": "My react component is not re-rendering.", "timestamp": "2026-05-02T10:00:00"},
            {"role": "assistant", "content": "Check the useEffect dependency array.", "timestamp": "2026-05-02T10:00:01"},
        ])
        yield idx


@pytest.fixture
def tool(indexer):
    return SessionSearchTool(indexer=indexer)


# ── DISCOVER mode ────────────────────────────────────────────────────


def test_discover_returns_hit_with_anchor_id(tool):
    """Discover hit must carry an anchor_id so the agent can scroll into it."""
    payload = json.loads(asyncio.run(tool.execute(query="docker")))
    assert payload["mode"] == "discover"
    assert payload["count"] >= 1
    hit = payload["results"][0]
    assert hit["session_key"] == "docker-session"
    assert isinstance(hit["anchor_id"], int)
    assert hit["anchor_id"] > 0


def test_discover_includes_snippet_with_match_markers(tool):
    """FTS5 snippet must wrap the matched token with the configured markers."""
    payload = json.loads(asyncio.run(tool.execute(query="docker")))
    hit = payload["results"][0]
    assert ">>>" in hit["snippet"]
    assert "<<<" in hit["snippet"]


def test_discover_includes_bookends(tool):
    """Each discover hit must include the session's opening and closing turns."""
    payload = json.loads(asyncio.run(tool.execute(query="docker")))
    hit = payload["results"][0]
    assert len(hit["bookend_start"]) > 0
    assert len(hit["bookend_end"]) > 0
    assert hit["bookend_start"][0]["content"].startswith("How do I deploy")
    assert hit["bookend_end"][-1]["content"] == "Anytime."


def test_discover_includes_wider_context_than_legacy(tool):
    """Context must span more than the legacy ±1 message window (now ±3)."""
    payload = json.loads(asyncio.run(tool.execute(query="kubernetes")))
    hit = payload["results"][0]
    # ±3 around the kubernetes hit (id=3) → ids 1-6 visible
    assert len(hit["context"]) >= 3


def test_discover_empty_query_falls_through_to_browse(tool):
    """An empty query is treated as browse intent, not as an error."""
    payload = json.loads(asyncio.run(tool.execute(query="   ")))
    assert payload["mode"] == "browse"


# ── SCROLL mode ──────────────────────────────────────────────────────


def test_scroll_returns_window_centered_on_anchor(tool, indexer):
    """Scroll mode returns ±window messages with the anchor marked."""
    # Find anchor_id via discover first (id contract preserved across calls)
    disc = json.loads(asyncio.run(tool.execute(query="docker")))
    anchor_id = disc["results"][0]["anchor_id"]
    payload = json.loads(
        asyncio.run(
            tool.execute(target_session="docker-session", around_message_id=anchor_id, window=2)
        )
    )
    assert payload["mode"] == "scroll"
    assert payload["window"] == 2
    assert payload["around_message_id"] == anchor_id
    # At least one message marked as anchor
    anchored = [m for m in payload["messages"] if m.get("anchor")]
    assert len(anchored) == 1
    assert anchored[0]["id"] == anchor_id


def test_scroll_exposes_remaining_message_counts(tool):
    """messages_before / messages_after enable 'can I scroll further?' decisions."""
    disc = json.loads(asyncio.run(tool.execute(query="kubernetes")))
    anchor_id = disc["results"][0]["anchor_id"]  # id=3 in 6-msg session
    payload = json.loads(
        asyncio.run(
            tool.execute(target_session="docker-session", around_message_id=anchor_id, window=1)
        )
    )
    # Window of 1 around id=3 → ids 2,3,4. Before=1 (id 1), After=2 (ids 5,6)
    assert payload["messages_before"] == 1
    assert payload["messages_after"] == 2


def test_scroll_rejects_current_session(tool):
    """Scrolling inside the active session is a no-op — those messages are already in context."""
    disc = json.loads(asyncio.run(tool.execute(query="docker")))
    anchor_id = disc["results"][0]["anchor_id"]
    payload = json.loads(
        asyncio.run(
            tool.execute(
                target_session="docker-session",
                around_message_id=anchor_id,
                session_key="docker-session",
            )
        )
    )
    assert "error" in payload
    assert "current session" in payload["error"]


def test_scroll_window_clamped_to_max_20(tool):
    """Caller-provided window > 20 must be clamped — guards token budget."""
    disc = json.loads(asyncio.run(tool.execute(query="docker")))
    anchor_id = disc["results"][0]["anchor_id"]
    payload = json.loads(
        asyncio.run(
            tool.execute(target_session="docker-session", around_message_id=anchor_id, window=999)
        )
    )
    assert payload["window"] == 20


def test_scroll_unknown_session_errors_cleanly(tool):
    payload = json.loads(
        asyncio.run(
            tool.execute(target_session="ghost-session", around_message_id=1)
        )
    )
    assert "error" in payload
    assert "not found" in payload["error"]


def test_scroll_next_action_hint_points_to_boundary_ids(tool):
    """Hint must reference real boundary ids of the returned window."""
    disc = json.loads(asyncio.run(tool.execute(query="docker")))
    anchor_id = disc["results"][0]["anchor_id"]
    payload = json.loads(
        asyncio.run(
            tool.execute(target_session="docker-session", around_message_id=anchor_id, window=1)
        )
    )
    hint = payload["next_action"]
    first_id = payload["messages"][0]["id"]
    last_id = payload["messages"][-1]["id"]
    assert str(first_id) in hint
    assert str(last_id) in hint


# ── BROWSE mode ──────────────────────────────────────────────────────


def test_browse_lists_recent_sessions(tool):
    payload = json.loads(asyncio.run(tool.execute()))
    assert payload["mode"] == "browse"
    assert payload["count"] >= 2
    keys = {item["session_key"] for item in payload["results"]}
    assert "docker-session" in keys
    assert "react-session" in keys


def test_browse_includes_preview_text(tool):
    payload = json.loads(asyncio.run(tool.execute()))
    react = next(r for r in payload["results"] if r["session_key"] == "react-session")
    assert "react" in react["preview"].lower()
