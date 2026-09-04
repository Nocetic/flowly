"""Durable public MCP history/event contracts against the real session store."""

from __future__ import annotations

import asyncio
import json
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest

from flowly.mcp.server.events import EventJournal
from flowly.mcp.server.readplane import SessionReader
from flowly.session.manager import SessionManager


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path))
    from flowly.mcp.server import readplane
    readplane.get_session_reader.cache_clear()
    manager = SessionManager(tmp_path)
    reader = SessionReader()
    yield manager, reader, tmp_path
    if reader._indexer:
        reader._indexer.close()
    readplane.get_session_reader.cache_clear()


def append(manager, key, content, **extra):
    session = manager.get_or_create(key)
    session.add_message("user", content, **extra)
    manager.save(session)
    return session


def test_archive_history_and_attachments_survive_compaction(state):
    manager, reader, _ = state
    session = append(manager, "web:under_score", "", media=["/private/test/photo.png"])
    old_id = session.messages[0]["_event_id"]
    append(manager, session.key, "newer message")
    manager.transition_archive_events(session, [old_id], "compacted")
    session.messages = session.messages[1:]
    manager.mark_full_synced(session)
    manager.save(session)

    history = reader.messages_read(session.key)
    assert history["count"] == 2
    assert history["messages"][0]["message_id"] == old_id
    media = reader.attachments_fetch(session.key, old_id)
    assert media["attachments"][0]["fileName"] == "photo.png"
    assert "/private" not in json.dumps(media)
    assert "error" in reader.attachments_fetch("web:other", old_id)
    assert manager.list_sessions()[0]["key"] == "web:under_score"


def test_events_do_not_replay_baseline_or_duplicate_after_restart(state):
    manager, reader, path = state
    append(manager, "web:a", "before baseline")
    journal = EventJournal(reader)
    head = journal.poll()
    assert head["events"] == []
    session = append(manager, "web:a", "after baseline")
    first = journal.poll(head["next_cursor"])
    assert [e["content"] for e in first["events"]] == ["after baseline"]
    # Reconnect with a fresh bridge object and the persisted cursor.
    restarted = EventJournal(SessionReader(), path / "mcp" / "conversation-events.sqlite")
    manager.save(session)
    replay = restarted.poll(first["next_cursor"])
    assert replay["events"] == []
    assert restarted.poll(head["next_cursor"])["events"] == first["events"]


def test_event_pagination_filtering_and_explicit_retention_gap(state):
    manager, reader, _ = state
    journal = EventJournal(reader, max_events=3)
    head = journal.poll(session_key="web:a")["next_cursor"]
    append(manager, "web:b", "unrelated")
    append(manager, "web:a", "one")
    first = journal.poll(head, "web:a", limit=1)
    assert [e["content"] for e in first["events"]] == ["one"]
    append(manager, "web:a", "two")
    append(manager, "web:a", "three")
    second = journal.poll(first["next_cursor"], "web:a", limit=1)
    assert second["has_more"] is True
    third = journal.poll(second["next_cursor"], "web:a", limit=1)
    assert [e["content"] for e in third["events"]] == ["three"]
    assert journal.poll(head, "web:a")["code"] == "CURSOR_EXPIRED"
    assert "error" in journal.poll(third["next_cursor"], "web:b")


def test_deleted_session_content_is_removed_from_retained_events(state):
    manager, reader, _ = state
    journal = EventJournal(reader)
    head = journal.poll()["next_cursor"]
    append(manager, "web:a", "should disappear")
    assert journal.poll(head)["events"]
    manager.delete("web:a")
    events = journal.poll(head)["events"]
    assert [e["type"] for e in events] == ["session_deleted"]
    assert "should disappear" not in json.dumps(events)


def test_reader_refreshes_external_writes_and_deletes_without_restart(state):
    manager, reader, _ = state
    assert reader.conversations_list()["count"] == 0
    session = append(manager, "web:under_score:topic", "needle")
    assert reader.conversations_list()["conversations"][0]["session_key"] == session.key
    assert reader.messages_search("needle")["count"] == 1
    append(manager, session.key, "x" * 4500 + " deepneedle")
    assert reader.messages_search("deepneedle")["count"] == 1
    assert reader.conversation_get(session.key)["msg_count"] == 2
    assert "error" in reader.messages_read("web:under_score_topic")
    manager.delete(session.key)
    assert reader.messages_search("needle")["count"] == 0
    assert reader.conversations_list()["count"] == 0
    assert "error" in reader.conversation_get(session.key)


def test_hidden_and_withdrawn_messages_never_enter_public_reads_or_events(state):
    manager, reader, _ = state
    journal = EventJournal(reader)
    head = journal.poll()["next_cursor"]
    session = append(manager, "web:a", "hiddenneedle", _display_hidden=True)
    append(manager, session.key, "withdrawnneedle")
    event_id = session.messages[-1]["_event_id"]
    assert reader.messages_search("withdrawnneedle")["count"] == 1
    assert journal.poll(head)["events"]
    manager.transition_archive_events(session, [event_id], "withdrawn")
    assert reader.messages_search("hiddenneedle")["count"] == 0
    assert reader.messages_search("withdrawnneedle")["count"] == 0
    assert reader.messages_read(session.key)["count"] == 0
    assert journal.poll(head)["events"] == []


def test_concurrent_independent_bridges_share_one_event_identity(state):
    manager, reader, _ = state
    journal = EventJournal(reader)
    head = journal.poll()["next_cursor"]
    peers = [EventJournal(reader) for _ in range(4)]
    append(manager, "web:a", "only once")
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda peer: peer.poll(head), peers))
    assert all(len(result["events"]) == 1 for result in results)
    assert all(result == results[0] for result in results)


@pytest.mark.parametrize("cursor", ["!", "[]", "bnVsbA", "eyJwb3NpdGlvbiI6dHJ1ZX0", 42])
def test_event_cursor_rejects_malformed_input(state, cursor):
    _manager, reader, _ = state
    assert "error" in EventJournal(reader).poll(cursor)


def test_attachment_projection_rejects_secret_urls_and_malformed_metadata():
    from flowly.mcp.server.projection import attachments

    result = attachments({"media": [
        {"path": "https://[invalid", "size": float("inf")},
        {"path": "https://example.org/a.png?token=secret", "width": float("nan")},
    ]})
    assert result[0]["fileName"] == "attachment"
    assert result[1]["fileName"] == "a.png"
    assert "secret" not in json.dumps(result, allow_nan=False)


@pytest.mark.asyncio
async def test_wait_observes_new_messages_and_is_cancellable(state):
    manager, reader, _ = state
    journal = EventJournal(reader)
    head = journal.poll()["next_cursor"]
    waiter = asyncio.create_task(journal.wait(head, timeout_ms=3000))
    await asyncio.sleep(0)
    append(manager, "web:a", "arrived while waiting")
    result = await asyncio.wait_for(waiter, 4)
    assert result["events"][0]["content"] == "arrived while waiting"
    waiter = asyncio.create_task(journal.wait(result["next_cursor"], timeout_ms=300_000))
    await asyncio.sleep(0.03)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    timeout = await journal.wait(result["next_cursor"], timeout_ms=0)
    assert timeout["reason"] == "timeout"


@pytest.mark.asyncio
async def test_public_mcp_stdio_wait_and_attachment_read(state):
    """A separate MCP subprocess observes writes from this process."""
    import os

    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    manager, _reader, path = state
    params = StdioServerParameters(
        command=sys.executable,
        args=["-c", "from flowly.mcp.server import run_server; run_server()"],
        env={**os.environ, "FLOWLY_HOME": str(path)},
    )
    async with stdio_client(params) as (read, write), ClientSession(read, write) as client:
        await client.initialize()
        head = await client.call_tool("events_poll", {})
        cursor = head.structured_content["next_cursor"]
        waiter = asyncio.create_task(client.call_tool("events_wait", {
            "after_cursor": cursor, "timeout_ms": 5000,
        }))
        append(manager, "web:stdio_test", "", media=["/private/test/audio.wav"])
        result = await asyncio.wait_for(waiter, 8)
        assert result.is_error is False
        event = result.structured_content["events"][0]
        media = await client.call_tool("attachments_fetch", {
            "session_key": "web:stdio_test", "message_id": event["message_id"],
        })
        assert media.structured_content["attachments"][0]["mimeType"] == "audio/x-wav"
        assert "/private/test" not in str(media)
        saved_cursor = result.structured_content["next_cursor"]
        cancelled = asyncio.create_task(client.call_tool("events_wait", {
            "after_cursor": saved_cursor, "timeout_ms": 300_000,
        }))
        await asyncio.sleep(0.05)
        cancelled.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled
        health = await asyncio.wait_for(client.call_tool("conversations_list", {}), 2)
        assert health.structured_content["conversations"][0]["session_key"] == "web:stdio_test"

    # Full process restart, not just reconnecting a Python reader object.
    append(manager, "web:stdio_test", "written while MCP was stopped")
    async with stdio_client(params) as (read, write), ClientSession(read, write) as client:
        await client.initialize()
        resumed = await client.call_tool("events_poll", {"after_cursor": saved_cursor})
        assert [e["content"] for e in resumed.structured_content["events"]] == [
            "written while MCP was stopped",
        ]


def test_channel_targets_keep_exact_session_address(state):
    from flowly.mcp.server.readplane import channels_list

    manager, _reader, _ = state
    append(manager, "telegram:group_id:topic", "hello")
    result = channels_list(platform="telegram")
    assert result["targets"][0]["target"] == "telegram:group_id:topic"
    assert len(result["channels"]) == 1
