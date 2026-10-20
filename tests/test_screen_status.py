"""Public status ordering is independent of control ownership and gateway lifetime."""

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from flowly.desktop_runtime.authority import ScreenError
from flowly.desktop_runtime.status import StatusSequence


def test_phase_changes_advance_revision_without_changing_control(tmp_path):
    sequence = StatusSequence(tmp_path)
    value = {"phase": "running", "generation": "one", "control": {"revision": 3}}
    initial = sequence.snapshot(lambda: value)
    assert initial["statusRevision"] == 1
    assert "statusRevision" not in value
    assert sequence.snapshot(lambda: dict(value)) == initial
    failed = {**value, "phase": "failed"}
    assert sequence.snapshot(lambda: failed)["statusRevision"] == 2
    reconstructed = StatusSequence(tmp_path)
    assert reconstructed.snapshot(lambda: failed)["statusRevision"] == 2
    assert reconstructed.snapshot(lambda: {**value, "generation": "two"})["statusRevision"] == 3


def test_sampling_happens_inside_cross_instance_serialization(tmp_path):
    first, second = StatusSequence(tmp_path), StatusSequence(tmp_path)
    started, release = Event(), Event()

    def slow_sample():
        started.set()
        assert release.wait(3)
        return {"phase": "starting"}

    with ThreadPoolExecutor(max_workers=2) as executor:
        older = executor.submit(first.snapshot, slow_sample)
        assert started.wait(3)
        newer = executor.submit(second.snapshot, lambda: {"phase": "running"})
        release.set()
        assert older.result()["statusRevision"] == 1
        assert newer.result()["statusRevision"] == 2
    assert first.snapshot(lambda: {"phase": "running"})["statusRevision"] == 2


def test_failed_sample_does_not_allocate_a_revision(tmp_path):
    sequence = StatusSequence(tmp_path)
    def failed():
        raise ScreenError("CONTROL_UNAVAILABLE")
    with pytest.raises(ScreenError, match="CONTROL_UNAVAILABLE"):
        sequence.snapshot(failed)
    assert sequence.snapshot(lambda: {"phase": "stopped"})["statusRevision"] == 1


def test_lost_state_cannot_restart_sequence_at_one(tmp_path):
    sequence = StatusSequence(tmp_path)
    sequence.snapshot(lambda: {"phase": "running"})
    sequence.path.unlink()
    with pytest.raises(ScreenError, match="STATUS_STATE_INVALID"):
        StatusSequence(tmp_path).snapshot(lambda: {"phase": "failed"})


@pytest.mark.parametrize("revision", [-1, 2**53 - 1])
def test_invalid_or_exhausted_sequence_fails_closed(tmp_path, revision):
    sequence = StatusSequence(tmp_path)
    sequence.snapshot(lambda: {"phase": "stopped"})
    with sqlite3.connect(tmp_path / "status.sqlite3") as db:
        db.execute("UPDATE status SET revision = ?", (revision,))
    with pytest.raises(ScreenError, match="STATUS_STATE_INVALID"):
        sequence.snapshot(lambda: {"phase": "starting"})
