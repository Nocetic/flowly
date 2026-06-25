"""Audit log retention tests.

Cover the two-tier policy (age cap + size cap), disabled caps, malformed
filenames, and the "never raises" contract — startup must never fail
because of an audit pruning hiccup.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from flowly.audit.retention import prune_audit_logs


def _make_audit_file(audit_dir: Path, name: str, mtime_days_ago: float, size_bytes: int = 100) -> Path:
    """Create an audit file with a synthetic mtime and size."""
    path = audit_dir / name
    path.write_bytes(b"x" * size_bytes)
    target = time.time() - (mtime_days_ago * 86_400)
    os.utime(path, (target, target))
    return path


def test_no_audit_dir_is_safe(tmp_path: Path):
    """Pruning a missing dir returns empty summary, no error."""
    summary = prune_audit_logs(tmp_path / "missing", retention_days=30, max_size_mb=10)
    assert summary["deleted_files"] == 0
    assert summary["remaining_files"] == 0


def test_empty_audit_dir(tmp_path: Path):
    """Empty dir → nothing happens."""
    audit_dir = tmp_path / "audit"
    audit_dir.mkdir()
    summary = prune_audit_logs(audit_dir, retention_days=30, max_size_mb=10)
    assert summary["deleted_files"] == 0
    assert summary["remaining_files"] == 0


def test_age_cap_deletes_old_files(tmp_path: Path):
    """Files older than retention_days are removed; recent files survive."""
    audit_dir = tmp_path / "audit"
    audit_dir.mkdir()

    _make_audit_file(audit_dir, "2025-01-01.jsonl", mtime_days_ago=120)
    _make_audit_file(audit_dir, "2025-12-01.jsonl", mtime_days_ago=100)
    _make_audit_file(audit_dir, "2026-04-01.jsonl", mtime_days_ago=20)
    _make_audit_file(audit_dir, "2026-04-25.jsonl", mtime_days_ago=0)

    summary = prune_audit_logs(audit_dir, retention_days=90, max_size_mb=0)

    assert summary["deleted_files"] == 2
    assert summary["remaining_files"] == 2
    assert (audit_dir / "2026-04-01.jsonl").exists()
    assert (audit_dir / "2026-04-25.jsonl").exists()
    assert not (audit_dir / "2025-01-01.jsonl").exists()
    assert not (audit_dir / "2025-12-01.jsonl").exists()


def test_age_cap_disabled(tmp_path: Path):
    """retention_days=-1 keeps every file regardless of age."""
    audit_dir = tmp_path / "audit"
    audit_dir.mkdir()
    _make_audit_file(audit_dir, "2020-01-01.jsonl", mtime_days_ago=2_000)

    summary = prune_audit_logs(audit_dir, retention_days=-1, max_size_mb=0)
    assert summary["deleted_files"] == 0
    assert (audit_dir / "2020-01-01.jsonl").exists()


def test_size_cap_evicts_oldest(tmp_path: Path):
    """When the folder is over the size cap, the oldest file goes first."""
    audit_dir = tmp_path / "audit"
    audit_dir.mkdir()

    # 3 MB total across three files; cap is 2 MB.
    _make_audit_file(audit_dir, "2026-04-01.jsonl", mtime_days_ago=20, size_bytes=1_048_576)
    _make_audit_file(audit_dir, "2026-04-15.jsonl", mtime_days_ago=10, size_bytes=1_048_576)
    _make_audit_file(audit_dir, "2026-04-25.jsonl", mtime_days_ago=0, size_bytes=1_048_576)

    summary = prune_audit_logs(audit_dir, retention_days=-1, max_size_mb=2)

    assert summary["deleted_files"] == 1
    assert not (audit_dir / "2026-04-01.jsonl").exists()
    assert (audit_dir / "2026-04-15.jsonl").exists()
    assert (audit_dir / "2026-04-25.jsonl").exists()


def test_size_cap_disabled(tmp_path: Path):
    """max_size_mb=0 keeps everything regardless of size."""
    audit_dir = tmp_path / "audit"
    audit_dir.mkdir()
    _make_audit_file(audit_dir, "2026-04-25.jsonl", mtime_days_ago=0, size_bytes=10 * 1_048_576)

    summary = prune_audit_logs(audit_dir, retention_days=-1, max_size_mb=0)
    assert summary["deleted_files"] == 0
    assert (audit_dir / "2026-04-25.jsonl").exists()


def test_age_then_size(tmp_path: Path):
    """Age runs first, then size — order matters when both fire."""
    audit_dir = tmp_path / "audit"
    audit_dir.mkdir()

    # Three old (will be age-pruned), two recent (one will be size-pruned).
    for i in range(3):
        _make_audit_file(audit_dir, f"2025-01-0{i+1}.jsonl", mtime_days_ago=200, size_bytes=1_048_576)
    _make_audit_file(audit_dir, "2026-04-15.jsonl", mtime_days_ago=10, size_bytes=2 * 1_048_576)
    _make_audit_file(audit_dir, "2026-04-25.jsonl", mtime_days_ago=0, size_bytes=2 * 1_048_576)

    summary = prune_audit_logs(audit_dir, retention_days=90, max_size_mb=2)

    # 3 deleted by age + 1 by size = 4 total
    assert summary["deleted_files"] == 4
    assert summary["remaining_files"] == 1
    assert (audit_dir / "2026-04-25.jsonl").exists()


def test_non_audit_files_ignored(tmp_path: Path):
    """Files that don't match YYYY-MM-DD.jsonl are left untouched."""
    audit_dir = tmp_path / "audit"
    audit_dir.mkdir()
    (audit_dir / "README.md").write_text("hi")
    (audit_dir / "weird-name.jsonl").write_text("{}")
    (audit_dir / "2024-99-99.jsonl").write_text("{}")  # invalid month/day, but matches regex
    _make_audit_file(audit_dir, "2026-04-25.jsonl", mtime_days_ago=0)

    prune_audit_logs(audit_dir, retention_days=30, max_size_mb=0)

    assert (audit_dir / "README.md").exists()
    assert (audit_dir / "weird-name.jsonl").exists()
    # Non-conforming names are ignored entirely; only the matching one survives age check.


def test_never_raises_on_permission_error(tmp_path: Path, monkeypatch):
    """Even if unlink fails, prune returns a summary instead of raising."""
    audit_dir = tmp_path / "audit"
    audit_dir.mkdir()
    _make_audit_file(audit_dir, "2025-01-01.jsonl", mtime_days_ago=200)

    real_unlink = Path.unlink

    def boom(self, *args, **kwargs):
        raise PermissionError("denied")

    monkeypatch.setattr(Path, "unlink", boom)
    try:
        summary = prune_audit_logs(audit_dir, retention_days=90, max_size_mb=0)
    finally:
        monkeypatch.setattr(Path, "unlink", real_unlink)

    # File still there because unlink failed, but prune did not crash.
    assert summary["deleted_files"] == 0
    assert (audit_dir / "2025-01-01.jsonl").exists()


def test_summary_shape(tmp_path: Path):
    """Summary keys are stable — UI / RPC depends on them."""
    audit_dir = tmp_path / "audit"
    audit_dir.mkdir()
    _make_audit_file(audit_dir, "2026-04-25.jsonl", mtime_days_ago=0, size_bytes=1024)

    summary = prune_audit_logs(audit_dir, retention_days=30, max_size_mb=10)
    assert set(summary.keys()) == {
        "deleted_files",
        "deleted_bytes",
        "remaining_files",
        "remaining_bytes",
        "skipped",
    }
    assert summary["remaining_files"] == 1
    assert summary["remaining_bytes"] == 1024
    assert summary["skipped"] is False
