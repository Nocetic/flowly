"""Tests for transport routing (T3) and orphan reaping (S7), Faz 2c."""

from __future__ import annotations

import subprocess
import sys
import time

import pytest

import flowly.mcp.client as client
import flowly.mcp.proc as proc


# ── transport routing ──────────────────────────────────────────────


def test_use_sse_only_for_sse_transport():
    task = client.MCPServerTask("s")
    task._config = {"url": "https://x/mcp", "transport": "sse"}
    assert task._use_sse() is True

    task._config = {"url": "https://x/mcp", "transport": "http"}
    assert task._use_sse() is False

    task._config = {"url": "https://x/mcp", "transport": "auto"}
    assert task._use_sse() is False

    task._config = {"url": "https://x/mcp"}
    assert task._use_sse() is False


def test_is_http_detects_url():
    task = client.MCPServerTask("s")
    task._config = {"url": "https://x/mcp"}
    assert task.is_http() is True
    task._config = {"command": "echo"}
    assert task.is_http() is False


# ── orphan reaping ──────────────────────────────────────────────────


def test_snapshot_returns_set():
    assert isinstance(proc.snapshot_child_pids(), set)


def test_reap_empty_is_noop():
    # Must never raise on empty or already-dead PIDs.
    proc.reap_pids(set())
    proc.reap_pids({999999999})


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only reap test")
def test_reap_kills_a_real_child():
    # Spawn a long sleep we own, confirm it's our child, reap it.
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        time.sleep(0.3)
        children = proc.snapshot_child_pids()
        assert child.pid in children, "spawned child not seen in snapshot"

        proc.reap_pids({child.pid}, "test")

        # Reaped — process should be gone within a moment.
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if child.poll() is not None:
                break
            time.sleep(0.1)
        assert child.poll() is not None, "child survived reap"
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)


def test_reap_ignores_pids_not_passed():
    # A child we DON'T pass to reap_pids must survive.
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"])
    try:
        time.sleep(0.3)
        proc.reap_pids({999999998}, "test")  # unrelated, nonexistent
        assert child.poll() is None, "reap touched a PID it wasn't given"
    finally:
        child.kill()
        child.wait(timeout=5)
