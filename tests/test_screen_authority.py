"""Control handoff invariants shared by tools, viewers and runtime workers."""

import json
from concurrent.futures import ProcessPoolExecutor

import pytest

from flowly.desktop_runtime.authority import ScreenAuthority, ScreenError


def _compete(directory, run):
    try:
        ScreenAuthority(directory).begin_action(run, "action-" + run, "generation-1")
        return "admitted"
    except ScreenError as exc:
        return exc.code


@pytest.fixture
def authority(tmp_path):
    instance = ScreenAuthority(tmp_path / "screen")
    instance.start("generation-1")
    return instance


def test_takeover_waits_for_inflight_action_and_discards_its_result(authority):
    action = authority.begin_action("run-1", "action-1", "generation-1")
    pending = authority.acquire("viewer-1", "generation-1", 1)
    assert pending["control"] == "human-pending"
    with pytest.raises(ScreenError, match="TAKEOVER_PENDING"):
        authority.confirm_acquire("viewer-1", "generation-1", pending["epoch"])
    assert not authority.end_action(action)
    state = authority.confirm_acquire("viewer-1", "generation-1", pending["epoch"])
    assert state["control"] == "human"
    assert authority.may_input("viewer-1", "generation-1", state["epoch"])
    assert not authority.may_input("viewer-2", "generation-1", state["epoch"])
    with pytest.raises(ScreenError, match="HUMAN_HAS_CONTROL"):
        authority.begin_action("run-1", "action-2", "generation-1")


def test_run_ownership_outlives_individual_actions(authority):
    action = authority.begin_action("run-1", "action-1", "generation-1")
    assert authority.end_action(action)
    with pytest.raises(ScreenError, match="SCREEN_BUSY"):
        authority.begin_action("run-2", "action-2", "generation-1")
    authority.finish_run("run-1", "generation-1")
    authority.begin_action("run-2", "action-2", "generation-1")


def test_release_cannot_be_forged_or_replayed_against_new_holder(authority):
    human = authority.acquire("viewer-1", "generation-1", 1)
    with pytest.raises(ScreenError, match="VIEWER_MISMATCH"):
        authority.release("viewer-2", "generation-1", human["epoch"])
    released = authority.release("viewer-1", "generation-1", human["epoch"])
    other = authority.acquire("viewer-2", "generation-1", released["epoch"])
    with pytest.raises(ScreenError, match="STALE_EPOCH"):
        authority.release("viewer-1", "generation-1", human["epoch"])
    assert authority.may_input("viewer-2", "generation-1", other["epoch"])


def test_reconnect_new_instance_preserves_human_control(authority):
    human = authority.acquire("viewer-1", "generation-1", 1)
    restored = ScreenAuthority(authority.directory)
    assert restored.snapshot() == human
    with pytest.raises(ScreenError, match="HUMAN_HAS_CONTROL"):
        restored.start("generation-2")
    assert not restored.may_input("viewer-1", "generation-2", human["epoch"])


def test_explicit_reclaim_revokes_old_viewer(authority):
    human = authority.acquire("viewer-1", "generation-1", 1)
    with pytest.raises(ScreenError, match="SCREEN_BUSY"):
        authority.acquire("viewer-2", "generation-1", human["epoch"])
    replacement = authority.acquire("viewer-2", "generation-1", human["epoch"], reclaim=True)
    assert not authority.may_input("viewer-1", "generation-1", human["epoch"])
    assert authority.may_input("viewer-2", "generation-1", replacement["epoch"])


def test_runtime_generation_invalidates_old_action(authority):
    action = authority.begin_action("run-1", "action-1", "generation-1")
    with pytest.raises(ScreenError, match="SCREEN_BUSY"):
        authority.start("generation-2")
    assert authority.end_action(action)
    authority.finish_run("run-1", "generation-1")
    new = authority.start("generation-2")
    assert new["generation"] == "generation-2"
    assert not authority.end_action(action)
    with pytest.raises(ScreenError, match="STALE_GENERATION"):
        authority.acquire("viewer-1", "generation-1", new["epoch"])


@pytest.mark.parametrize("raw", ["{", "null", "{}", '{"schema": 99}'])
def test_corrupt_state_fails_closed_without_reinitializing(authority, raw):
    path = authority.directory / "control.json"
    path.write_text(raw)
    with pytest.raises(ScreenError, match="CONTROL_STATE_INVALID"):
        authority.start("generation-2")
    assert not authority.may_input("viewer-1", "generation-1", 1)
    assert path.read_text() == raw


def test_semantically_invalid_state_fails_closed(authority):
    path = authority.directory / "control.json"
    raw = json.loads(path.read_text())
    raw["epoch"] = True
    path.write_text(json.dumps(raw))
    with pytest.raises(ScreenError, match="CONTROL_STATE_INVALID"):
        authority.snapshot()


def test_profiles_are_independent(tmp_path):
    left, right = [ScreenAuthority(tmp_path / name) for name in ("left", "right")]
    left.start("generation-1")
    right.start("generation-1")
    left.acquire("viewer-1", "generation-1", 1)
    action = right.begin_action("run-1", "action-1", "generation-1")
    assert right.end_action(action)


def test_only_one_process_can_claim_the_profile(authority):
    with ProcessPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(_compete, [authority.directory] * 2, ["run-a", "run-b"]))
    assert sorted(results) == ["SCREEN_BUSY", "admitted"]


def test_public_state_contains_no_viewer_capability(authority):
    human = authority.acquire("secret-viewer-capability", "generation-1", 1)
    assert "secret-viewer-capability" not in json.dumps(human)
    assert human["holderHash"]


def test_stop_rejects_human_and_work(authority):
    action = authority.begin_action("run-1", "action-1", "generation-1")
    with pytest.raises(ScreenError, match="SCREEN_BUSY"):
        authority.stop("generation-1")
    authority.end_action(action)
    authority.finish_run("run-1", "generation-1")
    state = authority.acquire("viewer-1", "generation-1", 1)
    with pytest.raises(ScreenError, match="HUMAN_HAS_CONTROL"):
        authority.stop("generation-1")
    authority.release("viewer-1", "generation-1", state["epoch"])
    assert not authority.stop("generation-1")["running"]
    with pytest.raises(ScreenError, match="SCREEN_NOT_RUNNING"):
        authority.begin_action("run-2", "action-2", "generation-1")


def test_dead_generation_stop_ends_human_input_and_run_claims(authority):
    action = authority.begin_action("run-1", "action-1", "generation-1")
    human = authority.acquire("viewer-1", "generation-1", authority.snapshot()["epoch"])
    assert human["control"] == "human-pending"
    # Live screen: an ordinary stop still refuses to override the human.
    with pytest.raises(ScreenError, match="HUMAN_HAS_CONTROL"):
        authority.stop("generation-1")
    with pytest.raises(ScreenError, match="STALE_GENERATION"):
        authority.stop_dead("generation-0")
    finished = authority.stop_dead("generation-1")
    assert finished == {**finished, "running": False, "control": "agent", "runId": None,
                        "actionInFlight": False, "inputDrainRequired": False, "holderHash": None}
    assert finished["epoch"] > human["epoch"]
    assert not authority.end_action(action), "the dead generation's action is fenced"
    assert not authority.may_input("viewer-1", "generation-1", human["epoch"])
    assert authority.start("generation-2")["running"]
    assert ScreenAuthority(authority.directory).snapshot()["generation"] == "generation-2"


def test_dead_generation_stop_needs_the_running_generation(authority):
    authority.stop("generation-1")
    with pytest.raises(ScreenError, match="SCREEN_NOT_RUNNING"):
        authority.stop_dead("generation-1")
