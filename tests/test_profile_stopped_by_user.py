"""An owner's stop is remembered on the agent, and only there."""

from __future__ import annotations

import json
import tarfile
from pathlib import Path

import pytest

import flowly.profile as profiles


@pytest.fixture()
def roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    default = tmp_path / "flowly"
    monkeypatch.setattr(profiles, "_DEFAULT_HOME", default)
    monkeypatch.setattr(profiles, "_PROFILES_ROOT", default / "profiles")
    monkeypatch.setenv("FLOWLY_HOME", str(default))
    return default


def _metadata(name: str) -> dict:
    path = profiles.describe_profile(name).path / profiles._PROFILE_METADATA_FILE
    return json.loads(path.read_text(encoding="utf-8"))


def test_a_new_agent_is_not_stopped(roots) -> None:
    profiles.create_profile("writer", local_runtime=True)

    assert profiles.describe_profile("writer").stopped_by_user is False
    assert profiles.describe_profile("writer").to_public_dict()["stoppedByUser"] is False
    assert "stoppedByUser" not in _metadata("writer")


def test_the_mark_is_set_and_cleared_without_touching_the_edit_time(roots) -> None:
    profiles.create_profile("writer", local_runtime=True)
    updated_at = _metadata("writer")["updatedAt"]

    assert profiles.set_profile_stopped_by_user("writer", True) is True
    assert profiles.describe_profile("writer").stopped_by_user is True
    assert _metadata("writer")["updatedAt"] == updated_at

    assert profiles.set_profile_stopped_by_user("writer", False) is True
    assert profiles.describe_profile("writer").stopped_by_user is False
    assert "stoppedByUser" not in _metadata("writer")


def test_writing_what_is_already_stored_changes_nothing(roots) -> None:
    profiles.create_profile("writer", local_runtime=True)
    path = profiles.describe_profile("writer").path / profiles._PROFILE_METADATA_FILE
    before = path.stat().st_mtime_ns

    assert profiles.set_profile_stopped_by_user("writer", False) is False
    assert path.stat().st_mtime_ns == before


def test_an_edit_keeps_the_mark(roots) -> None:
    profiles.create_profile("writer", local_runtime=True)
    profiles.set_profile_stopped_by_user("writer", True)

    profiles.update_profile_metadata("writer", display_name="Writer")

    assert profiles.describe_profile("writer").stopped_by_user is True


def test_only_a_real_true_counts_as_stopped(roots) -> None:
    profiles.create_profile("writer", local_runtime=True)
    path = profiles.describe_profile("writer").path / profiles._PROFILE_METADATA_FILE
    for value in ("true", 1, None, ["yes"]):
        data = _metadata("writer")
        data["stoppedByUser"] = value
        path.write_text(json.dumps(data), encoding="utf-8")
        assert profiles.describe_profile("writer").stopped_by_user is False


def test_the_main_agent_cannot_be_stopped(roots) -> None:
    roots.mkdir(parents=True)
    with pytest.raises(ValueError):
        profiles.set_profile_stopped_by_user("default", True)
    assert profiles.describe_profile("default").stopped_by_user is False


def test_a_missing_agent_is_an_error(roots) -> None:
    roots.mkdir(parents=True)
    with pytest.raises(FileNotFoundError):
        profiles.set_profile_stopped_by_user("ghost", True)


def test_a_shared_template_does_not_carry_the_mark(roots, tmp_path: Path) -> None:
    profiles.create_profile("writer", local_runtime=True)
    profiles.set_profile_stopped_by_user("writer", True)

    archive = profiles.export_profile_template("writer", str(tmp_path / "writer"))

    with tarfile.open(archive, "r:gz") as bundle:
        metadata = json.load(bundle.extractfile("writer/profile.json"))
    assert "stoppedByUser" not in metadata


def test_an_imported_agent_arrives_ready_to_run(roots, tmp_path: Path) -> None:
    profiles.create_profile("writer", local_runtime=True)
    profiles.set_profile_stopped_by_user("writer", True)
    archive = profiles.export_profile("writer", str(tmp_path / "backup"))

    imported = profiles.import_profile(str(archive), name="copy")

    assert profiles.describe_profile(imported.name).stopped_by_user is False
    assert profiles.describe_profile("writer").stopped_by_user is True


def test_the_cli_records_the_owners_choice(roots) -> None:
    from typer.testing import CliRunner

    from flowly.cli.profile_cmd import profile_app

    profiles.create_profile("writer", local_runtime=True)
    runner = CliRunner()

    stopped = runner.invoke(profile_app, ["run-state", "writer", "stopped", "--json"])
    assert stopped.exit_code == 0, stopped.output
    payload = json.loads(stopped.output)
    assert payload["changed"] is True
    assert payload["profile"]["stoppedByUser"] is True

    again = runner.invoke(profile_app, ["run-state", "writer", "stopped", "--json"])
    assert json.loads(again.output)["changed"] is False

    running = runner.invoke(profile_app, ["run-state", "writer", "running", "--json"])
    assert json.loads(running.output)["profile"]["stoppedByUser"] is False

    wrong = runner.invoke(profile_app, ["run-state", "writer", "paused", "--json"])
    assert wrong.exit_code != 0
    main = runner.invoke(profile_app, ["run-state", "default", "stopped", "--json"])
    assert main.exit_code != 0
