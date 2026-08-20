"""Explicit Coach session ids keep WebSocket ownership and cleanup semantics."""

from unittest.mock import AsyncMock, Mock

import pytest

from flowly.gateway.server import GatewayServer


@pytest.mark.asyncio
async def test_disconnect_stops_explicit_coaching_session():
    server = GatewayServer()
    manager = Mock()
    manager.is_active.side_effect = lambda session_id: session_id == "coach-123"
    manager.stop = AsyncMock(return_value={"status": "stopped"})
    server._coaching_manager = manager
    server._track_coaching_session("desktop-1", "coach-123")

    await server._stop_client_coaching_sessions("desktop-1")

    manager.stop.assert_awaited_once_with("coach-123", background_finalize=True)
    assert "desktop-1" not in server._coaching_sessions_by_client


def test_manual_stop_removes_explicit_session_ownership():
    server = GatewayServer()
    server._track_coaching_session("desktop-1", "coach-123")

    server._untrack_coaching_session("desktop-1", "coach-123")

    assert "desktop-1" not in server._coaching_sessions_by_client
