"""Installation jobs preserve identity, admission bounds and uncertain outcomes."""

import json
import os
import sqlite3
import threading
import time
from unittest.mock import Mock

import pytest

from flowly.desktop_runtime import install_jobs as jobs
from flowly.desktop_runtime.authority import ScreenError

IDENTITY = {"hostId": "host", "botId": "bot", "profileId": "default"}
OP = "1" * 32
PLAN = "a" * 64
pytestmark = pytest.mark.skipif(os.name == "nt", reason="private POSIX installation state")


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    root = tmp_path / "environment"
    root.mkdir()
    monkeypatch.setattr(jobs, "state_root", lambda: root / "state")
    monkeypatch.setattr(jobs, "process_identity", lambda _: {"pid": 42, "start": "1", "boot": "boot"})
    monkeypatch.setattr(jobs, "is_alive", lambda _: True)
    plan = {"planId": PLAN, "runtimeAccountRequired": False,
            "system": {"planId": "b" * 64, "supported": True},
            "python": {"planId": "c" * 64, "supported": True}}
    monkeypatch.setattr(jobs, "installation_plan", Mock(return_value=plan))
    monkeypatch.setattr(jobs, "install_system", Mock(return_value={"state": "succeeded", "error": None}))
    monkeypatch.setattr(jobs, "install_python", Mock(return_value={"state": "succeeded", "error": None, "restartRequired": True}))
    return jobs.ScreenInstallJobs(IDENTITY, Mock()), plan


def finished(service, operation=OP):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        result = service.status(operation)
        if result["state"] not in {"pending", "running"}:
            return result
        time.sleep(0.005)
    pytest.fail("installation worker did not finish")


def test_status_before_admission_is_read_only(fixture):
    service, _ = fixture
    with pytest.raises(ScreenError, match="OPERATION_NOT_FOUND"):
        service.status(OP)
    assert not service.root.exists()


def test_result_survives_controller_recreation_and_never_replays(fixture):
    service, _ = fixture
    service.submit(PLAN, OP, password="synthetic-secret")
    result = finished(service)
    assert result["state"] == "succeeded" and result["restartRequired"]
    assert result["completedSteps"] == ["system", "python"]
    replacement = jobs.ScreenInstallJobs(IDENTITY, Mock())
    jobs.installation_plan.side_effect = AssertionError("retries use receipts, not a new plan")
    assert replacement.submit(PLAN, OP, password="different-secret") == result
    jobs.install_system.assert_called_once_with("b" * 64, OP, password="synthetic-secret")
    jobs.install_python.assert_called_once_with("c" * 64, OP)
    assert "owner" not in result and "deadline" not in result
    assert "secret" not in json.dumps(result)
    assert b"secret" not in service.path.read_bytes()
    assert service.path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(ScreenError, match="OPERATION_CONFLICT"):
        replacement.submit("d" * 64, OP)


def test_profiles_share_one_environment_job_and_scope_receipts(fixture):
    service, _ = fixture
    entered, release = threading.Event(), threading.Event()
    def install(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        return {"state": "succeeded", "error": None}
    jobs.install_system.side_effect = install
    try:
        service.submit(PLAN, OP)
        assert entered.wait(2)
        sibling = jobs.ScreenInstallJobs({**IDENTITY, "botId": "other", "profileId": "writer"}, Mock())
        with pytest.raises(ScreenError, match="PROFILE_IDENTITY_CHANGED"):
            sibling.status(OP)
        with pytest.raises(ScreenError, match="INSTALL_BUSY"):
            sibling.submit(PLAN, "2" * 32)
        assert service.submit(PLAN, OP)["state"] == "running"
    finally:
        release.set()
        assert finished(service)["state"] == "succeeded"
    jobs.install_system.assert_called_once()


@pytest.mark.parametrize("change,error", [("root", "INSTALL_RUNTIME_ACCOUNT_REQUIRED"),
    ("system", "INSTALL_UNSUPPORTED_HOST"), ("python", "INSTALL_VIRTUALENV_REQUIRED"), ("plan", "INSTALL_PLAN_CHANGED")])
def test_ineligible_or_changed_plans_never_create_jobs(fixture, change, error):
    service, plan = fixture
    if change == "root":
        plan["runtimeAccountRequired"] = True
    elif change == "plan":
        plan["planId"] = "d" * 64
    else:
        plan[change].update(supported=False, reason=error)
    with pytest.raises(ScreenError, match=error):
        service.submit(PLAN, OP)
    assert not service.root.exists()
    jobs.install_system.assert_not_called()


@pytest.mark.parametrize("password", [123, "bad\n", "bad\x00", "x" * 4097, "ü" * 3000, "\ud800"])
def test_invalid_password_never_creates_receipt(fixture, password):
    service, _ = fixture
    with pytest.raises(ScreenError, match="INVALID_PARAMS"):
        service.submit(PLAN, OP, password=password)
    assert not service.root.exists()


def test_partial_python_failure_blocks_new_installations_and_hides_details(fixture):
    service, _ = fixture
    jobs.install_python.side_effect = RuntimeError("private token and path")
    service.submit(PLAN, OP)
    result = finished(service)
    assert result["state"] == "recovery_required"
    assert result["phase"] == "python" and result["completedSteps"] == ["system"]
    assert result["restartRequired"] and "private" not in str(result)
    assert service.submit(PLAN, OP) == result
    with pytest.raises(ScreenError, match="INSTALL_BUSY"):
        service.submit(PLAN, "2" * 32)
    jobs.install_python.assert_called_once()


def test_identity_changes_between_steps_stop_before_python(fixture):
    service, _ = fixture
    service.guard.side_effect = [None, None, ScreenError("PROFILE_IDENTITY_CHANGED")]
    service.submit(PLAN, OP)
    result = finished(service)
    assert result["completedSteps"] == ["system"] and result["state"] == "recovery_required"
    jobs.install_python.assert_not_called()


def test_lost_or_expired_owner_never_replays(fixture, monkeypatch):
    service, _ = fixture
    executor = Mock()
    monkeypatch.setattr(jobs, "_EXECUTOR", executor)
    try:
        assert service.submit(PLAN, OP)["state"] == "pending"
        monkeypatch.setattr(jobs, "is_alive", lambda _: False)
        assert service.status(OP)["state"] == "recovery_required"
        assert service.submit(PLAN, OP)["state"] == "recovery_required"
        with pytest.raises(ScreenError, match="INSTALL_BUSY"):
            service.submit(PLAN, "2" * 32)
        jobs.install_system.assert_not_called()
    finally:
        # Execute the intercepted worker only to release its accepted slot.
        function, *args = executor.submit.call_args.args
        function(*args)


def test_history_is_not_pruned_and_malformed_storage_fails_closed(fixture):
    service, _ = fixture
    service.submit(PLAN, OP)
    finished(service)
    with sqlite3.connect(service.path) as db:
        raw = json.loads(db.execute("SELECT value FROM operations").fetchone()[0])
        for index in range(2, 129):
            identifier = f"{index:032x}"
            db.execute("INSERT INTO operations VALUES (?, ?)", (identifier, json.dumps({**raw, "operationId": identifier})))
    with pytest.raises(ScreenError, match="INSTALL_HISTORY_FULL"):
        service.submit(PLAN, "f" * 32)
    assert service.submit(PLAN, OP)["state"] == "succeeded"
    with sqlite3.connect(service.path) as db:
        db.execute("UPDATE operations SET value=? WHERE id=?", ('{"password":"private"}', OP))
    with pytest.raises(ScreenError, match="INSTALL_STATE_INVALID"):
        service.status(OP)


def test_same_version_python_step_preserves_no_restart_result(fixture):
    service, _ = fixture
    jobs.install_python.return_value["restartRequired"] = False
    service.submit(PLAN, OP)
    assert not finished(service)["restartRequired"]


@pytest.mark.parametrize("accepted_receipt", [False, True])
def test_privilege_errors_are_retryable_only_before_worker_acceptance(fixture, monkeypatch, accepted_receipt):
    service, _ = fixture
    jobs.install_system.side_effect = ScreenError("INSTALL_PRIVILEGE_REQUIRED")
    status = Mock(return_value={"state": "running"})
    if not accepted_receipt:
        status.side_effect = ScreenError("OPERATION_NOT_FOUND")
    monkeypatch.setattr(jobs, "install_status", status)
    service.submit(PLAN, OP, password="wrong-synthetic-password")
    result = finished(service)
    assert result["state"] == ("recovery_required" if accepted_receipt else "failed")
    jobs.install_python.assert_not_called()
    if accepted_receipt:
        with pytest.raises(ScreenError, match="INSTALL_BUSY"):
            service.submit(PLAN, "2" * 32)
    else:
        jobs.install_system.side_effect = None
        service.submit(PLAN, "2" * 32, password="correct-synthetic-password")
        assert finished(service, "2" * 32)["state"] == "succeeded"


def test_executor_rejection_is_durable_without_executing_packages(fixture, monkeypatch):
    service, _ = fixture
    executor = Mock()
    executor.submit.side_effect = RuntimeError("private executor context")
    monkeypatch.setattr(jobs, "_EXECUTOR", executor)
    with pytest.raises(ScreenError, match="CONTROL_UNAVAILABLE"):
        service.submit(PLAN, OP)
    assert service.status(OP)["state"] == "failed"
    assert service.submit(PLAN, OP)["error"] == "CONTROL_UNAVAILABLE"
    jobs.install_system.assert_not_called()


def test_observation_deadline_is_not_permission_to_replay(fixture, monkeypatch):
    service, _ = fixture
    executor = Mock()
    monkeypatch.setattr(jobs, "_EXECUTOR", executor)
    try:
        service.submit(PLAN, OP)
        now = time.time()
        monkeypatch.setattr(jobs.time, "time", lambda: now + 1261)
        assert service.status(OP)["state"] == "recovery_required"
        assert service.submit(PLAN, OP)["state"] == "recovery_required"
        executor.submit.assert_called_once()
    finally:
        function, *args = executor.submit.call_args.args
        function(*args)
    jobs.install_system.assert_not_called()
    assert service.status(OP)["error"] == "INSTALL_TIMEOUT"


def test_capacity_rejection_does_not_leave_an_unusable_new_store(fixture, monkeypatch, tmp_path):
    service, _ = fixture
    entered, release = threading.Event(), threading.Event()
    def install(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        return {"state": "succeeded", "error": None}
    jobs.install_system.side_effect = install
    try:
        service.submit(PLAN, OP)
        assert entered.wait(2)
        monkeypatch.setattr(jobs, "state_root", lambda: tmp_path / "other-environment-state")
        other = jobs.ScreenInstallJobs(IDENTITY, Mock())
        with pytest.raises(ScreenError, match="INSTALL_BUSY"):
            other.submit(PLAN, "2" * 32)
        with pytest.raises(ScreenError, match="OPERATION_NOT_FOUND"):
            other.status("2" * 32)
    finally:
        release.set()
        assert finished(service)["state"] == "succeeded"
    deadline = time.monotonic() + 2
    while True:
        try:
            other.submit(PLAN, "2" * 32)
            break
        except ScreenError as exc:
            assert exc.code == "INSTALL_BUSY" and time.monotonic() < deadline
            time.sleep(0.005)
    assert finished(other, "2" * 32)["state"] == "succeeded"


def test_state_directory_symlink_cannot_redirect_admission(fixture, tmp_path):
    service, _ = fixture
    external = tmp_path / "external"
    external.mkdir()
    service.root.symlink_to(external, target_is_directory=True)
    with pytest.raises(ScreenError, match="INSTALL_STATE_INVALID"):
        service.submit(PLAN, OP)
    assert list(external.iterdir()) == []
    jobs.install_system.assert_not_called()


def test_publicly_readable_database_is_rejected(fixture):
    service, _ = fixture
    service.submit(PLAN, OP)
    finished(service)
    service.path.chmod(0o644)
    with pytest.raises(ScreenError, match="INSTALL_STATE_INVALID"):
        service.status(OP)
