"""Persisted origin/run resume authority without a model or browser dependency."""

import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from flowly.desktop_runtime.authority import ScreenAuthority, ScreenError
from flowly.desktop_runtime.handoff import RunHandoffs

OWNER = {"pid": 4321, "start": "123", "boot": "boot-one"}
IDENTITY = {"hostId": "host", "botId": "bot", "profileId": "default"}


@pytest.fixture
def handoff(tmp_path, monkeypatch):
    monkeypatch.setattr("flowly.desktop_runtime.handoff.process_identity", lambda pid: dict(OWNER))
    monkeypatch.setattr("flowly.desktop_runtime.handoff.is_alive", lambda identity: identity == OWNER)
    authority = ScreenAuthority(tmp_path)
    authority.start("generation")
    authority.claim_run("original-run", "generation")
    store = RunHandoffs(tmp_path, authority)
    execution = store.start(IDENTITY, "original-session", "original-run", "generation")
    return store, authority, execution


def hand_back(store, authority, execution):
    held = authority.acquire("viewer", "generation", authority.snapshot()["epoch"])
    checkpoint = store.checkpoint(execution)
    authority.release("viewer", "generation", held["epoch"])
    ready = store.checkpoint(execution)
    assert ready["handoffId"] == checkpoint["handoffId"]
    return ready


def test_plain_handback_never_resumes_the_original_task(handoff):
    store, authority, execution = handoff
    waiting = hand_back(*handoff)
    assert waiting["state"] == "waiting_resume"
    assert waiting["originSessionKey"] == "original-session"
    assert waiting["originRunId"] == "original-run"
    assert store.begin_refresh(execution) is None
    assert authority.snapshot()["runId"] == "original-run"
    with pytest.raises(ScreenError, match="SCREEN_BUSY"):
        authority.claim_run("second-run", "generation")


def test_resume_needs_an_explicit_request_and_completed_fresh_observation(handoff):
    store, authority, execution = handoff
    waiting = hand_back(*handoff)
    receipt = store.request_resume(waiting["handoffId"], waiting["controlEpoch"], "resume-operation")
    assert receipt["requested"] is True
    assert store.status()["state"] == "resume_requested"
    epoch = store.begin_refresh(execution)
    assert epoch == waiting["controlEpoch"]
    assert store.status()["state"] == "refreshing"
    store.complete_refresh(execution, epoch)
    assert store.status()["state"] == "ready"
    assert authority.snapshot()["epoch"] == epoch
    assert authority.snapshot()["runId"] == "original-run"


def test_takeover_during_refresh_discards_that_resume_and_requires_another(handoff):
    store, authority, execution = handoff
    waiting = hand_back(*handoff)
    store.request_resume(waiting["handoffId"], waiting["controlEpoch"], "resume-one")
    epoch = store.begin_refresh(execution)
    authority.acquire("other-viewer", "generation", epoch)
    with pytest.raises(ScreenError, match="STALE_OBSERVATION"):
        store.complete_refresh(execution, epoch)
    latest = store.checkpoint(execution)
    assert latest["state"] == "waiting_human"
    assert latest["handoffId"] != waiting["handoffId"]
    store.request_resume(waiting["handoffId"], epoch, "resume-one")  # historical ACK only
    assert store.status()["state"] == "waiting_human"


def test_cancellation_and_a_reused_pid_cannot_resurrect_a_task(handoff, monkeypatch):
    store, authority, execution = handoff
    waiting = hand_back(*handoff)
    monkeypatch.setattr("flowly.desktop_runtime.handoff.is_alive", lambda identity: False)
    assert store.status()["state"] == "recovery_required"
    with pytest.raises(ScreenError, match="HANDOFF_RECOVERY_REQUIRED"):
        store.request_resume(waiting["handoffId"], waiting["controlEpoch"], "resume-one")
    monkeypatch.setattr("flowly.desktop_runtime.handoff.is_alive", lambda identity: True)
    store.finish(execution, cancelled=True)
    with pytest.raises(ScreenError, match="RUN_ENDED"):
        store.request_resume(waiting["handoffId"], waiting["controlEpoch"], "resume-two")
    assert authority.snapshot()["control"] == "agent"


def test_gateway_reconstruction_preserves_checkpoint_and_idempotent_receipt(handoff):
    store, authority, execution = handoff
    waiting = hand_back(*handoff)
    first = store.request_resume(waiting["handoffId"], waiting["controlEpoch"], "resume-one")
    replacement = RunHandoffs(store.path.parent, authority)
    assert replacement.status()["originRunId"] == "original-run"
    assert replacement.request_resume(waiting["handoffId"], waiting["controlEpoch"], "resume-one") == first
    with pytest.raises(ScreenError, match="OPERATION_CONFLICT"):
        replacement.request_resume(waiting["handoffId"], waiting["controlEpoch"] + 1, "resume-one")
    with pytest.raises(ScreenError, match="RUN_CHANGED"):
        replacement.begin_refresh("other-execution")


def test_resume_does_not_bypass_human_control_or_input_drain(handoff):
    store, authority, execution = handoff
    held = authority.acquire("viewer", "generation", 1)
    waiting = store.checkpoint(execution)
    with pytest.raises(ScreenError, match="HUMAN_HAS_CONTROL"):
        store.request_resume(waiting["handoffId"], held["epoch"], "resume")
    assert authority.snapshot()["control"] == "human"


def test_action_admission_fences_a_handback_between_checkpoint_and_action(handoff):
    store, authority, execution = handoff
    checkpoint = store.checkpoint(execution)
    hand_back(*handoff)
    with pytest.raises(ScreenError, match="STALE_EPOCH"):
        authority.begin_action("original-run", "old-click", "generation", expected_epoch=checkpoint["controlEpoch"])
    assert not authority.snapshot()["actionInFlight"]


def test_missing_or_corrupt_initialized_checkpoint_fails_closed(handoff):
    store, _, _ = handoff
    store.path.unlink()
    with pytest.raises(ScreenError, match="HANDOFF_STATE_INVALID"):
        store.status()


def test_an_unobserved_takeover_and_handback_still_requires_explicit_resume(handoff):
    store, authority, execution = handoff
    held = authority.acquire("viewer", "generation", 1)
    authority.release("viewer", "generation", held["epoch"])
    assert store.checkpoint(execution)["state"] == "waiting_resume"


def test_concurrent_resume_retries_share_one_receipt(handoff):
    store, _, _ = handoff
    waiting = hand_back(*handoff)
    def request(_):
        return RunHandoffs(store.path.parent, store.authority).request_resume(
            waiting["handoffId"], waiting["controlEpoch"], "same-operation")
    with ThreadPoolExecutor(max_workers=4) as executor:
        replies = list(executor.map(request, range(8)))
    assert all(reply == replies[0] for reply in replies)
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 1


def test_public_checkpoint_contains_no_execution_or_process_authority(handoff):
    store, _, execution = handoff
    value = store.checkpoint(execution)
    assert "owner" not in value and "executionId" not in value
    assert execution not in str(value)


def test_another_gateway_worker_can_request_but_cannot_execute_the_resume(handoff, monkeypatch):
    store, _, execution = handoff
    waiting = hand_back(*handoff)
    monkeypatch.setattr("flowly.desktop_runtime.handoff.process_identity", lambda pid: {**OWNER, "start": "new-process"})
    store.request_resume(waiting["handoffId"], waiting["controlEpoch"], "resume-other-worker")
    with pytest.raises(ScreenError, match="HANDOFF_RECOVERY_REQUIRED"):
        store.begin_refresh(execution)
    assert store.status()["state"] == "resume_requested"


def test_resume_receipts_are_bounded_and_old_receipts_cannot_resume_a_new_handoff(handoff):
    store, _, execution = handoff
    first = None
    for index in range(132):
        waiting = hand_back(*handoff)
        args = (waiting["handoffId"], waiting["controlEpoch"], f"resume-{index}")
        first = first or args
        store.request_resume(*args)
        epoch = store.begin_refresh(execution)
        store.complete_refresh(execution, epoch)
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 128
    with pytest.raises(ScreenError, match="HANDOFF_CHANGED"):
        store.request_resume(*first)
    assert store.status()["state"] == "ready"


@pytest.mark.parametrize("value", ['{invalid-json', 'null', '{}', '0', '"private"'])
def test_malformed_stored_state_is_not_treated_as_a_new_run(handoff, value):
    store, _, _ = handoff
    with sqlite3.connect(store.path) as db:
        db.execute("UPDATE current_run SET value=?", (value,))
    with pytest.raises(ScreenError, match="HANDOFF_STATE_INVALID"):
        store.status()


def test_unchanged_checkpoint_does_not_rewrite_durable_state(handoff):
    store, _, execution = handoff
    before = store.path.stat().st_mtime_ns
    for _ in range(5):
        store.checkpoint(execution)
    assert store.path.stat().st_mtime_ns == before


def test_container_init_can_own_a_run_without_becoming_a_signal_target(monkeypatch):
    from pathlib import Path

    from flowly.desktop_runtime import handoff, launcher

    def read(path):
        if str(path) == "/proc/1/stat":
            return "1 (agent) " + " ".join(["S"] + ["0"] * 18 + ["123"])
        assert str(path) == "/proc/sys/kernel/random/boot_id"
        return "boot-id"
    monkeypatch.setattr(Path, "read_text", read)
    owner = handoff.process_identity(1)
    assert owner == {"pid": 1, "start": "123", "boot": "boot-id"}
    assert handoff.is_alive(owner)
    assert launcher.process_identity(1) is None
    assert not launcher.is_alive(owner)
