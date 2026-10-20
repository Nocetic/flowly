"""Destructive profile operations must not orphan or resurrect a screen."""

import shutil
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

import flowly.profile as profiles
from flowly.desktop_runtime import launcher, profile_lifecycle
from flowly.desktop_runtime.authority import ScreenError
from flowly.desktop_runtime.launcher import ScreenRuntime, write_state


@pytest.fixture
def profile(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(profiles, "_DEFAULT_HOME", home)
    monkeypatch.setattr(profiles, "_PROFILES_ROOT", home / "profiles")
    monkeypatch.delenv("FLOWLY_HOME", raising=False)
    monkeypatch.setattr(ScreenRuntime, "capabilities", staticmethod(
        lambda: {"supported": True, "reason": None, "missing": []}))
    return profiles.create_profile("writer", local_runtime=True)


def manifest(runtime, phase, **changes):
    state = dict(schema=1, generation="generation-1", phase=phase,
                 supervisor=None, children=[], error=None)
    state.update(changes)
    write_state(runtime.manifest, state)


@pytest.mark.parametrize("phase,code", [
    ("starting", "SCREEN_BUSY"), ("running", "SCREEN_BUSY"),
    ("failed", "RUNTIME_RECOVERY_REQUIRED"),
])
def test_active_or_uncertain_screen_blocks_profile_deletion(profile, phase, code):
    runtime = ScreenRuntime(profile / "desktop")
    manifest(runtime, phase)
    with pytest.raises(ScreenError, match=code):
        profiles.delete_profile("writer")
    assert profile.is_dir()
    assert profile_lifecycle.retirement(runtime.directory) is None


def test_stopped_manifest_with_live_child_cannot_be_deleted(profile, monkeypatch):
    runtime = ScreenRuntime(profile / "desktop")
    manifest(runtime, "stopped", children=[{"pid": 123}])
    monkeypatch.setattr(launcher, "is_alive", lambda value: value == {"pid": 123})
    with pytest.raises(ScreenError, match="SCREEN_BUSY"):
        profiles.delete_profile("writer")
    assert profile.is_dir()


def test_human_ownership_blocks_deletion_even_without_manifest(profile):
    runtime = ScreenRuntime(profile / "desktop")
    state = runtime.authority.start("generation-1")
    runtime.authority.acquire("viewer", "generation-1", state["epoch"])
    with pytest.raises(ScreenError, match="HUMAN_HAS_CONTROL"):
        profiles.delete_profile("writer")
    assert runtime.authority.snapshot()["control"] == "human"


def test_stopped_deletion_removes_sockets_and_stale_object_cannot_recreate(profile):
    runtime = ScreenRuntime(profile / "desktop")
    sockets = runtime.socket_directory
    applications = runtime.application_socket_directory
    (applications / "cookie").write_text("private")
    manifest(runtime, "stopped")
    profiles.delete_profile("writer")
    assert not profile.exists() and not sockets.exists() and not applications.exists()
    for operation in (runtime.start, runtime.read_manifest, lambda: runtime.stop("generation-1")):
        with pytest.raises(ScreenError, match="PROFILE_IDENTITY_CHANGED"):
            operation()
    assert not profile.exists()
    replacement = profiles.create_profile("writer", local_runtime=True)
    fresh = ScreenRuntime(replacement / "desktop")
    assert fresh.read_manifest() is None
    with pytest.raises(ScreenError, match="PROFILE_IDENTITY_CHANGED"):
        runtime.read_manifest()


def test_cleanup_failure_fences_runtime_until_delete_retry(profile, monkeypatch):
    runtime = ScreenRuntime(profile / "desktop")
    manifest(runtime, "stopped")
    original = profile_lifecycle.cleanup_screen_storage
    monkeypatch.setattr(profile_lifecycle, "cleanup_screen_storage",
                        lambda _: (_ for _ in ()).throw(OSError("disk unavailable")))
    with pytest.raises(OSError, match="disk unavailable"):
        profiles.delete_profile("writer")
    assert profile_lifecycle.retirement(runtime.directory) is not None
    for operation in (runtime.start, lambda: ScreenRuntime(runtime.directory)):
        with pytest.raises(ScreenError, match="PROFILE_IDENTITY_CHANGED"):
            operation()
    monkeypatch.setattr(profile_lifecycle, "cleanup_screen_storage", original)
    profiles.delete_profile("writer")
    assert not profile.exists()
    assert profile_lifecycle.retirement(runtime.directory) is None


def test_lost_final_ack_blocks_recreation_until_idempotent_delete(profile, monkeypatch):
    original = profile_lifecycle.finish_retirement
    monkeypatch.setattr(profile_lifecycle, "finish_retirement",
                        lambda _: (_ for _ in ()).throw(OSError("lost acknowledgement")))
    with pytest.raises(OSError, match="lost acknowledgement"):
        profiles.delete_profile("writer")
    assert not profile.exists()
    with pytest.raises(ScreenError, match="PROFILE_IDENTITY_CHANGED"):
        profiles.create_profile("writer", local_runtime=True)
    monkeypatch.setattr(profile_lifecycle, "finish_retirement", original)
    profiles.delete_profile("writer")
    assert profiles.create_profile("writer", local_runtime=True).is_dir()


def test_retirement_survives_loss_of_temporary_locks(profile, tmp_path, monkeypatch):
    profile_lifecycle.begin_retirement(profile)
    journal = profile_lifecycle._retirement_path(profile / "desktop")
    assert journal.parent.parent == profile.parent
    assert not journal.is_relative_to(profile)
    shutil.rmtree(profile)  # Crash after removing data but before acknowledging.
    new_boot_locks = tmp_path / "fresh-boot"
    new_boot_locks.mkdir(mode=0o700)
    monkeypatch.setattr(profile_lifecycle, "_guard_path", lambda _: new_boot_locks / "profile.lock")
    assert profile_lifecycle.retirement(profile / "desktop") is not None
    with pytest.raises(ScreenError, match="PROFILE_IDENTITY_CHANGED"):
        profiles.create_profile("writer", local_runtime=True)
    profiles.delete_profile("writer")
    assert not journal.exists()
    assert profiles.create_profile("writer", local_runtime=True).is_dir()


@pytest.mark.parametrize("unsafe", ["symlink", "shared"])
def test_untrusted_retirement_directory_is_rejected_without_writing(profile, tmp_path, unsafe):
    parent = profile_lifecycle._retirement_path(profile / "desktop").parent
    outside = tmp_path / "outside-journal"
    outside.mkdir()
    if unsafe == "symlink":
        parent.symlink_to(outside, target_is_directory=True)
    else:
        parent.mkdir(mode=0o755)
        parent.chmod(0o755)
    with pytest.raises(ScreenError, match="RUNTIME_STATE_INVALID"):
        profiles.delete_profile("writer")
    assert profile.is_dir() and list(outside.iterdir()) == []


def test_retirement_read_does_not_create_journal_directory(profile):
    path = profile_lifecycle._retirement_path(profile / "desktop")
    assert profile_lifecycle.retirement(profile / "desktop") is None
    assert not path.parent.exists()


def test_pending_retirement_cannot_delete_a_replaced_profile(profile):
    profile_lifecycle.begin_retirement(profile)
    saved = profile.with_name("saved")
    profile.rename(saved)
    profile.mkdir()
    marker = profile / "replacement"
    marker.write_text("keep")
    try:
        with pytest.raises(ScreenError, match="PROFILE_IDENTITY_CHANGED"):
            profiles.delete_profile("writer")
        assert marker.read_text() == "keep"
    finally:
        shutil.rmtree(profile)
        saved.rename(profile)
        profiles.delete_profile("writer")


def test_confirmation_identity_is_checked_before_removal(profile):
    with pytest.raises(ScreenError, match="PROFILE_IDENTITY_CHANGED"):
        profiles.delete_profile("writer", expected_bot_id="different-bot")
    assert profile.is_dir()
    assert profile_lifecycle.retirement(profile / "desktop") is None


def test_profile_symlink_is_rejected_before_constructing_authority(profile, tmp_path):
    outside = tmp_path / "outside"
    desktop = outside / "desktop"
    desktop.mkdir(parents=True)
    shutil.rmtree(profile)
    profile.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ScreenError, match="RUNTIME_STATE_INVALID"):
        profiles.delete_profile("writer")
    assert list(desktop.iterdir()) == []
    assert profile.is_symlink()


def test_constructor_waits_for_delete_and_does_not_resurrect_profile(profile, monkeypatch):
    ScreenRuntime(profile / "desktop")
    entered = threading.Event()
    release = threading.Event()
    original = profile_lifecycle.cleanup_screen_storage

    def cleanup(runtime):
        entered.set()
        assert release.wait(5)
        original(runtime)

    monkeypatch.setattr(profile_lifecycle, "cleanup_screen_storage", cleanup)
    with ThreadPoolExecutor(max_workers=2) as pool:
        deleting = pool.submit(profiles.delete_profile, "writer")
        assert entered.wait(5)
        creating = pool.submit(ScreenRuntime, profile / "desktop", require_parent=True)
        release.set()
        deleting.result(timeout=5)
        with pytest.raises(ScreenError, match="PROFILE_IDENTITY_CHANGED"):
            creating.result(timeout=5)
    assert not profile.exists()
