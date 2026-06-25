"""H5 — generated media (~/.flowly/media) must not grow without bound.

The disk-cleanup plugin deliberately protects ~/.flowly/media, so image
generation accumulates ``img-*.png`` forever and eventually fills the disk.
``prune_media`` reclaims it at gateway start with an age cap (keep recent so
chat-history re-fetch still works) plus a size cap as a backstop.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from flowly.media.retention import prune_media


def _make(path: Path, *, size: int = 1024, age_days: float = 0.0) -> Path:
    path.write_bytes(b"x" * size)
    mt = time.time() - age_days * 86_400
    os.utime(path, (mt, mt))
    return path


def test_age_cap_deletes_old_keeps_recent(tmp_path):
    d = tmp_path / "media"
    d.mkdir()
    old = _make(d / "img-old.png", age_days=40)
    new = _make(d / "img-new.png", age_days=1)

    summary = prune_media(d, retention_days=30, max_size_mb=0)

    assert not old.exists()
    assert new.exists()
    assert summary["deleted_files"] == 1
    assert summary["remaining_files"] == 1


def test_size_cap_deletes_oldest_first(tmp_path):
    d = tmp_path / "media"
    d.mkdir()
    files = []
    for i in range(3):  # i=0 oldest
        f = _make(d / f"img-{i}.png", size=1_000_000, age_days=10 - i)
        files.append(f)

    # ~3 MB total, 2 MB cap → oldest deleted until under cap.
    prune_media(d, retention_days=-1, max_size_mb=2)

    assert not files[0].exists()   # oldest reclaimed first
    assert files[2].exists()       # newest kept


def test_nonexistent_dir_is_safe(tmp_path):
    summary = prune_media(tmp_path / "nope", retention_days=30)
    assert summary["skipped"] is False
    assert summary["deleted_files"] == 0


def test_disabled_caps_keep_everything(tmp_path):
    d = tmp_path / "media"
    d.mkdir()
    f = _make(d / "img-ancient.png", age_days=999)
    summary = prune_media(d, retention_days=-1, max_size_mb=0)
    assert f.exists()
    assert summary["deleted_files"] == 0


def test_subdirectories_are_left_alone(tmp_path):
    d = tmp_path / "media"
    (d / "keep").mkdir(parents=True)
    nested = _make(d / "keep" / "img-old.png", age_days=999)
    prune_media(d, retention_days=30, max_size_mb=0)
    assert nested.exists()  # only top-level files are pruned
