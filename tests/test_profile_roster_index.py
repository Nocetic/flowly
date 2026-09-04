"""The published roster index: how a bot learns its siblings' names.

A named bot may not read a sibling's profile.json — the isolation policy
withholds it — so the name its owner gave the bot next door is the one
thing it cannot discover for itself. The primary publishes that mapping
where a bot is allowed to look, and these tests pin what a bot may
conclude from it.
"""

from __future__ import annotations

import json
import pathlib
from pathlib import Path

import pytest

import flowly.profile as profiles


@pytest.fixture
def profile_roots(tmp_path, monkeypatch: pytest.MonkeyPatch):
    default = tmp_path / ".flowly"
    root = default / "profiles"
    monkeypatch.setattr(profiles, "_DEFAULT_HOME", default)
    monkeypatch.setattr(profiles, "_PROFILES_ROOT", root)
    default.mkdir(parents=True)
    (default / "config.json").write_text(json.dumps({"providers": {"active": "flowly"}}))
    root.mkdir()
    return default, root


@pytest.fixture
def deny_read(monkeypatch: pytest.MonkeyPatch):
    """Deny reads of one path the way the sandbox does: EPERM from stat."""

    def _deny(target: Path) -> None:
        denied = str(target)
        real_stat = pathlib.Path.stat
        real_read = pathlib.Path.read_text

        def guarded_stat(self, *args, **kwargs):
            if str(self).startswith(denied):
                raise PermissionError(1, "Operation not permitted", denied)
            return real_stat(self, *args, **kwargs)

        def guarded_read(self, *args, **kwargs):
            if str(self).startswith(denied):
                raise PermissionError(1, "Operation not permitted", denied)
            return real_read(self, *args, **kwargs)

        monkeypatch.setattr(pathlib.Path, "stat", guarded_stat)
        monkeypatch.setattr(pathlib.Path, "read_text", guarded_read)

    return _deny


def _write_bot(root: Path, profile_id: str, display_name: str) -> None:
    bot = root / profile_id
    bot.mkdir(parents=True, exist_ok=True)
    (bot / "config.json").write_text(json.dumps({"providers": {"active": "flowly"}}))
    (bot / "profile.json").write_text(json.dumps({"displayName": display_name}))


def test_creating_a_bot_publishes_the_name_its_owner_reads(profile_roots):
    profiles.create_profile("dqwdqwd", display_name="Friday")

    published = json.loads(profiles.roster_index_path().read_text(encoding="utf-8"))
    assert published["dqwdqwd"] == "Friday"


def test_renaming_a_bot_republishes_the_index(profile_roots):
    profiles.create_profile("dqwdqwd", display_name="Friday")

    profiles.update_profile_metadata("dqwdqwd", display_name="Jarvis")

    published = json.loads(profiles.roster_index_path().read_text(encoding="utf-8"))
    assert published["dqwdqwd"] == "Jarvis"


def test_deleting_a_bot_takes_its_name_out_of_the_index(profile_roots):
    profiles.create_profile("dqwdqwd", display_name="Friday")

    profiles.delete_profile("dqwdqwd")

    published = json.loads(profiles.roster_index_path().read_text(encoding="utf-8"))
    assert "dqwdqwd" not in published


def test_a_sibling_we_may_not_read_is_still_named(profile_roots, deny_read):
    default, root = profile_roots
    _write_bot(root, "dqwdqwd", "Friday")
    profiles.refresh_roster_index()
    # Now become a bot that is denied its sibling, as Desktop runs one.
    deny_read(root / "dqwdqwd")

    assert profiles.profile_display_names(["dqwdqwd"]) == {"dqwdqwd": "Friday"}
    assert profiles.resolve_profile_reference("Friday") == "dqwdqwd"
    # The id keeps working too: the policy withholds what is inside a
    # profile, not the fact that it is there.
    assert profiles.resolve_profile_reference("dqwdqwd") == "dqwdqwd"


def test_an_entry_for_a_bot_that_is_gone_names_nobody(profile_roots):
    default, _ = profile_roots
    profiles.roster_index_path().write_text(json.dumps({"deleted-bot": "Ghost"}))

    named = profiles.profile_display_names()
    assert "deleted-bot" not in named
    # The main bot stays nameable; it is the one profile always readable.
    assert named == {"default": "Flowly"}
    assert profiles.resolve_profile_reference("Ghost") is None


def test_a_profile_we_can_read_outranks_the_index(profile_roots):
    _, root = profile_roots
    _write_bot(root, "dqwdqwd", "Friday")
    profiles.roster_index_path().write_text(json.dumps({"dqwdqwd": "Stale Name"}))

    assert profiles.profile_display_names(["dqwdqwd"]) == {"dqwdqwd": "Friday"}


def test_a_damaged_index_reads_as_no_index(profile_roots):
    profiles.roster_index_path().write_text("{not json at all")

    assert profiles.read_roster_index() == {}
    # Falls back to what this profile can see for itself.
    assert profiles.profile_display_names() == {"default": "Flowly"}


def test_publishing_never_fails_the_change_that_prompted_it(profile_roots, deny_read):
    default, _ = profile_roots
    deny_read(profiles.roster_index_path())

    # A bot cannot write the primary's home, and a failure to publish must
    # not become a failure to create.
    profiles.refresh_roster_index()
