"""Generated-media retention — reclaiming ``~/.flowly/media`` without breaking it.

Generation tools drop files here and the delivery path serves them from here, so
nothing else reclaims the folder: the disk-cleanup plugin deliberately protects
it. Left alone on an always-on agent it grows until the disk fills.

Three decisions shape this module.

**A clip and its poster are ONE thing.** ``vid-abc.mp4`` and ``vid-abc.jpg`` are
two files but a single attachment, and deleting one without the other leaves a
video with no preview or a preview with no video. Pruning therefore works on
*units*, never on loose files.

**Age is about relevance; size is about disk.** So there is one age cap for
everything — a month-old clip is as stale as a month-old screenshot — and
SEPARATE size budgets per kind. Video is one to two orders of magnitude larger
than an image, and a shared budget means generating video silently evicts
screenshots somebody still wanted.

**Nothing deletes what was just made.** Callers pruning right after a generation
pass the new files as ``protect``, so a clip larger than its own budget fails
loudly at the quota rather than vanishing the moment it lands.

Every cap is best-effort and never raises — pruning must not block startup or a
reply. ``retention_days=-1`` disables the age cap; a size budget of ``0``
disables that budget. Only regular files directly inside the media dir are
touched; subdirectories belong to other code.
"""

from __future__ import annotations

import mimetypes
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from loguru import logger as _logger

DEFAULT_RETENTION_DAYS = 30

# Separate budgets, sized to what each kind actually costs. A generated clip
# runs from under a megabyte to tens of megabytes depending on length and
# resolution; images are a fraction of that. The image budget is unchanged from
# when this folder held nothing else, so no existing install suddenly prunes
# differently because video arrived.
DEFAULT_IMAGE_MAX_SIZE_MB = 500
DEFAULT_VIDEO_MAX_SIZE_MB = 2000

#: Bucket a kind falls into for the size cap. Audio and stray files share the
#: image budget: both are small, and giving every kind its own knob would be
#: more configuration than anyone wants to reason about.
_BUDGET_FOR_KIND = {"video": "video", "image": "image", "audio": "image", "file": "image"}


@dataclass(frozen=True, slots=True)
class MediaUnit:
    """One attachment on disk: a primary file plus any sidecars it owns."""

    paths: tuple[Path, ...]
    kind: str
    size: int
    #: The NEWEST member's mtime. A unit is as fresh as its freshest file, so
    #: regenerating a poster can't make the clip look stale.
    mtime: float

    @property
    def budget(self) -> str:
        return _BUDGET_FOR_KIND.get(self.kind, "image")


def _kind_of(path: Path) -> str:
    mime, _ = mimetypes.guess_type(str(path))
    mime = mime or ""
    if mime.startswith("video/"):
        return "video"
    if mime.startswith("image/"):
        return "image"
    if mime.startswith("audio/"):
        return "audio"
    return "file"


def _safe_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _safe_mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        # Unreadable: treat as brand new so a stat failure never causes a
        # deletion it wouldn't otherwise have caused.
        return time.time()


def media_units(media_dir: Path) -> list[MediaUnit]:
    """Group the media directory into deletable units, oldest first.

    A video or audio file absorbs same-stem IMAGE files as sidecars — that is
    exactly the poster relationship, and nothing else in this folder produces
    it. Anything not in that relationship stays its own unit, so two unrelated
    files that happen to share a stem (``cat.jpg`` and ``cat.png``) are never
    deleted together.
    """
    if not media_dir.exists() or not media_dir.is_dir():
        return []
    try:
        files = [p for p in media_dir.iterdir() if p.is_file()]
    except OSError as exc:
        _logger.debug("[Media] retention: list failed for {}: {}", media_dir, exc)
        return []

    by_stem: dict[str, list[Path]] = {}
    for path in files:
        by_stem.setdefault(path.stem, []).append(path)

    units: list[MediaUnit] = []
    for members in by_stem.values():
        primaries = [p for p in members if _kind_of(p) in ("video", "audio")]
        sidecars = [p for p in members if _kind_of(p) == "image"]

        if len(primaries) == 1 and len(primaries) + len(sidecars) == len(members):
            grouped = (primaries[0], *sidecars)
            units.append(
                MediaUnit(
                    paths=grouped,
                    kind=_kind_of(primaries[0]),
                    size=sum(_safe_size(p) for p in grouped),
                    mtime=max(_safe_mtime(p) for p in grouped),
                )
            )
            continue

        # No poster relationship — every file stands alone.
        for path in members:
            units.append(
                MediaUnit(
                    paths=(path,),
                    kind=_kind_of(path),
                    size=_safe_size(path),
                    mtime=_safe_mtime(path),
                )
            )

    units.sort(key=lambda u: u.mtime)
    return units


def _delete(unit: MediaUnit) -> int:
    """Delete every file in a unit. Returns the bytes actually reclaimed."""
    freed = 0
    for path in unit.paths:
        size = _safe_size(path)
        try:
            path.unlink()
            freed += size
        except OSError as exc:
            _logger.debug("[Media] retention: unlink failed for {}: {}", path, exc)
    return freed


def prune_media(
    media_dir: Path,
    retention_days: int = DEFAULT_RETENTION_DAYS,
    image_max_size_mb: int = DEFAULT_IMAGE_MAX_SIZE_MB,
    video_max_size_mb: int = DEFAULT_VIDEO_MAX_SIZE_MB,
    protect: Iterable[Path] = (),
) -> dict:
    """Trim old / oversized generated media. Never raises.

    Returns a summary the caller can log. ``protect`` names files that must
    survive regardless — what a caller just produced, so pruning immediately
    after a generation can never delete the thing that triggered it.
    """
    summary = {
        "deleted_units": 0,
        "deleted_bytes": 0,
        "remaining_units": 0,
        "remaining_bytes": 0,
        "skipped": False,
    }

    protected = {Path(p).resolve() for p in protect}

    def _is_protected(unit: MediaUnit) -> bool:
        return any(p.resolve() in protected for p in unit.paths)

    try:
        units = media_units(media_dir)
    except OSError as exc:
        _logger.debug("[Media] retention: scan failed for {}: {}", media_dir, exc)
        summary["skipped"] = True
        return summary
    if not units:
        return summary

    # ── 1. Age cap — one rule for every kind ────────────────────────────
    survivors: list[MediaUnit] = []
    if retention_days >= 0:
        cutoff = time.time() - (retention_days * 86_400)
        for unit in units:
            if unit.mtime < cutoff and not _is_protected(unit):
                freed = _delete(unit)
                if freed:
                    summary["deleted_units"] += 1
                    summary["deleted_bytes"] += freed
            else:
                survivors.append(unit)
    else:
        survivors = list(units)

    # ── 2. Size cap — one budget per kind ───────────────────────────────
    budgets = {
        "image": image_max_size_mb * 1024 * 1024 if image_max_size_mb > 0 else None,
        "video": video_max_size_mb * 1024 * 1024 if video_max_size_mb > 0 else None,
    }
    totals: dict[str, int] = {"image": 0, "video": 0}
    for unit in survivors:
        totals[unit.budget] += unit.size

    kept: list[MediaUnit] = []
    for unit in survivors:  # oldest first
        budget = budgets.get(unit.budget)
        over = budget is not None and totals[unit.budget] > budget
        if over and not _is_protected(unit):
            freed = _delete(unit)
            if freed:
                summary["deleted_units"] += 1
                summary["deleted_bytes"] += freed
            totals[unit.budget] -= unit.size
        else:
            kept.append(unit)

    summary["remaining_units"] = len(kept)
    summary["remaining_bytes"] = sum(u.size for u in kept)
    return summary
