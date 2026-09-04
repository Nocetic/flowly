"""Profile enumeration under a sandbox that denies the primary profile.

A named bot is a security boundary: Desktop runs it under a policy that
denies reading ``~/.flowly``. macOS reports that denial as ``EPERM`` from
``stat`` — which ``Path.exists()`` raises rather than swallows, because it
only treats "no such file" errors as an answer. Listing profiles from
inside a bot therefore used to end a chat turn with

    [Errno 1] Operation not permitted: '/Users/…/.flowly/config.json'

on screen. Being denied is the policy working, so enumeration has to have
an answer for it.
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
    bot = root / "testbot"
    bot.mkdir(parents=True)
    (bot / "config.json").write_text(json.dumps({"providers": {"active": "flowly"}}))
    (bot / "profile.json").write_text(json.dumps({"displayName": "TestBot"}))
    return default, root


@pytest.fixture
def deny_read(monkeypatch: pytest.MonkeyPatch):
    """Deny every read of one path the way a sandbox does: EPERM from stat."""

    def _deny(target: Path) -> None:
        # Compared as text: resolving inside the hook would stat its way back
        # into the hook.
        denied = str(target)
        real_stat = pathlib.Path.stat
        real_read = pathlib.Path.read_text

        def guarded_stat(self, *args, **kwargs):
            if str(self) == denied:
                raise PermissionError(1, "Operation not permitted", denied)
            return real_stat(self, *args, **kwargs)

        def guarded_read(self, *args, **kwargs):
            if str(self) == denied:
                raise PermissionError(1, "Operation not permitted", denied)
            return real_read(self, *args, **kwargs)

        monkeypatch.setattr(pathlib.Path, "stat", guarded_stat)
        monkeypatch.setattr(pathlib.Path, "read_text", guarded_read)

    return _deny


def test_listing_survives_a_denied_primary_config(profile_roots, deny_read):
    default, _ = profile_roots
    deny_read(default / "config.json")

    listed = profiles.list_profiles()

    names = [p.name for p in listed]
    assert names == ["default", "testbot"]
    primary = listed[0]
    # Unreadable is reported as absent, not as a crash and not as present.
    assert primary.has_config is False
    assert primary.provider == ""
    assert primary.model == ""


def test_display_names_survive_a_denied_primary_config(profile_roots, deny_read):
    default, _ = profile_roots
    deny_read(default / "config.json")

    # This is the chat.send path: a bot resolving the name its owner typed.
    assert profiles.profile_display_names(["testbot"]) == {"testbot": "TestBot"}


def test_denied_config_is_not_reported_as_malformed(profile_roots, deny_read):
    default, _ = profile_roots
    deny_read(default / "config.json")

    # A denial is an OSError, so it stays inside the ValueError contract the
    # callers already handle rather than escaping as PermissionError.
    with pytest.raises(ValueError):
        profiles._load_config_object(default / "config.json")


def test_missing_config_still_reads_as_empty(profile_roots):
    default, _ = profile_roots
    assert profiles._load_config_object(default / "nope.json") == {}


def test_listing_skips_a_sibling_it_may_not_examine(profile_roots, deny_read):
    _, root = profile_roots
    # A bot is denied its siblings outright: stat on the directory fails, so
    # is_dir() raises where it would normally answer.
    deny_read(root / "testbot")

    listed = profiles.list_profiles()

    # Nothing true can be said about a sibling we cannot read, so it is left
    # out — and the listing still answers for everything else.
    assert [p.name for p in listed] == ["default"]


def test_display_names_survive_a_denied_sibling(profile_roots, deny_read):
    _, root = profile_roots
    deny_read(root / "testbot")

    assert profiles.profile_display_names(["testbot"]) == {}
