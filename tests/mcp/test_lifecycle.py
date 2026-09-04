"""MCP connection lifecycle policy and recovery regression tests."""

from __future__ import annotations

import asyncio

import pytest

from flowly.mcp import client
from flowly.mcp.lifecycle import (
    MCPConnectionState,
    MCPRetryPolicy,
    is_transport_failure,
)


def test_retry_policy_uses_bounded_exponential_backoff_without_jitter():
    policy = MCPRetryPolicy(
        reconnect_base_delay=0.5,
        reconnect_max_delay=3.0,
        reconnect_jitter=0.0,
        park_after_attempts=10,
        parked_probe_interval=60.0,
    )

    assert [policy.delay_for(attempt) for attempt in range(1, 6)] == [
        0.5,
        1.0,
        2.0,
        3.0,
        3.0,
    ]


def test_retry_policy_parks_after_burst_budget():
    policy = MCPRetryPolicy(
        reconnect_base_delay=0.01,
        reconnect_max_delay=1.0,
        reconnect_jitter=0.0,
        park_after_attempts=3,
        parked_probe_interval=90.0,
    )

    assert policy.delay_for(2) == 0.02
    assert policy.delay_for(3) == 90.0
    assert policy.state_for(2) is MCPConnectionState.RECONNECTING
    assert policy.state_for(3) is MCPConnectionState.PARKED


@pytest.mark.parametrize(
    "exc",
    [
        ConnectionError("connection reset"),
        BrokenPipeError("closed"),
        EOFError("eof"),
        RuntimeError("MCP session is closed"),
        ExceptionGroup("transport", [OSError("broken pipe")]),
    ],
)
def test_transport_failures_are_classified(exc):
    assert is_transport_failure(exc) is True


def test_protocol_application_errors_do_not_force_reconnect():
    assert is_transport_failure(ValueError("invalid tool arguments")) is False
    assert is_transport_failure(RuntimeError("tool execution failed")) is False


@pytest.mark.asyncio
async def test_connected_server_reconnects_after_transport_failure(monkeypatch):
    task = client.MCPServerTask("recovering")
    attempts = 0
    second_connection = asyncio.Event()

    async def run_transport() -> None:
        nonlocal attempts
        attempts += 1
        task._mark_connected()
        if attempts == 1:
            raise ConnectionError("connection reset")
        second_connection.set()
        assert task.shutdown_event is not None
        await task.shutdown_event.wait()

    monkeypatch.setattr(task, "_run_transport", run_transport)

    await task.start({
        "command": "ignored",
        "connect_timeout": 1,
        "lifecycle": {
            "reconnect_enabled": True,
            "reconnect_base_delay": 0.001,
            "reconnect_max_delay": 0.001,
            "reconnect_jitter": 0.0,
            "park_after_attempts": 10,
            "parked_probe_interval": 1.0,
        },
    })

    await asyncio.wait_for(second_connection.wait(), timeout=1)
    assert attempts == 2
    assert task.state is MCPConnectionState.CONNECTED
    assert task.health_snapshot()["reconnectCount"] == 1

    await task.shutdown()
    assert task.state is MCPConnectionState.STOPPED


@pytest.mark.asyncio
async def test_shutdown_interrupts_reconnect_backoff(monkeypatch):
    task = client.MCPServerTask("stopping")

    async def run_transport() -> None:
        task._mark_connected()
        raise ConnectionError("offline")

    monkeypatch.setattr(task, "_run_transport", run_transport)

    await task.start({
        "command": "ignored",
        "connect_timeout": 1,
        "lifecycle": {
            "reconnect_enabled": True,
            "reconnect_base_delay": 300.0,
            "reconnect_max_delay": 300.0,
            "reconnect_jitter": 0.0,
            "park_after_attempts": 10,
            "parked_probe_interval": 300.0,
        },
    })
    await asyncio.sleep(0)

    await asyncio.wait_for(task.shutdown(), timeout=0.2)
    assert task.state is MCPConnectionState.STOPPED


@pytest.mark.asyncio
async def test_keepalive_failure_is_propagated_to_connection_supervisor(monkeypatch):
    task = client.MCPServerTask("keepalive")
    task.ready = asyncio.Event()
    task.shutdown_event = asyncio.Event()
    task.connection_failed_event = asyncio.Event()
    task.rpc_lock = asyncio.Lock()
    task._retry_policy = MCPRetryPolicy(
        keepalive_interval=0.001,
        keepalive_timeout=0.05,
    )

    class Session:
        async def initialize(self):
            return type("Init", (), {"capabilities": None})()

        async def list_tools(self):
            if task.ready.is_set():
                raise ConnectionError("keepalive failed")
            return type("Tools", (), {"tools": []})()

    with pytest.raises(ConnectionError, match="keepalive failed"):
        await asyncio.wait_for(task._serve(Session()), timeout=0.2)

    assert task.session is None


@pytest.mark.asyncio
async def test_late_failure_from_replaced_session_is_ignored():
    task = client.MCPServerTask("generation-safe")
    task.connection_failed_event = asyncio.Event()
    old_session = object()
    current_session = object()
    task.session = current_session

    task.report_transport_failure(ConnectionError("late"), old_session)
    assert task.connection_failed_event.is_set() is False

    task.report_transport_failure(ConnectionError("current"), current_session)
    assert task.connection_failed_event.is_set() is True


def test_runtime_health_snapshot_is_public_and_credential_free(monkeypatch):
    task = client.MCPServerTask("visible")
    task._config = {
        "headers": {"Authorization": "Bearer secret"},
        "env": {"TOKEN": "secret"},
    }
    task._set_state(MCPConnectionState.PARKED)
    monkeypatch.setitem(client._servers, "visible", task)

    health = client.get_mcp_server_health()["visible"]

    assert health["state"] == "parked"
    assert "config" not in health
    assert "secret" not in str(health)


@pytest.mark.asyncio
async def test_transport_exception_notification_wakes_supervisor():
    task = client.MCPServerTask("notified")
    task.connection_failed_event = asyncio.Event()
    task.session = object()

    await task._make_message_handler()(ConnectionError("stream closed"))

    assert task.connection_failed_event.is_set() is True
