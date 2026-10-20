"""A custom FLOWLY_HOME is its own installation for the screen, never the default one."""

import json

import pytest

import flowly.profile as profiles
from flowly.desktop_runtime.authority import ScreenError
from flowly.gateway import display
from tests.test_screen_display_gateway import display_gateway as gateway_fixture

display_gateway = gateway_fixture


@pytest.fixture
def custom_home(display_gateway, tmp_path, monkeypatch):
    default = display._resolve(display_gateway.gateway, {"profileId": "default"}).identity
    home = tmp_path / "srv" / "flowly-data"
    monkeypatch.setenv("FLOWLY_HOME", str(home))
    assert profiles.current_profile_name() == "default" and not profiles.is_default_home()
    return display_gateway, home, default


def test_custom_home_screen_uses_its_own_identity_and_directory(custom_home):
    f, home, default = custom_home
    session = display._resolve(f.gateway, {"profileId": "default"})
    assert session.directory == home / "desktop"
    assert session.identity["botId"] != default["botId"], "never the default install's identity"
    assert json.loads((home / "profile.json").read_text())["botId"] == session.identity["botId"]
    again = display._resolve(f.gateway, {"profileId": "default", "expectedHostId": session.identity["hostId"],
                                         "expectedBotId": session.identity["botId"]}, pinned=True)
    assert again is session
    with pytest.raises(ScreenError, match="PROFILE_IDENTITY_CHANGED"):
        display._resolve(f.gateway, {"profileId": "default", "expectedHostId": default["hostId"],
                                     "expectedBotId": default["botId"]}, pinned=True)


def test_custom_home_never_exposes_the_default_installs_named_profiles(custom_home):
    f, _, _ = custom_home
    assert f.gateway.profile_host is not None
    with pytest.raises(ScreenError, match="PROFILE_NOT_AVAILABLE"):
        display._resolve(f.gateway, {"profileId": "writer"})


async def test_custom_home_status_rpc_reports_the_custom_identity(custom_home):
    f, home, default = custom_home
    status = (await f.rpc("display.status", {"profileId": "default"}))["result"]
    assert status["botId"] != default["botId"]
    assert status["botId"] == json.loads((home / "profile.json").read_text())["botId"]


def test_default_home_identity_is_unchanged(display_gateway):
    _, path, bot_id = profiles.active_profile_identity()
    info = profiles.ensure_profile_bot_id("default")
    assert (path, bot_id) == (info.path, info.bot_id)
