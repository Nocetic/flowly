"""Gateway transport-rebind: a session's live stream follows the latest socket.

A run streams to the socket that started it. If the client leaves and re-enters
mid-stream it comes back on a NEW socket and calls chat.inflight; without
rebinding, forward events (deltas / iteration_step / final) keep going to the
dead socket and the re-entered view freezes at the snapshot. ``bind_session_ws``
+ ``_session_send`` route every live event to the session's CURRENT socket.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from flowly.gateway.server import GatewayServer


class _FakeWS:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.closed = False

    async def send_json(self, data: dict) -> None:
        self.sent.append(data)


def _bare_server() -> GatewayServer:
    srv = object.__new__(GatewayServer)  # bypass the heavy __init__
    srv._session_ws = {}
    return srv


@pytest.mark.asyncio
async def test_forward_events_follow_reentered_socket() -> None:
    srv = _bare_server()
    ws_a, ws_b = _FakeWS(), _FakeWS()

    # chat.send started the run on ws_a.
    srv.bind_session_ws("web:c1", ws_a)
    await srv._session_send("web:c1", ws_a, {"d": 1})
    assert ws_a.sent == [{"d": 1}]
    assert ws_b.sent == []

    # Client left and re-entered on ws_b (chat.inflight rebinds).
    srv.bind_session_ws("web:c1", ws_b)
    await srv._session_send("web:c1", ws_a, {"d": 2})  # fallback ws_a, current ws_b
    assert ws_a.sent == [{"d": 1}]
    assert ws_b.sent == [{"d": 2}]


@pytest.mark.asyncio
async def test_unbound_session_falls_back_to_originating_socket() -> None:
    srv = _bare_server()
    ws = _FakeWS()
    await srv._session_send("web:none", ws, {"d": 3})
    assert ws.sent == [{"d": 3}]


@pytest.mark.asyncio
async def test_closed_current_socket_is_dropped_silently() -> None:
    srv = _bare_server()
    ws_a, ws_b = _FakeWS(), _FakeWS()
    srv.bind_session_ws("web:c1", ws_b)
    ws_b.closed = True
    # current (ws_b) closed → dropped; must not raise, must not hit fallback.
    await srv._session_send("web:c1", ws_a, {"d": 4})
    assert ws_a.sent == []
    assert ws_b.sent == []


def test_bind_ignores_empty_session_key() -> None:
    srv = _bare_server()
    ws = _FakeWS()
    srv.bind_session_ws("", ws)
    assert srv._session_ws == {}


@pytest.mark.asyncio
async def test_chat_history_surfaces_assistant_completion_identity() -> None:
    srv = _bare_server()
    srv.sessions = SimpleNamespace(
        get_or_create=lambda _key: SimpleNamespace(metadata={}),
        get_full_messages=lambda _key: [
            {
                "role": "assistant",
                "content": "Done.",
                "timestamp": "2026-07-30T10:00:00+00:00",
                "run_id": "run-1",
            }
        ],
    )
    srv._ws_rpc_reply = AsyncMock()

    await srv._ws_rpc_chat_history(
        _FakeWS(),
        "rpc-1",
        {"sessionKey": "ios:chat-1"},
    )

    payload = srv._ws_rpc_reply.await_args.args[2]
    assert payload["messages"][0]["runId"] == "run-1"
    assert payload["messages"][0]["timestamp"] == "2026-07-30T10:00:00+00:00"


@pytest.mark.asyncio
async def test_chat_history_projects_deferred_tool_identity_for_clients() -> None:
    canonical_messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": "call-1",
                "type": "function",
                "function": {
                    "name": "tool_call",
                    "arguments": (
                        '{"name":"mcp_context7_query_docs",'
                        '"arguments":{"query":"useEffect"}}'
                    ),
                },
            }],
        },
        {
            "role": "tool",
            "content": "docs",
            "tool_call_id": "call-1",
            "name": "tool_call",
        },
    ]
    srv = _bare_server()
    srv.sessions = SimpleNamespace(
        get_or_create=lambda _key: SimpleNamespace(metadata={}),
        get_full_messages=lambda _key: canonical_messages,
    )
    srv._ws_rpc_reply = AsyncMock()

    await srv._ws_rpc_chat_history(
        _FakeWS(),
        "rpc-1",
        {"sessionKey": "desktop:chat-1"},
    )

    payload = srv._ws_rpc_reply.await_args.args[2]
    call = payload["messages"][0]["tool_calls"][0]
    assert call["function"]["name"] == "mcp_context7_query_docs"
    assert call["tool_activity"]["protocol_name"] == "tool_call"
    assert payload["messages"][1]["name"] == "mcp_context7_query_docs"
    # The gateway read path must never rewrite canonical session records.
    assert canonical_messages[0]["tool_calls"][0]["function"]["name"] == "tool_call"


@pytest.mark.asyncio
async def test_chat_history_exposes_full_display_rows_without_archive_internals() -> None:
    """Internal lineage can evolve without changing released client DTOs."""
    srv = _bare_server()
    srv.sessions = SimpleNamespace(
        get_or_create=lambda _key: SimpleNamespace(metadata={}),
        get_full_messages=lambda _key: [
            {
                "role": "user",
                "content": "early question",
                "timestamp": "2026-01-01T00:00:00+00:00",
                "_event_id": "evt_1",
                "_event_seq": 1,
                "_archive_state": "compacted",
            },
            {
                "role": "assistant",
                "content": "[context-optimized]",
                "kind": "context_boundary",
                "boundaryKind": "compaction",
                "compactionId": "cmp_1",
                "_event_id": "evt_boundary",
                "_event_seq": 2,
                "_archive_state": "active",
            },
            {
                "role": "assistant",
                "content": "recent answer",
                "timestamp": "2026-01-01T00:00:02+00:00",
                "_provider_replay": {
                    "items": [{
                        "type": "reasoning",
                        "encrypted_content": "must-not-leak",
                    }],
                },
                "_event_id": "evt_3",
                "_event_seq": 3,
                "_archive_state": "active",
            },
        ],
    )
    srv._ws_rpc_reply = AsyncMock()

    await srv._ws_rpc_chat_history(
        _FakeWS(),
        "rpc-history",
        {"sessionKey": "desktop:full-history"},
    )

    payload = srv._ws_rpc_reply.await_args.args[2]
    messages = payload["messages"]
    assert [
        "".join(block.get("text", "") for block in row["content"])
        for row in messages
    ] == ["early question", "[context-optimized]", "recent answer"]
    assert set(messages[0]) == {"role", "content", "timestamp"}
    assert set(messages[1]) == {
        "role", "content", "kind", "boundaryKind", "compactionId",
    }
    assert set(messages[2]) == {"role", "content", "timestamp"}


@pytest.mark.asyncio
async def test_offline_chat_final_schedules_push(monkeypatch) -> None:
    srv = _bare_server()
    calls: list[dict] = []

    async def fake_notify(title: str, body: str, **kwargs) -> None:
        calls.append({"title": title, "body": body, **kwargs})

    from flowly.push import relay_push

    monkeypatch.setattr(relay_push, "notify_devices", fake_notify)
    srv._schedule_offline_chat_push(
        "ios:chat-1",
        "hello\nsecond line",
        run_id="run-1",
        completed_at="2026-07-30T10:00:00Z",
    )
    await asyncio.sleep(0)

    assert calls == [{
        "title": "Flowly",
        "body": "hello",
        "conversation_id": "ios:chat-1",
        "data": {
            "type": "chat",
            "runId": "run-1",
            "completedAt": "2026-07-30T10:00:00Z",
        },
    }]


@pytest.mark.asyncio
async def test_offline_chat_final_skips_push_when_session_live(monkeypatch) -> None:
    srv = _bare_server()
    ws = _FakeWS()
    srv.bind_session_ws("ios:chat-1", ws)
    calls: list[dict] = []

    async def fake_notify(title: str, body: str, **kwargs) -> None:
        calls.append({"title": title, "body": body, **kwargs})

    from flowly.push import relay_push

    monkeypatch.setattr(relay_push, "notify_devices", fake_notify)
    srv._schedule_offline_chat_push("ios:chat-1", "hello")
    await asyncio.sleep(0)

    assert calls == []


@pytest.mark.asyncio
async def test_offline_media_only_final_still_schedules_attention_push(monkeypatch) -> None:
    srv = _bare_server()
    calls: list[dict] = []

    async def fake_notify(title: str, body: str, **kwargs) -> None:
        calls.append({"title": title, "body": body, **kwargs})

    from flowly.push import relay_push

    monkeypatch.setattr(relay_push, "notify_devices", fake_notify)
    srv._schedule_offline_chat_push(
        "ios:chat-1",
        "",
        run_id="run-media",
        completed_at="2026-07-30T10:00:00Z",
    )
    await asyncio.sleep(0)

    assert calls[0]["body"] == "New message"
    assert calls[0]["data"]["runId"] == "run-media"
