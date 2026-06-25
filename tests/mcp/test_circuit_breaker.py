"""Tests for the MCP circuit breaker (T10).

The breaker keeps the model from hammering a server that has failed
repeatedly. We exercise the state machine directly with a monkeypatched
clock so the cooldown is deterministic:

* Below threshold → no block.
* At/above threshold within cooldown → block message with countdown.
* After cooldown → half-open (no block) so one probe can go through.
* A success resets the counter entirely.
"""

from __future__ import annotations

import pytest

import flowly.mcp.client as client


@pytest.fixture(autouse=True)
def clean_breaker():
    client._server_error_counts.clear()
    client._server_breaker_opened_at.clear()
    yield
    client._server_error_counts.clear()
    client._server_breaker_opened_at.clear()


def test_below_threshold_no_block():
    for _ in range(client._CIRCUIT_BREAKER_THRESHOLD - 1):
        client._bump_server_error("srv")
    assert client.circuit_breaker_block_reason("srv") is None


def test_at_threshold_blocks(monkeypatch):
    import time as _time
    clock = {"t": 1000.0}
    monkeypatch.setattr(_time, "monotonic", lambda: clock["t"])

    for _ in range(client._CIRCUIT_BREAKER_THRESHOLD):
        client._bump_server_error("srv")
    reason = client.circuit_breaker_block_reason("srv")
    assert reason is not None
    assert "unreachable" in reason
    assert "srv" in reason


def test_half_open_after_cooldown(monkeypatch):
    import time as _time
    clock = {"t": 1000.0}
    monkeypatch.setattr(_time, "monotonic", lambda: clock["t"])

    for _ in range(client._CIRCUIT_BREAKER_THRESHOLD):
        client._bump_server_error("srv")
    assert client.circuit_breaker_block_reason("srv") is not None

    # Advance past the cooldown — breaker goes half-open (no block).
    clock["t"] += client._CIRCUIT_BREAKER_COOLDOWN_SEC + 1
    assert client.circuit_breaker_block_reason("srv") is None


def test_reset_clears_state():
    for _ in range(client._CIRCUIT_BREAKER_THRESHOLD):
        client._bump_server_error("srv")
    assert client.circuit_breaker_block_reason("srv") is not None
    client._reset_server_error("srv")
    assert client.circuit_breaker_block_reason("srv") is None
    assert "srv" not in client._server_error_counts
