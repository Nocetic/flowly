"""The test session can never reach the owner's real Flowly home."""

from __future__ import annotations

import os
import pwd
from pathlib import Path

import flowly.profile as profiles


def _real_home() -> Path:
    # From the account database, not HOME, which the session redirects.
    return Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()


def test_the_fallback_home_is_not_the_owners() -> None:
    real = _real_home() / ".flowly"
    assert profiles._DEFAULT_HOME.resolve() != real
    assert real not in profiles._PROFILES_ROOT.resolve().parents


def test_the_active_home_is_not_the_owners() -> None:
    real = _real_home() / ".flowly"
    active = profiles.get_flowly_home().resolve()
    assert active != real
    assert real not in active.parents


def test_even_without_flowly_home_nothing_lands_in_the_owners_home(monkeypatch) -> None:
    monkeypatch.delenv("FLOWLY_HOME", raising=False)
    assert profiles.get_flowly_home().resolve() != _real_home() / ".flowly"
