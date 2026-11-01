"""Every prune keeps the same files (flowly/media/retention.py prune_configured).

2026-11-01: the start-up prune spared group attachments and the one after a
generation did not; neither spared what the owner starred in the library.
Sharing files alone never pruned an agent that stays up.
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

import pytest
from PIL import Image

from flowly.media import library as library_mod
from flowly.media.assets import describe
from flowly.media.retention import prune_and_expire, prune_configured

OLD = time.time() - 40 * 24 * 3600


@pytest.fixture
def media(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    (home / "media").mkdir(parents=True)
    monkeypatch.setenv("FLOWLY_HOME", str(home))
    library_mod.reset_library_cache()
    yield home / "media"
    library_mod.reset_library_cache()


def old_file(path: Path) -> Path:
    if path.suffix == ".png":
        Image.new("RGB", (4, 4)).save(path)
    else:
        path.write_bytes(b"x" * 64)
    os.utime(path, (OLD, OLD))
    return path


def test_a_month_old_file_goes_but_a_starred_one_and_a_group_attachment_stay(media):
    gone = old_file(media / "img-old.png")
    shared = old_file(media / "report.pdf")
    starred = old_file(media / "img-keep.png")
    group = old_file(media / "group-abc-photo.png")
    library = library_mod.get_library()
    library.record([describe(starred)], source=library_mod.SOURCE_GENERATED)
    library.star("img-keep.png", True)

    summary = prune_configured()

    assert not gone.exists() and not shared.exists(), "a month-old file and a shared copy go"
    assert starred.exists(), "what the owner starred stays"
    assert group.exists(), "a group attachment is its room's to delete"
    assert {Path(p).name for p in summary["deleted_paths"]} == {"img-old.png", "report.pdf"}


def test_after_a_generation_the_same_files_stay_and_the_new_one_too(media):
    group = old_file(media / "group-abc-photo.png")
    new = old_file(media / "img-new.png")
    asyncio.run(prune_and_expire(protect=[str(new)]))
    assert group.exists() and new.exists()
