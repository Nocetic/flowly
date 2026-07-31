"""Best-effort probing for generated media — dimensions, duration, poster frames.

Pillow already ships with Flowly (the transport compressor needs it), so image
dimensions are always available. Video is different: ``ffprobe``/``ffmpeg`` are
NOT Flowly dependencies, and requiring them would make video generation fail on
a machine that is otherwise perfectly able to produce and deliver an MP4.

So every function here degrades instead of raising. A missing width/duration
means "not known", never "bad file", and a missing poster means the client falls
back to its own placeholder. Callers must treat the optional values as hints.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from loguru import logger

# Probing a local file is fast; a hang here would stall a whole agent turn.
_PROBE_TIMEOUT = 20
_POSTER_TIMEOUT = 60

# Where in the clip to grab the poster. Frame 0 of a generated video is often a
# fade-in from black, which makes a useless bubble preview.
_POSTER_OFFSET_SECONDS = 1.0
_POSTER_MAX_WIDTH = 1280


@dataclass(frozen=True, slots=True)
class MediaProbe:
    """What we could learn about a media file. Every field is optional."""

    width: int | None = None
    height: int | None = None
    duration_ms: int | None = None


def have_ffprobe() -> bool:
    return shutil.which("ffprobe") is not None


def have_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None


def probe_image(path: Path) -> MediaProbe:
    """Image dimensions via Pillow. Empty probe if Pillow can't open the file."""
    try:
        from PIL import Image

        with Image.open(path) as img:
            w, h = img.size
        return MediaProbe(width=int(w), height=int(h))
    except Exception as exc:  # noqa: BLE001 - probing must never break a turn
        logger.debug("[media] image probe failed for {}: {}", path, exc)
        return MediaProbe()


def probe_video(path: Path) -> MediaProbe:
    """Video dimensions + duration via ffprobe. Empty probe when unavailable."""
    if not have_ffprobe():
        return MediaProbe()
    cmd = [
        "ffprobe",
        "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height:format=duration",
        "-of", "json",
        str(path),
    ]
    try:
        proc = subprocess.run(  # noqa: S603 - fixed argv, no shell, local path
            cmd, capture_output=True, text=True, timeout=_PROBE_TIMEOUT, check=False
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("[media] ffprobe failed for {}: {}", path, exc)
        return MediaProbe()
    if proc.returncode != 0:
        logger.debug("[media] ffprobe returned {} for {}", proc.returncode, path)
        return MediaProbe()
    try:
        data = json.loads(proc.stdout or "{}")
    except (json.JSONDecodeError, ValueError):
        return MediaProbe()

    streams = data.get("streams") or []
    stream = streams[0] if isinstance(streams, list) and streams else {}
    width = stream.get("width") if isinstance(stream, dict) else None
    height = stream.get("height") if isinstance(stream, dict) else None

    duration_ms: int | None = None
    fmt = data.get("format")
    raw_duration = fmt.get("duration") if isinstance(fmt, dict) else None
    if raw_duration is not None:
        try:
            duration_ms = max(0, int(round(float(raw_duration) * 1000)))
        except (TypeError, ValueError):
            duration_ms = None

    return MediaProbe(
        width=int(width) if isinstance(width, int) else None,
        height=int(height) if isinstance(height, int) else None,
        duration_ms=duration_ms,
    )


def probe(path: Path, mime_type: str) -> MediaProbe:
    """Probe by media kind. Unknown kinds return an empty probe."""
    if mime_type.startswith("image/"):
        return probe_image(path)
    if mime_type.startswith("video/"):
        return probe_video(path)
    return MediaProbe()


def extract_poster(video_path: Path, dest: Path) -> bool:
    """Write a JPEG poster frame for *video_path* to *dest*.

    Returns True only when a non-empty file was actually produced — a caller
    must not advertise a poster it can't serve. Without ffmpeg this is a no-op
    returning False, which is a supported state (clients render a play badge
    over a plain background instead).
    """
    if not have_ffmpeg():
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    # ``-ss`` before ``-i`` seeks by keyframe (fast). If the clip is shorter than
    # the offset ffmpeg produces nothing, so we retry from the very first frame.
    for offset in (_POSTER_OFFSET_SECONDS, 0.0):
        cmd = [
            "ffmpeg", "-y", "-loglevel", "error",
            "-ss", str(offset),
            "-i", str(video_path),
            "-frames:v", "1",
            "-vf", f"scale='min({_POSTER_MAX_WIDTH},iw)':-2",
            "-q:v", "4",
            str(dest),
        ]
        try:
            proc = subprocess.run(  # noqa: S603 - fixed argv, no shell, local paths
                cmd, capture_output=True, text=True, timeout=_POSTER_TIMEOUT, check=False
            )
        except (OSError, subprocess.SubprocessError) as exc:
            logger.debug("[media] poster extraction failed for {}: {}", video_path, exc)
            return False
        if proc.returncode == 0 and dest.is_file() and dest.stat().st_size > 0:
            return True
    # Clean up a zero-byte artefact so nothing downstream sees a "poster" file.
    try:
        if dest.is_file() and dest.stat().st_size == 0:
            dest.unlink()
    except OSError:
        pass
    return False
