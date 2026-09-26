"""Lifecycle retries cannot create another browser or stop a replacement."""

import json
import sqlite3
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from flowly.desktop_runtime import lifecycle
from flowly.desktop_runtime.authority import ScreenError
from flowly.desktop_runtime.lifecycle import ScreenLifecycle

IDENTITY = {"hostId": "host", "botId": "bot", "profileId": "default"}


@pytest.fixture
def operations(tmp_path, monkeypatch):
    monkeypatch.setattr(lifecycle, "process_identity", lambda _: {"pid": 42, "start": "1", "boot": "boot"})
    monkeypatch.setattr(lifecycle, "is_alive", lambda _: True)
    runtime = SimpleNamespace(directory=tmp_path, start=Mock(return_value={"generation": "new"}), stop=Mock())
    return ScreenLifecycle(runtime, IDENTITY, Mock())


def finished(operations, operation="operation"):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        result = operations.status(operation)
        if result["state"] not in {"pending", "running"}:
            return result
        time.sleep(0.005)
    pytest.fail("lifecycle worker did not finish")


def test_durable_retry_does_not_restart_after_stop(operations):
    operations.submit("start", None, "operation")
    result = finished(operations)
    assert result["state"] == "succeeded" and result["resultGeneration"] == "new"
    operations.submit("stop", "new", "stop")
    assert finished(operations, "stop")["state"] == "succeeded"
    replacement = ScreenLifecycle(operations.runtime, IDENTITY, Mock())
    assert replacement.submit("start", None, "operation") == result
    assert replacement.status("operation") == result
    operations.runtime.start.assert_called_once_with(expected_generation=None, identity_guard=operations.guard)
    operations.runtime.stop.assert_called_once_with("new", identity_guard=operations.guard)
    assert set(result) == {"hostId", "botId", "profileId", "operationId", "action", "generation", "resultGeneration", "state", "error"}
    assert operations.path.stat().st_mode & 0o777 == 0o600


def test_pending_request_has_one_worker_across_controllers(operations):
    entered, release = threading.Event(), threading.Event()
    def start(**kwargs):
        entered.set()
        assert release.wait(3)
        return {"generation": "new"}
    operations.runtime.start.side_effect = start
    try:
        assert operations.submit("start", None, "operation")["state"] == "pending"
        assert entered.wait(2)
        replacement = ScreenLifecycle(operations.runtime, IDENTITY, Mock())
        assert replacement.submit("start", None, "operation")["state"] == "running"
        with pytest.raises(ScreenError, match="LIFECYCLE_BUSY"):
            replacement.submit("start", None, "another")
        with pytest.raises(ScreenError, match="OPERATION_CONFLICT"):
            replacement.submit("stop", "new", "operation")
    finally:
        release.set()
        assert finished(operations)["state"] == "succeeded"
    operations.runtime.start.assert_called_once()


@pytest.mark.parametrize("code", ["HUMAN_HAS_CONTROL", "SCREEN_BUSY", "STALE_GENERATION"])
def test_runtime_refusal_is_durable_and_never_retried(operations, code):
    operations.runtime.stop.side_effect = ScreenError(code)
    operations.submit("stop", "old", "operation")
    result = finished(operations)
    assert result["state"] == "failed" and result["error"] == code
    assert operations.submit("stop", "old", "operation") == result
    operations.runtime.stop.assert_called_once()


def test_identity_is_rechecked_before_mutation_and_private_errors_are_hidden(operations):
    operations.guard.side_effect = ScreenError("PROFILE_IDENTITY_CHANGED")
    operations.submit("start", None, "operation")
    assert finished(operations)["error"] == "PROFILE_IDENTITY_CHANGED"
    operations.runtime.start.assert_not_called()
    operations.guard.side_effect = RuntimeError("private filesystem/credential detail")
    operations.submit("start", None, "next")
    result = finished(operations, "next")
    assert result["error"] == "CONTROL_UNAVAILABLE" and "private" not in str(result)


def test_lost_worker_and_elapsed_deadline_require_recovery_without_replay(operations, monkeypatch):
    class HeldExecutor:
        def submit(self, *args):
            self.args = args
    executor = HeldExecutor()
    monkeypatch.setattr(lifecycle, "_EXECUTOR", executor)
    try:
        operations.submit("start", None, "operation")
        monkeypatch.setattr(lifecycle, "is_alive", lambda _: False)
        result = operations.status("operation")
        assert result["state"] == "recovery_required"
        assert operations.submit("start", None, "operation") == result
        with pytest.raises(ScreenError, match="LIFECYCLE_BUSY"):
            operations.submit("start", None, "new")
        monkeypatch.setattr(lifecycle, "is_alive", lambda _: True)
        monkeypatch.setattr(lifecycle.time, "time", lambda: 2**52)
        assert operations.status("operation")["state"] == "recovery_required"
        operations.runtime.start.assert_not_called()
    finally:
        executor.args[0](*executor.args[1:])  # Retire the accepted worker/slot.


def test_capacity_refusal_does_not_leave_a_reserved_job(operations, monkeypatch):
    monkeypatch.setattr(lifecycle, "_SLOTS", threading.BoundedSemaphore(0))
    with pytest.raises(ScreenError, match="LIFECYCLE_BUSY"):
        operations.submit("start", None, "operation")
    with pytest.raises(ScreenError, match="OPERATION_NOT_FOUND"):
        operations.status("operation")


def test_receipts_are_bounded_and_changed_identity_cannot_read_them(operations):
    for index in range(130):
        key = str(index)
        operations.submit("start", None, key)
        assert finished(operations, key)["state"] == "succeeded"
    with sqlite3.connect(operations.path) as db:
        assert db.execute("SELECT count(*) FROM operations").fetchone()[0] == 128
    with pytest.raises(ScreenError, match="OPERATION_NOT_FOUND"):
        operations.status("0")
    replacement = ScreenLifecycle(operations.runtime, {**IDENTITY, "botId": "replacement"}, Mock())
    with pytest.raises(ScreenError, match="PROFILE_IDENTITY_CHANGED"):
        replacement.status("129")
    with pytest.raises(ScreenError, match="OPERATION_CONFLICT"):
        replacement.submit("start", None, "129")


def test_corrupt_receipt_never_launches_a_worker(operations):
    operations.submit("start", None, "operation")
    finished(operations)
    with sqlite3.connect(operations.path) as db:
        raw = json.loads(db.execute("SELECT value FROM operations").fetchone()[0])
        raw["operationId"] = "forged"
        db.execute("UPDATE operations SET value=?", (json.dumps(raw),))
    with pytest.raises(ScreenError, match="LIFECYCLE_STATE_INVALID"):
        operations.submit("start", None, "another")
    operations.runtime.start.assert_called_once()
