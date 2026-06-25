"""Tests for `flowly mcp serve` read-plane (Faz 3a, M1a).

Exercises the standalone readers against a synthetic $FLOWLY_HOME with
hand-written JSONL sessions — no gateway, no network.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


@pytest.fixture
def home_with_sessions(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path))
    sessions = tmp_path / "sessions"
    sessions.mkdir(parents=True)

    def _write(key: str, rows: list[dict]):
        (sessions / f"{key}.jsonl").write_text(
            "\n".join(json.dumps(r) for r in rows)
        )

    _write("telegram_123", [
        {"_type": "metadata", "created_at": "2026-05-30T10:00:00",
         "updated_at": "2026-05-30T10:05:00", "metadata": {}},
        {"role": "user", "content": "hello from telegram", "timestamp": "2026-05-30T10:00:01"},
        {"role": "assistant", "content": "hi there", "timestamp": "2026-05-30T10:00:02"},
        {"role": "tool", "content": "internal", "tool_call_id": "x", "name": "y",
         "timestamp": "2026-05-30T10:00:03"},
    ])
    _write("discord_999", [
        {"_type": "metadata", "created_at": "2026-05-29T09:00:00",
         "updated_at": "2026-05-29T09:01:00", "metadata": {}},
        {"role": "user", "content": "discord question about pizza", "timestamp": "2026-05-29T09:00:01"},
    ])

    # Reset the cached reader so it picks up THIS home.
    from flowly.mcp.server import readplane
    readplane.get_session_reader.cache_clear()
    yield tmp_path
    readplane.get_session_reader.cache_clear()


def _reader():
    from flowly.mcp.server.readplane import get_session_reader
    return get_session_reader()


def test_conversations_list_all(home_with_sessions):
    out = _reader().conversations_list()
    keys = {c["session_key"] for c in out["conversations"]}
    assert keys == {"telegram:123", "discord:999"}


def test_conversations_list_platform_filter(home_with_sessions):
    out = _reader().conversations_list(platform="telegram")
    assert out["count"] == 1
    assert out["conversations"][0]["session_key"] == "telegram:123"


def test_conversations_list_search(home_with_sessions):
    out = _reader().conversations_list(search="pizza")
    assert out["count"] == 1
    assert out["conversations"][0]["session_key"] == "discord:999"


def test_conversation_get(home_with_sessions):
    out = _reader().conversation_get("telegram:123")
    assert out["session_key"] == "telegram:123"
    assert out["platform"] == "telegram"
    assert out["msg_count"] == 2  # tool/metadata excluded from count


def test_conversation_get_missing(home_with_sessions):
    out = _reader().conversation_get("telegram:nope")
    assert "error" in out


def test_messages_read_excludes_tool_and_metadata(home_with_sessions):
    out = _reader().messages_read("telegram:123")
    roles = [m["role"] for m in out["messages"]]
    assert roles == ["user", "assistant"]
    assert out["messages"][0]["content"] == "hello from telegram"
    assert out["messages"][0]["timestamp"] == "2026-05-30T10:00:01"


def test_messages_read_limit(home_with_sessions):
    out = _reader().messages_read("telegram:123", limit=1)
    assert out["count"] == 1
    # Most recent kept.
    assert out["messages"][0]["role"] == "assistant"


def test_messages_search_fts(home_with_sessions):
    out = _reader().messages_search("pizza")
    assert out["count"] >= 1
    assert any(r["session_key"] == "discord:999" for r in out["results"])


def test_messages_search_empty_query(home_with_sessions):
    out = _reader().messages_search("")
    assert "error" in out


def test_channels_list_reads_config(home_with_sessions):
    from flowly.mcp.server.readplane import channels_list
    out = channels_list()
    names = {c["platform"] for c in out["channels"]}
    assert {"telegram", "discord", "slack", "whatsapp", "web", "email", "teams"} <= names


def test_server_builds_with_read_tools(home_with_sessions):
    try:
        import mcp  # noqa: F401
    except ImportError:
        pytest.skip("mcp SDK not installed")
    from flowly.mcp.server.serve import create_server
    server = create_server(allow_writes=False)
    assert server is not None


def test_registered_tools_are_callable(home_with_sessions):
    """Regression: invoke each read tool THROUGH the FastMCP wrapper, not
    just the readplane helper. Catches name-collision bugs like a tool
    function shadowing its imported helper (channels_list → infinite
    recursion), which create_server() alone wouldn't surface.
    """
    try:
        import mcp  # noqa: F401
    except ImportError:
        pytest.skip("mcp SDK not installed")
    import asyncio
    from flowly.mcp.server.serve import create_server

    server = create_server(allow_writes=False)
    mgr = server._tool_manager

    async def _call(name, args):
        return await mgr.call_tool(name, args)

    # channels_list previously self-recursed; assert it returns real data.
    out = asyncio.run(_call("channels_list", {}))
    assert "telegram" in str(out)

    out = asyncio.run(_call("conversations_list", {"limit": 5}))
    assert "telegram:123" in str(out)

    out = asyncio.run(_call("messages_search", {"query": "pizza"}))
    assert "discord:999" in str(out)
