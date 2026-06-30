"""Gateway code-skew detection after a hot git pull."""

from __future__ import annotations

import pytest

from flowly.gateway import code_skew


@pytest.fixture(autouse=True)
def _reset():
    code_skew._reset_for_tests()
    yield
    code_skew._reset_for_tests()


def test_no_snapshot_is_never_skewed(monkeypatch):
    monkeypatch.setattr(code_skew, "_read_revision", lambda: "abc123")
    # No snapshot taken → boot revision None → never skewed.
    assert code_skew.is_skewed() is False


def test_same_revision_not_skewed(monkeypatch):
    monkeypatch.setattr(code_skew, "_read_revision", lambda: "abc123")
    code_skew.snapshot_boot_revision()
    assert code_skew.is_skewed() is False


def test_changed_revision_is_skewed(monkeypatch):
    revs = iter(["abc123", "def456"])
    monkeypatch.setattr(code_skew, "_read_revision", lambda: next(revs))
    code_skew.snapshot_boot_revision()  # snapshots abc123
    assert code_skew.is_skewed() is True  # now reads def456


def test_non_git_install_snapshots_none(monkeypatch):
    monkeypatch.setattr(code_skew, "_repo_root", lambda: None)
    code_skew.snapshot_boot_revision()
    assert code_skew._boot_revision is None
    assert code_skew.is_skewed() is False


def test_unreadable_revision_is_not_a_false_positive(monkeypatch):
    monkeypatch.setattr(code_skew, "_read_revision", lambda: "abc123")
    code_skew.snapshot_boot_revision()
    # git rev becomes unreadable on the compare → treat as not skewed.
    monkeypatch.setattr(code_skew, "_read_revision", lambda: None)
    assert code_skew.is_skewed() is False
