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
    monkeypatch.setattr(jobs, "_restart_boundary", lambda owner: {"boot": owner["boot"], "ticks": 100})
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


@pytest.mark.parametrize("change,error", [("system", "INSTALL_UNSUPPORTED_HOST"), ("python", "INSTALL_VIRTUALENV_REQUIRED"), ("plan", "INSTALL_PLAN_CHANGED")])
def test_ineligible_or_changed_plans_never_create_jobs(fixture, change, error):
    service, plan = fixture
    if change == "plan":
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


def test_partial_python_failure_is_never_replayed_and_hides_details(fixture):
    service, _ = fixture
    jobs.install_python.side_effect = RuntimeError("private token and path")
    service.submit(PLAN, OP)
    result = finished(service)
    assert result["state"] == "recovery_required"
    assert result["phase"] == "python" and result["completedSteps"] == ["system"]
    assert result["restartRequired"] and "private" not in str(result)
    # The same operation reports what happened; it never runs again.
    assert service.submit(PLAN, OP) == result
    jobs.install_python.assert_called_once()
    with pytest.raises(ScreenError, match="INSTALL_RECOVERY_REQUIRED"):
        jobs.assert_environment_installation_idle()


def test_a_new_operation_repairs_an_uncertain_python_step_and_requires_a_restart(fixture, monkeypatch):
    service, _ = fixture
    jobs.install_python.side_effect = RuntimeError("interrupted")
    service.submit(PLAN, OP)
    assert finished(service)["state"] == "recovery_required"
    # The repair finds nothing left to install, yet modules may have changed
    # under the running process: it must still be restarted.
    jobs.install_python.side_effect = None
    jobs.install_python.return_value = {"state": "succeeded", "error": None, "restartRequired": False}
    service.submit(PLAN, "2" * 32)
    repaired = finished(service, "2" * 32)
    assert repaired["state"] == "succeeded" and repaired["restartRequired"] is True
    with pytest.raises(ScreenError, match="INSTALL_RESTART_REQUIRED"):
        jobs.assert_environment_installation_idle()
    monkeypatch.setattr(jobs, "process_identity", lambda _: {"pid": 43, "start": "101", "boot": "boot"})
    jobs.assert_environment_installation_idle()
    # History is kept as it happened.
    assert service.status(OP)["state"] == "recovery_required"


@pytest.mark.parametrize("receipt", [
    {"state": "recovery_required", "error": "INSTALL_PACKAGES_FAILED"},
    {"state": "failed", "error": "INSTALL_INDEX_FAILED"},
])
def test_system_package_trouble_never_stops_agent_turns(fixture, receipt):
    service, _ = fixture
    jobs.install_system.return_value = receipt
    service.submit(PLAN, OP)
    result = finished(service)
    # A worker that changed nothing reports a plain failure; anything uncertain needs repair.
    assert result["state"] == ("failed" if receipt["state"] == "failed" else "recovery_required")
    assert result["error"] == receipt["error"] and result["phase"] == "system"
    jobs.install_python.assert_not_called()
    jobs.assert_environment_installation_idle()  # the agent's own environment was not touched
    jobs.install_system.return_value = {"state": "succeeded", "error": None}
    service.submit(PLAN, "2" * 32)
    assert finished(service, "2" * 32)["state"] == "succeeded"


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
        jobs.install_system.assert_not_called()
        # A lost job that never reached the agent's environment does not stop its turns.
        jobs.assert_environment_installation_idle()
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
    # An accepted worker that went silent (killed with its service) was
    # interrupted, not refused: the record says so (seen live).
    assert result["error"] == ("INSTALL_RECOVERY_REQUIRED" if accepted_receipt else "INSTALL_PRIVILEGE_REQUIRED")
    jobs.install_python.assert_not_called()
    # Either way a new, explicit operation may follow; an uncertain one repairs.
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
        monkeypatch.setattr(jobs.time, "time", lambda: now + jobs.JOB_SECONDS + 1)
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


def test_root_plan_uses_system_worker_for_account_provisioning(fixture):
    service, plan = fixture
    plan["runtimeAccountRequired"] = True
    service.submit(PLAN, OP)
    result = finished(service)
    assert result["state"] == "succeeded"
    jobs.install_system.assert_called_once_with("b" * 64, OP, password=None)


def test_live_screen_blocks_job_without_receipt_or_package_execution(fixture):
    from flowly.desktop_runtime.installation_gate import environment_activity

    service, _ = fixture
    with environment_activity():
        with pytest.raises(ScreenError, match="INSTALL_BUSY"):
            service.submit(PLAN, OP)
    assert not service.path.exists()
    jobs.install_system.assert_not_called()


def test_worker_keeps_exclusive_reservation_after_submit_returns(fixture):
    from flowly.desktop_runtime.installation_gate import environment_activity

    service, _ = fixture
    entered, release = threading.Event(), threading.Event()
    def system(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        return {"state": "succeeded", "error": None}
    jobs.install_system.side_effect = system
    try:
        service.submit(PLAN, OP)
        assert entered.wait(2)
        with pytest.raises(ScreenError, match="INSTALL_BUSY"):
            with environment_activity():
                pytest.fail("screen entered accepted installation")
    finally:
        release.set()
        assert finished(service)["state"] == "succeeded"
    # Terminal publication precedes descriptor close. Wait only for that bounded
    # worker cleanup, rather than mistaking the public receipt for lock ownership.
    deadline = time.monotonic() + 2
    while True:
        try:
            with environment_activity():
                break
        except ScreenError:
            assert time.monotonic() < deadline
            time.sleep(.005)


def test_recovery_receipt_blocks_environment_after_live_reservation_ends(fixture):
    service, _ = fixture
    jobs.assert_environment_installation_idle()
    assert not service.root.exists()
    jobs.install_python.side_effect = RuntimeError("private failure")
    service.submit(PLAN, OP)
    assert finished(service)["state"] == "recovery_required"
    with pytest.raises(ScreenError, match="INSTALL_RECOVERY_REQUIRED"):
        jobs.assert_environment_installation_idle()


def test_rejected_executor_releases_reservation(fixture, monkeypatch):
    from flowly.desktop_runtime.installation_gate import environment_activity

    service, _ = fixture
    monkeypatch.setattr(jobs, "_EXECUTOR", Mock(submit=Mock(side_effect=RuntimeError())))
    with pytest.raises(ScreenError, match="CONTROL_UNAVAILABLE"):
        service.submit(PLAN, OP)
    with environment_activity():
        jobs.assert_environment_installation_idle()


@pytest.mark.parametrize("current,allowed", [
    ({"pid": 42, "start": "1", "boot": "boot"}, False),
    ({"pid": 99, "start": "99", "boot": "boot"}, False),
    ({"pid": 99, "start": "100", "boot": "boot"}, False),
    ({"pid": 42, "start": "101", "boot": "boot"}, True),
    ({"pid": 42, "start": "1", "boot": "new-boot"}, True),
    (None, False),
])
def test_screen_requires_process_start_after_package_completion(fixture, monkeypatch, current, allowed):
    service, _ = fixture
    service.submit(PLAN, OP)
    result = finished(service)
    assert result["state"] == "succeeded"
    assert "restartBoundary" not in result
    monkeypatch.setattr(jobs, "process_identity", lambda _: current)
    if allowed:
        jobs.assert_environment_installation_idle()
    else:
        with pytest.raises(ScreenError, match="INSTALL_RESTART_REQUIRED"):
            jobs.assert_environment_installation_idle()
    # Process verification never edits historical receipts or replays installers.
    assert service.status(OP) == result
    jobs.install_python.assert_called_once()


def test_no_package_changes_do_not_require_restart(fixture):
    service, _ = fixture
    jobs.install_python.return_value["restartRequired"] = False
    service.submit(PLAN, OP)
    assert finished(service)["state"] == "succeeded"
    jobs.assert_environment_installation_idle()


def test_legacy_success_without_completion_boundary_needs_recovery(fixture):
    service, _ = fixture
    service.submit(PLAN, OP)
    finished(service)
    with sqlite3.connect(service.path) as db:
        row = json.loads(db.execute("SELECT value FROM operations").fetchone()[0])
        del row["restartBoundary"]
        db.execute("UPDATE operations SET value=?", (json.dumps(row),))
    assert service.status(OP)["state"] == "succeeded"
    with pytest.raises(ScreenError, match="INSTALL_RECOVERY_REQUIRED"):
        jobs.assert_environment_installation_idle()


def test_failure_to_record_completion_boundary_needs_recovery(fixture, monkeypatch):
    service, _ = fixture
    monkeypatch.setattr(jobs, "_restart_boundary", Mock(side_effect=ScreenError("INSTALL_RECOVERY_REQUIRED")))
    service.submit(PLAN, OP)
    assert finished(service)["state"] == "recovery_required"
    with pytest.raises(ScreenError, match="INSTALL_RECOVERY_REQUIRED"):
        jobs.assert_environment_installation_idle()


def test_a_live_job_still_excludes_any_other(fixture):
    service, _ = fixture
    entered, release = threading.Event(), threading.Event()
    def install(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        return {"state": "succeeded", "error": None}
    jobs.install_system.side_effect = install
    service.submit(PLAN, OP)
    assert entered.wait(3)
    try:
        with pytest.raises(ScreenError, match="INSTALL_BUSY"):
            service.submit(PLAN, "2" * 32)
        with pytest.raises(ScreenError, match="INSTALL_BUSY"):
            jobs.assert_environment_installation_idle()
    finally:
        release.set()
    assert finished(service)["state"] == "succeeded"


def test_an_overdue_job_still_holding_the_environment_excludes_a_repair(fixture, monkeypatch):
    """Past its deadline a job reads as uncertain, but while its worker lives
    it holds the environment lease: a repair cannot run beside it."""
    service, _ = fixture
    entered, release = threading.Event(), threading.Event()
    def install(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        return {"state": "succeeded", "error": None}
    jobs.install_system.side_effect = install
    service.submit(PLAN, OP)
    assert entered.wait(3)
    now = time.time()
    try:
        monkeypatch.setattr(jobs.time, "time", lambda: now + jobs.JOB_SECONDS + 1)
        assert service.status(OP)["state"] == "recovery_required"
        with pytest.raises(ScreenError, match="INSTALL_BUSY"):
            service.submit(PLAN, "2" * 32)
    finally:
        monkeypatch.setattr(jobs.time, "time", lambda: now)
        release.set()
    finished(service)


def test_any_unfinished_installation_is_reported_until_a_later_success(fixture):
    service, _ = fixture
    assert jobs.environment_installation_uncertain() is False  # nothing installed yet
    jobs.install_system.return_value = {"state": "recovery_required", "error": "INSTALL_PACKAGES_FAILED"}
    service.submit(PLAN, OP)
    finished(service)
    assert jobs.environment_installation_uncertain() is True
    jobs.install_system.return_value = {"state": "failed", "error": "INSTALL_INDEX_FAILED"}
    service.submit(PLAN, "2" * 32)
    finished(service, "2" * 32)
    assert jobs.environment_installation_uncertain() is True  # a plain failure repairs nothing
    jobs.install_system.return_value = {"state": "succeeded", "error": None}
    service.submit(PLAN, "3" * 32)
    finished(service, "3" * 32)
    assert jobs.environment_installation_uncertain() is False
