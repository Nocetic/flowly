"""A named bot's screen colour is the avatar colour Desktop draws; the main agent's is black."""

import json

import pytest

import flowly.profile as profiles
from flowly.desktop_runtime.desktop_color import avatar_color, screen_color

# Desktop's profileAvatarColor (ProfileMark.tsx) for the same inputs, rendered
# to hex with its own hslToRgb/Math.round. Any drift here is a visible mismatch.
DESKTOP = [
    (("writer", "", 0), "#40d1e7"),
    (("writer", "", 1), "#e740a0"),
    (("researcher", "", 7), "#cae740"),
    (("default", "", None), "#40d1e7"),
    (("alpha", "", None), "#e79c40"),
    (("şeker", "", None), "#e7c940"),
    (("x", "violet", 3), "#8940e7"),
    (("x", "amber", None), "#e79f40"),
    (("x", "#ff5500", 2), "#ff5500"),
    (("x", "#111111", None), "#575757"),
    (("x", "#f8f8f8", None), "#e6e6e6"),
    (("x", "#3b82f6", None), "#3b82f6"),
    (("x", "#22c55e", None), "#22c55e"),
    (("Z9", "", 123), "#40e3e7"),
]


@pytest.mark.parametrize(("arguments", "expected"), DESKTOP)
def test_matches_the_avatar_colour_desktop_draws(arguments, expected):
    assert avatar_color(*arguments) == expected


@pytest.fixture
def homes(tmp_path, monkeypatch):
    monkeypatch.setattr(profiles, "_DEFAULT_HOME", tmp_path / ".flowly")
    monkeypatch.setattr(profiles, "_PROFILES_ROOT", tmp_path / ".flowly" / "profiles")
    return tmp_path / ".flowly"


def write(home, metadata):
    home.mkdir(parents=True, exist_ok=True)
    (home / "profile.json").write_text(json.dumps(metadata))
    return home / "desktop"


def test_a_named_bot_wears_its_colour_and_the_main_agent_stays_black(homes):
    assert screen_color(write(homes / "profiles" / "writer", {"markTone": "#3B82F6", "markSeed": 4})) == "#3b82f6"
    assert screen_color(write(homes / "profiles" / "scout", {"markSeed": 1})) == "#e740a0"
    assert screen_color(write(homes, {"markTone": "#ff0000"})) is None


def test_unreadable_or_foreign_homes_never_borrow_a_colour(homes, tmp_path):
    assert screen_color(homes / "profiles" / "ghost" / "desktop") == avatar_color("ghost")
    assert screen_color(write(tmp_path / "elsewhere" / "writer", {"markTone": "#ff0000"})) is None
    assert screen_color(write(homes / "profiles" / "writer", {"markTone": 7, "markSeed": "x"})) == avatar_color("writer")
