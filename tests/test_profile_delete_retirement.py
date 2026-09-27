"""Deleting a bot retires its name atomically and it never comes back.

Two failures motivated this. A delete raced a runtime that was starting in
the same profile: ``shutil.rmtree`` met files written mid-removal and failed
with ``[Errno 66] Directory not empty``, leaving a half-deleted bot. And a
runtime started for a deleted profile recreated its directory (logs,
databases), so the bot reappeared with no identity and a runtime that could
not start: another avatar, "Needs attention".
"""

from __future__ import annotations

import errno
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import flowly.profile as profiles


@pytest.fixture()
def roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    default = tmp_path / "flowly"
    monkeypatch.setattr(profiles, "_DEFAULT_HOME", default)
    monkeypatch.setattr(profiles, "_PROFILES_ROOT", default / "profiles")
    monkeypatch.setenv("FLOWLY_HOME", str(default))
    monkeypatch.delenv(profiles.PROFILE_DIR_IDENTITY_ENV, raising=False)
    monkeypatch.setattr(profiles, "refresh_roster_index", lambda: None)
    monkeypatch.setattr(profiles, "remove_wrapper_script", lambda name: None)
    return default


def _names() -> list[str]:
    return [profile.name for profile in profiles.list_profiles()]


def test_a_delete_survives_files_written_while_it_removes(roots, monkeypatch) -> None:
    profiles.create_profile("lovelace", local_runtime=True)
    real_rmtree = shutil.rmtree

    def busy_rmtree(path, *args, **kwargs):
        raise OSError(errno.ENOTEMPTY, "Directory not empty", str(path))

    monkeypatch.setattr(profiles.shutil, "rmtree", busy_rmtree)
    monkeypatch.setattr(profiles.time, "sleep", lambda _: None)

    profiles.delete_profile("lovelace")

    # The bot is gone and its name is free, even though removal kept failing.
    assert "lovelace" not in _names()
    assert not profiles.profile_exists("lovelace")
    assert not (roots / "profiles" / "lovelace").exists()
    profiles.create_profile("lovelace", local_runtime=True)
    assert "lovelace" in _names()

    # The next delete sweeps what the first one could not remove.
    monkeypatch.setattr(profiles.shutil, "rmtree", real_rmtree)
    profiles.delete_profile("lovelace")
    assert list((roots / "profiles" / profiles._PROFILE_TRASH_DIR).iterdir()) == []


def test_retired_trees_never_count_as_bots(roots, monkeypatch) -> None:
    profiles.create_profile("ada", local_runtime=True)
    monkeypatch.setattr(profiles, "_remove_retired_profile", lambda path: False)
    profiles.delete_profile("ada")
    assert (roots / "profiles" / profiles._PROFILE_TRASH_DIR).is_dir()
    assert _names() == ["default"]


def test_a_runtime_cannot_claim_a_deleted_profile(roots, monkeypatch) -> None:
    profiles.create_profile("grace", local_runtime=True)
    home = roots / "profiles" / "grace"
    monkeypatch.setenv("FLOWLY_HOME", str(home))
    monkeypatch.setenv(profiles.PROFILE_DIR_IDENTITY_ENV, profiles.named_profile_identity("grace"))
    profiles.delete_profile("grace")

    with pytest.raises(FileNotFoundError, match="does not exist"):
        profiles.claim_runtime_lease("instance-1")
    assert not home.exists()


def test_a_runtime_removes_what_it_recreated_after_a_delete(roots, monkeypatch) -> None:
    profiles.create_profile("hopper", local_runtime=True)
    home = roots / "profiles" / "hopper"
    monkeypatch.setenv("FLOWLY_HOME", str(home))
    monkeypatch.setenv(profiles.PROFILE_DIR_IDENTITY_ENV, profiles.named_profile_identity("hopper"))
    profiles.delete_profile("hopper")
    # The runtime's own start-up writes land before it claims the lease.
    (home / "logs").mkdir(parents=True)
    (home / "logs" / "gateway.log").write_text("starting\n")

    with pytest.raises(FileNotFoundError, match="was deleted"):
        profiles.claim_runtime_lease("instance-1")
    assert not home.exists()
    assert "hopper" not in _names()


def test_a_live_profile_is_still_claimed(roots, monkeypatch) -> None:
    profiles.create_profile("turing", local_runtime=True)
    home = roots / "profiles" / "turing"
    monkeypatch.setenv("FLOWLY_HOME", str(home))
    monkeypatch.setenv(profiles.PROFILE_DIR_IDENTITY_ENV, profiles.named_profile_identity("turing"))
    lease = profiles.claim_runtime_lease("instance-1")
    assert lease.exists()
    profiles.release_runtime_lease("instance-1")


def test_the_cli_never_creates_a_missing_profile(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / ".flowly" / "profiles").mkdir(parents=True)
    env = {**os.environ, "HOME": str(home)}
    env.pop("FLOWLY_HOME", None)
    env.pop("FLOWLY_PROFILE", None)
    result = subprocess.run(
        [sys.executable, "-c", "import sys; sys.argv = ['flowly', '--profile', 'ghost', 'serve', '--port', '0']; "
         "from flowly.cli.entry import main; main()"],
        env=env, capture_output=True, text=True, timeout=60,
        # Import this checkout, whichever interpreter runs the suite.
        cwd=Path(__file__).resolve().parents[1],
    )
    assert result.returncode == 2
    assert "Profile 'ghost' does not exist" in result.stderr
    assert list((home / ".flowly" / "profiles").iterdir()) == []
