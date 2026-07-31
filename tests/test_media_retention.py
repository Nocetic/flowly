"""Reclaiming ~/.flowly/media without breaking what stays.

Nothing else prunes this folder — the disk-cleanup plugin deliberately protects
it — so these rules are the only thing between an always-on agent and a full
disk. They are also the only thing between a careless sweep and a chat history
full of half-broken attachments, which is what most of these tests are about.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from flowly.media.retention import (
    DEFAULT_IMAGE_MAX_SIZE_MB,
    DEFAULT_VIDEO_MAX_SIZE_MB,
    media_units,
    prune_media,
)


def _write(path: Path, size_mb: float = 0.1, age_days: float = 0) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\0" * int(size_mb * 1024 * 1024))
    if age_days:
        old = time.time() - age_days * 86_400
        os.utime(path, (old, old))
    return path


def _names(directory: Path) -> set[str]:
    return {p.name for p in directory.iterdir() if p.is_file()}


# ── age cap ─────────────────────────────────────────────────────────────────


def test_age_cap_deletes_old_keeps_recent(tmp_path):
    _write(tmp_path / "old.png", age_days=60)
    _write(tmp_path / "new.png", age_days=1)

    summary = prune_media(tmp_path, retention_days=30, image_max_size_mb=0, video_max_size_mb=0)

    assert _names(tmp_path) == {"new.png"}
    assert summary["deleted_units"] == 1


def test_disabled_caps_keep_everything(tmp_path):
    _write(tmp_path / "ancient.png", age_days=400)
    _write(tmp_path / "huge.mp4", size_mb=5)

    prune_media(tmp_path, retention_days=-1, image_max_size_mb=0, video_max_size_mb=0)

    assert _names(tmp_path) == {"ancient.png", "huge.mp4"}


# ── a clip and its poster are ONE thing ─────────────────────────────────────


def test_a_clip_and_its_poster_are_deleted_together(tmp_path):
    """Deleting one without the other leaves a video with no preview, or a
    preview with no video — a half-broken bubble in chat history."""
    _write(tmp_path / "vid-abc.mp4", size_mb=1, age_days=60)
    _write(tmp_path / "vid-abc.jpg", size_mb=0.05, age_days=60)

    prune_media(tmp_path, retention_days=30)

    assert _names(tmp_path) == set()


def test_a_poster_cannot_be_evicted_on_its_own_by_the_size_cap(tmp_path):
    """The size cap walks oldest-first; without grouping it could stop exactly
    between a clip and its poster."""
    _write(tmp_path / "vid-a.mp4", size_mb=2)
    _write(tmp_path / "vid-a.jpg", size_mb=0.05)
    time.sleep(0.01)
    _write(tmp_path / "vid-b.mp4", size_mb=2)
    _write(tmp_path / "vid-b.jpg", size_mb=0.05)

    prune_media(tmp_path, retention_days=-1, video_max_size_mb=3)

    remaining = _names(tmp_path)
    # Whatever survived, no clip is left without its poster and no poster
    # without its clip.
    for name in remaining:
        stem = Path(name).stem
        assert {f"{stem}.mp4", f"{stem}.jpg"} <= remaining, remaining


def test_a_unit_is_as_fresh_as_its_freshest_file(tmp_path):
    """A poster regenerated today must not leave its clip looking month-old."""
    _write(tmp_path / "vid-x.mp4", age_days=60)
    _write(tmp_path / "vid-x.jpg", age_days=0)

    prune_media(tmp_path, retention_days=30)

    assert _names(tmp_path) == {"vid-x.mp4", "vid-x.jpg"}


def test_unrelated_files_sharing_a_stem_are_not_grouped(tmp_path):
    """Two uploads that happen to share a name are not a poster relationship,
    and deleting them together would be over-eager."""
    _write(tmp_path / "cat.jpg", age_days=60)
    _write(tmp_path / "cat.png", age_days=1)

    prune_media(tmp_path, retention_days=30)

    assert _names(tmp_path) == {"cat.png"}


def test_units_report_their_kind_and_combined_size(tmp_path):
    _write(tmp_path / "vid-a.mp4", size_mb=1)
    _write(tmp_path / "vid-a.jpg", size_mb=0.5)
    _write(tmp_path / "shot.png", size_mb=0.25)

    units = {u.kind: u for u in media_units(tmp_path)}

    assert set(units) == {"video", "image"}
    assert units["video"].size == int(1.5 * 1024 * 1024)
    assert len(units["video"].paths) == 2


# ── separate budgets per kind ───────────────────────────────────────────────


def test_video_does_not_evict_images(tmp_path):
    """A shared budget meant generating video silently deleted screenshots
    somebody still wanted."""
    _write(tmp_path / "keep-me.png", size_mb=1, age_days=10)
    time.sleep(0.01)
    for i in range(4):
        _write(tmp_path / f"vid-{i}.mp4", size_mb=2)

    prune_media(tmp_path, retention_days=-1, image_max_size_mb=500, video_max_size_mb=4)

    remaining = _names(tmp_path)
    assert "keep-me.png" in remaining, "the image budget was never the one under pressure"
    assert len([n for n in remaining if n.endswith(".mp4")]) <= 2


def test_images_do_not_evict_video(tmp_path):
    _write(tmp_path / "clip.mp4", size_mb=1, age_days=10)
    time.sleep(0.01)
    for i in range(4):
        _write(tmp_path / f"shot-{i}.png", size_mb=1)

    prune_media(tmp_path, retention_days=-1, image_max_size_mb=2, video_max_size_mb=2000)

    assert "clip.mp4" in _names(tmp_path)


def test_size_cap_deletes_oldest_first_within_a_budget(tmp_path):
    for i in range(4):
        _write(tmp_path / f"shot-{i}.png", size_mb=1)
        time.sleep(0.01)

    prune_media(tmp_path, retention_days=-1, image_max_size_mb=2)

    remaining = _names(tmp_path)
    assert "shot-3.png" in remaining  # newest survives
    assert "shot-0.png" not in remaining  # oldest goes first


def test_audio_and_stray_files_share_the_image_budget(tmp_path):
    _write(tmp_path / "note.mp3", size_mb=2, age_days=5)
    time.sleep(0.01)
    _write(tmp_path / "shot.png", size_mb=2)

    prune_media(tmp_path, retention_days=-1, image_max_size_mb=3)

    # One of them had to go, and it was the older one.
    assert _names(tmp_path) == {"shot.png"}


# ── nothing deletes what was just made ──────────────────────────────────────


def test_protected_files_survive_the_age_cap(tmp_path):
    _write(tmp_path / "vid-new.mp4", age_days=90)

    prune_media(tmp_path, retention_days=30, protect=[tmp_path / "vid-new.mp4"])

    assert _names(tmp_path) == {"vid-new.mp4"}


def test_a_clip_larger_than_its_budget_is_not_deleted_on_arrival(tmp_path):
    """Pruning right after a generation must not delete the thing that
    triggered it — the quota is reported by the file staying put, not enforced
    by making it vanish the moment it lands."""
    _write(tmp_path / "huge.mp4", size_mb=5)

    prune_media(
        tmp_path,
        retention_days=-1,
        video_max_size_mb=1,
        protect=[tmp_path / "huge.mp4"],
    )

    assert _names(tmp_path) == {"huge.mp4"}


def test_protection_covers_the_whole_unit(tmp_path):
    _write(tmp_path / "vid-new.mp4", age_days=90)
    _write(tmp_path / "vid-new.jpg", age_days=90)

    prune_media(tmp_path, retention_days=30, protect=[tmp_path / "vid-new.mp4"])

    assert _names(tmp_path) == {"vid-new.mp4", "vid-new.jpg"}


# ── never raises ────────────────────────────────────────────────────────────


def test_nonexistent_dir_is_safe(tmp_path):
    summary = prune_media(tmp_path / "nope", retention_days=30)
    assert summary["deleted_units"] == 0


def test_subdirectories_are_left_alone(tmp_path):
    """The folder is shared; other code owns its subdirectories."""
    (tmp_path / "jobs").mkdir()
    _write(tmp_path / "jobs" / "old.json", age_days=400)
    _write(tmp_path / "old.png", age_days=400)

    prune_media(tmp_path, retention_days=30)

    assert (tmp_path / "jobs" / "old.json").exists()
    assert not (tmp_path / "old.png").exists()


def test_defaults_give_video_the_larger_budget(tmp_path):
    """Video is one to two orders of magnitude bigger than an image; the
    defaults have to reflect that or the budgets are theatre."""
    assert DEFAULT_VIDEO_MAX_SIZE_MB > DEFAULT_IMAGE_MAX_SIZE_MB


# ── config wiring ───────────────────────────────────────────────────────────


def test_config_exposes_retention_so_clients_can_edit_it():
    """It used to be env-vars only, which meant Desktop and iOS could not show
    the setting at all."""
    from flowly.config.schema import Config

    retention = Config().media.retention
    assert retention.enabled is True
    assert retention.retention_days == 30
    assert retention.video_max_size_mb > retention.image_max_size_mb


def test_config_round_trips_through_camel_case_on_disk():
    """Keys are camelCase on disk; a block nobody can load is a block nobody
    can change."""
    from flowly.config.loader import convert_keys, convert_to_camel
    from flowly.config.schema import Config

    on_disk = convert_to_camel({
        "media": {"retention": {"enabled": False, "videoMaxSizeMb": 10}}
    })
    assert on_disk["media"]["retention"]["videoMaxSizeMb"] == 10

    parsed = Config(**convert_keys(on_disk))
    assert parsed.media.retention.enabled is False
    assert parsed.media.retention.video_max_size_mb == 10
