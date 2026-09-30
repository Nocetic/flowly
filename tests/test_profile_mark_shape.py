"""A bot's chosen avatar shape travels with its profile like its colour.

Empty means "not chosen": clients derive a shape from the mark seed, so older
bots get one without a migration and every device shows the same one.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

import flowly.profile as profiles
from flowly.cli.profile_cmd import profile_app


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


def test_shape_is_optional_and_stored_when_chosen(roots) -> None:
    profiles.create_profile("unset", local_runtime=True)
    profiles.create_profile("ghosty", local_runtime=True, mark_shape="  Ghost ")
    assert profiles.describe_profile("unset").to_dict()["markShape"] == ""
    assert profiles.describe_profile("ghosty").to_dict()["markShape"] == "ghost"
    assert _metadata("ghosty")["markShape"] == "ghost"


def test_unknown_shape_is_rejected_before_anything_is_created(roots) -> None:
    with pytest.raises(ValueError, match="Unknown profile avatar shape"):
        profiles.create_profile("writer", mark_shape="dragon")
    assert not (roots / "profiles" / "writer").exists()


def test_update_keeps_sets_and_clears_the_shape(roots) -> None:
    profiles.create_profile("writer", local_runtime=True, mark_shape="robot")
    assert profiles.update_profile_metadata("writer", display_name="Writer").mark_shape == "robot"
    assert profiles.update_profile_metadata("writer", mark_shape="bunny").mark_shape == "bunny"
    assert profiles.update_profile_metadata("writer", mark_shape="").mark_shape == ""
    with pytest.raises(ValueError):
        profiles.update_profile_metadata("writer", mark_shape="dragon")
    assert profiles.describe_profile("writer").mark_shape == ""


def test_hand_edited_or_newer_shape_reads_as_not_chosen(roots) -> None:
    profiles.create_profile("writer", local_runtime=True)
    path = profiles.describe_profile("writer").path / profiles._PROFILE_METADATA_FILE
    for value in ("hologram", 7, None, ["ghost"]):
        data = _metadata("writer")
        data["markShape"] = value
        path.write_text(json.dumps(data), encoding="utf-8")
        assert profiles.describe_profile("writer").mark_shape == ""


def test_export_import_keeps_the_shape(roots, tmp_path: Path) -> None:
    profiles.create_profile("writer", local_runtime=True, mark_shape="cloud")
    archive = profiles.export_profile("writer", str(tmp_path / "backup"))
    imported = profiles.import_profile(str(archive), name="copy")
    assert profiles.describe_profile(imported.name).mark_shape == "cloud"


def test_cli_create_and_configure_accept_the_shape(roots) -> None:
    created = CliRunner().invoke(
        profile_app, ["create", "writer", "--local-only", "--mark-shape", "star", "--json"]
    )
    assert created.exit_code == 0, created.output
    assert json.loads(created.output)["profile"]["markShape"] == "star"
    configured = CliRunner().invoke(profile_app, ["configure", "writer", "--mark-shape", "", "--json"])
    assert configured.exit_code == 0, configured.output
    assert json.loads(configured.output)["profile"]["markShape"] == ""
    rejected = CliRunner().invoke(profile_app, ["configure", "writer", "--mark-shape", "dragon", "--json"])
    assert rejected.exit_code != 0


@pytest.mark.asyncio
async def test_remote_host_creates_and_configures_the_shape(roots) -> None:
    from flowly.profile_host import ProfileHost

    (roots / "workspace").mkdir(parents=True, exist_ok=True)
    host = ProfileHost()
    created = await host.create({"name": "writer", "markShape": "bunny", "credentialPolicy": "isolated"})
    assert created["profile"]["markShape"] == "bunny"
    configured = await host.configure({"name": "writer", "markShape": "square"})
    assert configured["profile"]["markShape"] == "square"
    cleared = await host.configure({"name": "writer", "markShape": ""})
    assert cleared["profile"]["markShape"] == ""
    listed = {item["name"]: item for item in (await host.list())["profiles"]}
    assert listed["writer"]["markShape"] == ""
