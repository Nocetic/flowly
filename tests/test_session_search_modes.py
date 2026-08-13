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

from flowly.agent.loop import AgentLoop
from flowly.agent.tools.base import Tool
from flowly.agent.tools.session_search import SessionSearchTool
from flowly.bus.queue import MessageBus
from flowly.config.schema import Config
from flowly.providers.base import LLMProvider, LLMResponse, ToolCallRequest
from flowly.session.indexer import SessionIndexer


@pytest.fixture
def indexer():
    """Seeded indexer with two distinct sessions for cross-session search."""
    with tempfile.TemporaryDirectory() as tmp:
        idx = SessionIndexer(db_path=Path(tmp) / "search.db")
        idx.index_archive("docker-session", [
            {"role": "user", "content": "How do I deploy with docker?", "timestamp": "2026-05-01T10:00:00", "_event_id": "docker-1", "_event_seq": 1, "_archive_state": "compacted"},
            {"role": "assistant", "content": "Use docker compose up -d.", "timestamp": "2026-05-01T10:00:01", "_event_id": "docker-2", "_event_seq": 2, "_archive_state": "compacted"},
            {"role": "user", "content": "What about kubernetes?", "timestamp": "2026-05-01T10:01:00", "_event_id": "docker-3", "_event_seq": 3, "_archive_state": "active"},
            {"role": "assistant", "content": "For k8s use kubectl apply.", "timestamp": "2026-05-01T10:01:01", "_event_id": "docker-4", "_event_seq": 4, "_archive_state": "active"},
            {"role": "user", "content": "Got it, thanks.", "timestamp": "2026-05-01T10:02:00", "_event_id": "docker-5", "_event_seq": 5, "_archive_state": "active"},
            {"role": "assistant", "content": "Anytime.", "timestamp": "2026-05-01T10:02:01", "_event_id": "docker-6", "_event_seq": 6, "_archive_state": "active"},
        ])
        idx.index_archive("react-session", [
            {"role": "user", "content": "My react component is not re-rendering.", "timestamp": "2026-05-02T10:00:00", "_event_id": "react-1", "_event_seq": 1, "_archive_state": "active"},
            {"role": "assistant", "content": "Check the useEffect dependency array.", "timestamp": "2026-05-02T10:00:01", "_event_id": "react-2", "_event_seq": 2, "_archive_state": "active"},
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


def test_scroll_allows_current_session_archive(tool):
    """Compacted current-session events are no longer guaranteed in context."""
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
    assert payload["mode"] == "scroll"
    assert payload["current_session"] is True
    assert any(message["state"] == "compacted" for message in payload["messages"])


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


# ── CURRENT SESSION + EXACT modes ──────────────────────────────────


def test_discover_includes_compacted_current_session(tool):
    payload = json.loads(asyncio.run(
        tool.execute(query="docker", session_key="docker-session")
    ))

    hit = next(result for result in payload["results"] if result["session_key"] == "docker-session")
    assert hit["current_session"] is True
    assert hit["state"] == "compacted"
    assert hit["event_id"] in {"docker-1", "docker-2"}


def test_exact_first_user_message_defaults_to_current_session(tool):
    payload = json.loads(asyncio.run(tool.execute(
        exact_mode="first_user_message",
        session_key="docker-session",
    )))

    assert payload["mode"] == "exact"
    assert payload["current_session"] is True
    assert payload["messages"][0]["event_id"] == "docker-1"
    assert payload["messages"][0]["content"] == "How do I deploy with docker?"


def test_exact_by_stable_event_id(tool):
    payload = json.loads(asyncio.run(tool.execute(
        exact_mode="by_id",
        target_session="docker-session",
        event_id="docker-3",
    )))

    assert payload["messages"] == [{
        "id": payload["messages"][0]["id"],
        "event_id": "docker-3",
        "role": "user",
        "content": "What about kubernetes?",
        "state": "active",
        "timestamp": "2026-05-01 10:01",
    }]


def test_exact_event_range_is_inclusive_and_ordered(tool):
    payload = json.loads(asyncio.run(tool.execute(
        exact_mode="range",
        target_session="docker-session",
        start_event_id="docker-2",
        end_event_id="docker-4",
        limit=10,
    )))

    assert [message["event_id"] for message in payload["messages"]] == [
        "docker-2", "docker-3", "docker-4",
    ]


def test_withdrawn_events_are_not_recalled(indexer, tool):
    indexer._conn.execute(
        "UPDATE messages SET state = 'withdrawn' WHERE event_id = 'docker-1'"
    )
    indexer._conn.commit()

    discover = json.loads(asyncio.run(tool.execute(query="docker")))
    exact = json.loads(asyncio.run(tool.execute(
        exact_mode="by_id",
        target_session="docker-session",
        event_id="docker-1",
    )))

    assert not any(
        result.get("event_id") == "docker-1"
        for result in discover["results"]
    )
    assert exact["messages"] == []


@pytest.mark.asyncio
async def test_agent_runtime_injects_current_session_into_search_tool(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path / "home"))
    calls: list[dict] = []

    class CaptureSearch(Tool):
        @property
        def name(self) -> str:
            return "session_search"

        @property
        def description(self) -> str:
            return "Search sessions."

        @property
        def parameters(self) -> dict:
            return {
                "type": "object",
                "properties": {"query": {"type": "string"}},
            }

        async def execute(self, **kwargs) -> str:
            calls.append(kwargs)
            return json.dumps({"results": []})

    class Provider(LLMProvider):
        def __init__(self):
            super().__init__(api_key="test")
            self.calls = 0

        def get_default_model(self) -> str:
            return "test/model"

        async def chat(self, *args, **kwargs) -> LLMResponse:
            self.calls += 1
            if self.calls == 1:
                return LLMResponse(
                    content=None,
                    tool_calls=[ToolCallRequest(
                        id="search-1",
                        name="session_search",
                        arguments={"query": "docker"},
                    )],
                )
            return LLMResponse(content="done")

    loop = AgentLoop(
        bus=MessageBus(),
        provider=Provider(),
        workspace=tmp_path,
        main_config=Config(),
        max_iterations=2,
        soft_warn_at_iteration=0,
    )
    loop.tools.register(CaptureSearch(), toolset="sessions")
    try:
        final, *_ = await loop._run_llm_tool_loop(
            messages=[
                {"role": "system", "content": "test"},
                {"role": "user", "content": "find docker"},
            ],
            action_turn=False,
            turn_content="find docker",
            session_key="web:active-conversation",
            tool_platform="web",
        )
    finally:
        loop.stop()

    assert final == "done"
    assert calls == [{"query": "docker", "session_key": "web:active-conversation"}]
    assert "session_key" not in CaptureSearch().parameters["properties"]
