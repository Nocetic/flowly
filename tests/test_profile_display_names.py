"""Addressing a bot by the only name its owner has ever seen.

A bot keeps the id it was created with and shows a display name its owner can
change at any time. Every surface renders the display name, so the id is the
one label nobody recognises — which stayed harmless until something had to be
told which bot to talk to and was handed a list of ids alone. Asked to message
Friday, the model answered that no such bot existed, and from what it had been
given that was true: the list said `dqwdqwd`.
"""
from __future__ import annotations

import pytest

from flowly import profile as profile_module
from flowly.profile import ProfileInfo, profile_display_names, resolve_profile_reference


def _profile(name: str, display: str = "") -> ProfileInfo:
    return ProfileInfo(
        name=name,
        path=__import__("pathlib").Path("/tmp") / name,
        is_default=False,
        display_name=display,
    )


@pytest.fixture
def roster(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """Install a roster, with the real filesystem out of the way.

    A label now has two sources: the profiles this process can read, and the
    index the primary publishes for the siblings it may not. Faking only the
    first would leave the second reading whoever's bots happen to live in
    ``~/.flowly`` on the machine running the test.
    """
    home = tmp_path / ".flowly"
    (home / "profiles").mkdir(parents=True)
    monkeypatch.setattr(profile_module, "_DEFAULT_HOME", home)
    monkeypatch.setattr(profile_module, "_PROFILES_ROOT", home / "profiles")

    def install(*profiles: ProfileInfo) -> None:
        monkeypatch.setattr(profile_module, "list_profiles", lambda: list(profiles))
    return install


def test_the_label_travels_with_the_id(roster):
    roster(_profile("dqwdqwd", "Friday"), _profile("jarvis", "Jarvis"))
    assert profile_display_names() == {"dqwdqwd": "Friday", "jarvis": "Jarvis"}


def test_a_label_that_only_repeats_the_id_is_left_out(roster):
    # Nothing to add for these, and repeating them would pad every prompt with
    # `jarvis (jarvis)`.
    roster(_profile("jarvis", "jarvis"), _profile("scout", ""))
    assert profile_display_names() == {}


def test_only_the_ids_asked_for_come_back(roster):
    roster(_profile("dqwdqwd", "Friday"), _profile("jarvis", "Jarvis"))
    assert profile_display_names(["jarvis"]) == {"jarvis": "Jarvis"}


def test_an_id_resolves_to_itself(roster):
    roster(_profile("dqwdqwd", "Friday"))
    assert resolve_profile_reference("dqwdqwd") == "dqwdqwd"


def test_the_name_the_user_says_resolves_to_the_id(roster):
    roster(_profile("dqwdqwd", "Friday"), _profile("jarvis", "Jarvis"))
    assert resolve_profile_reference("Friday") == "dqwdqwd"
    assert resolve_profile_reference("friday") == "dqwdqwd"
    assert resolve_profile_reference("  FRIDAY  ") == "dqwdqwd"


def test_an_id_wins_over_a_display_name_that_collides_with_it(roster):
    # One bot is called `friday`; another is displayed as Friday. The typed id
    # is exact and must not be redirected by somebody else's label.
    roster(_profile("friday", "Scout"), _profile("dqwdqwd", "Friday"))
    assert resolve_profile_reference("friday") == "friday"


def test_a_name_two_bots_answer_to_resolves_to_neither(roster):
    # Display names are not unique. Picking one would message the wrong bot,
    # which is worse than reporting that the name did not identify anybody.
    roster(_profile("a1", "Friday"), _profile("b2", "Friday"))
    assert resolve_profile_reference("Friday") is None


def test_a_name_nobody_answers_to_resolves_to_nothing(roster):
    roster(_profile("dqwdqwd", "Friday"))
    assert resolve_profile_reference("Gandalf") is None
    assert resolve_profile_reference("") is None
    assert resolve_profile_reference("   ") is None
