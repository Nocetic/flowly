"""Session deletion is a lifecycle transaction, not a single-file unlink."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from flowly.agent import inflight
from flowly.agent.loop import AgentLoop
from flowly.gateway.server import GatewayServer
from flowly.session.indexer import SessionIndexer
from flowly.session.manager import (
    ConcurrentSessionWriteError,
    SessionBusyError,
    SessionManager,
)


@pytest.fixture
def manager(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("FLOWLY_HOME", str(home))
    from flowly import profile

    profile._cached_home = None
    value = SessionManager(workspace=tmp_path)
    value._indexer = SessionIndexer(db_path=home / "session-index.sqlite")
    try:
        yield value
    finally:
        value._indexer.close()
        profile._cached_home = None


def _seed(manager: SessionManager, key: str = "cli:delete-me"):
    session = manager.get_or_create(key)
    session.add_message("user", "unique-delete-marker")
    session.add_message("assistant", "temporary answer")
    manager.save(session)
    return session


def test_delete_removes_canonical_display_cache_and_search_rows(manager):
    session = _seed(manager)
    key = session.key
    assert manager._get_session_path(key).is_file()
    assert manager._get_full_path(key).is_file()
    assert manager._indexer.get_session_meta(key) is not None
    assert manager._indexer.search("unique-delete-marker")

    assert manager.delete(key) is True

    assert not manager._get_session_path(key).exists()
    assert not manager._get_full_path(key).exists()
    assert key not in manager._cache
    assert manager._indexer.get_session_meta(key) is None
    assert manager._indexer.search("unique-delete-marker") == []
    assert manager.delete(key) is False


def test_delete_cleans_a_legacy_orphaned_display_transcript(manager):
    key = "cli:display-only"
    display = manager._get_full_path(key)
    display.parent.mkdir(parents=True, exist_ok=True)
    display.write_text('{"role":"user","content":"orphan"}\n', encoding="utf-8")

    assert manager.delete(key) is True
    assert not display.exists()


def test_stale_writer_cannot_recreate_a_deleted_session(manager, tmp_path):
    session = _seed(manager, "cli:stale-writer")
    stale_manager = SessionManager(workspace=tmp_path)
    stale = stale_manager.get_or_create(session.key)

    assert manager.delete(session.key) is True
    with pytest.raises(ConcurrentSessionWriteError, match="stale session revision"):
        stale_manager.save(stale)

    assert not manager._get_session_path(session.key).exists()
    assert not manager._get_full_path(session.key).exists()


@pytest.mark.asyncio
async def test_agent_delete_cancels_background_session_writers():
    key = "cli:background"
    cancelled: list[str] = []

    async def writer(label: str) -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.append(label)
            raise

    title = asyncio.create_task(writer("title"))
    compaction = asyncio.create_task(writer("compaction"))
    await asyncio.sleep(0)

    loop = object.__new__(AgentLoop)
    loop._title_tasks = {key: title}
    loop._post_turn_compaction_tasks = {key: compaction}
    loop._last_turn_total_tokens = {key: 42}
    loop._started_sessions = {key}
    loop.sessions = SimpleNamespace(delete=Mock(return_value=True))

    assert await loop.delete_session(key) is True
    assert set(cancelled) == {"title", "compaction"}
    assert key not in loop._title_tasks
    assert key not in loop._post_turn_compaction_tasks
    assert key not in loop._last_turn_total_tokens
    assert key not in loop._started_sessions
    loop.sessions.delete.assert_called_once_with(key)


@pytest.mark.asyncio
async def test_agent_delete_rechecks_busy_state_at_turn_lock_boundary():
    key = "cli:raced-delete"
    loop = object.__new__(AgentLoop)
    loop._session_turn_locks = {}
    loop._title_tasks = {}
    loop._post_turn_compaction_tasks = {}
    loop._last_turn_total_tokens = {}
    loop._started_sessions = set()
    loop.sessions = SimpleNamespace(delete=Mock(return_value=True))
    inflight.begin(key, "run-race", "new turn")

    try:
        with pytest.raises(SessionBusyError, match=key):
            await loop.delete_session(key)
    finally:
        inflight.finish(key, "run-race")

    loop.sessions.delete.assert_not_called()


@pytest.mark.asyncio
async def test_gateway_refuses_to_delete_an_active_session():
    key = "cli:busy-delete"
    inflight.begin(key, "run-1", "working")
    try:
        server = object.__new__(GatewayServer)
        server.sessions = SimpleNamespace(delete=Mock(return_value=True))
        server.on_session_delete = AsyncMock(return_value=True)
        server._ws_rpc_error = AsyncMock()
        server._ws_rpc_reply = AsyncMock()
        ws = object()

        await server._ws_rpc_sessions_delete(
            ws, "rpc-1", {"sessionKey": key}
        )

        server._ws_rpc_error.assert_awaited_once_with(
            ws,
            "rpc-1",
            "SESSION_BUSY",
            "Finish or stop the active turn before deleting this session.",
        )
        server._ws_rpc_reply.assert_not_awaited()
        server.on_session_delete.assert_not_awaited()
        server.sessions.delete.assert_not_called()
    finally:
        inflight.finish(key, "run-1")


@pytest.mark.asyncio
async def test_gateway_routes_idle_session_delete_through_owner(monkeypatch):
    key = "cli:idle-delete"
    cleared: list[str] = []
    monkeypatch.setattr(
        "flowly.runtime_cwd.clear_session_cwd",
        lambda value: cleared.append(value),
    )
    ws = object()
    server = object.__new__(GatewayServer)
    server.sessions = SimpleNamespace(delete=Mock(return_value=False))
    server.on_session_delete = AsyncMock(return_value=True)
    server._ws_rpc_error = AsyncMock()
    server._ws_rpc_reply = AsyncMock()

    await server._ws_rpc_sessions_delete(ws, "rpc-2", {"sessionKey": key})

    server.on_session_delete.assert_awaited_once_with(key)
    server.sessions.delete.assert_not_called()
    server._ws_rpc_reply.assert_awaited_once_with(
        ws, "rpc-2", {"deleted": True, "sessionKey": key}
    )
    assert cleared == [key]


@pytest.mark.asyncio
async def test_gateway_maps_owner_side_delete_race_to_busy_error(monkeypatch):
    key = "cli:raced-owner-delete"
    cleared: list[str] = []
    monkeypatch.setattr(
        "flowly.runtime_cwd.clear_session_cwd",
        lambda value: cleared.append(value),
    )
    ws = object()
    server = object.__new__(GatewayServer)
    server.sessions = SimpleNamespace(delete=Mock(return_value=False))
    server.on_session_delete = AsyncMock(side_effect=SessionBusyError(key))
    server._ws_rpc_error = AsyncMock()
    server._ws_rpc_reply = AsyncMock()

    await server._ws_rpc_sessions_delete(ws, "rpc-race", {"sessionKey": key})

    server._ws_rpc_error.assert_awaited_once_with(
        ws,
        "rpc-race",
        "SESSION_BUSY",
        "Finish or stop the active turn before deleting this session.",
    )
    server._ws_rpc_reply.assert_not_awaited()
    server.sessions.delete.assert_not_called()
    assert cleared == []
