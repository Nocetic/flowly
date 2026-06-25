"""Attachment helpers for the local TUI composer."""

from __future__ import annotations

import mimetypes
import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlparse

IMAGE_EXTENSIONS = {
    ".apng",
    ".avif",
    ".bmp",
    ".gif",
    ".heic",
    ".heif",
    ".jpeg",
    ".jpg",
    ".png",
    ".tif",
    ".tiff",
    ".webp",
}

VIDEO_EXTENSIONS = {
    ".avi",
    ".m4v",
    ".mkv",
    ".mov",
    ".mp4",
    ".mpeg",
    ".mpg",
    ".webm",
}


@dataclass(frozen=True)
class FileDrop:
    path: Path
    remainder: str = ""
    kind: str = "image"


def is_image_path(path: Path) -> bool:
    return path.suffix.lower() in IMAGE_EXTENSIONS


def is_video_path(path: Path) -> bool:
    return path.suffix.lower() in VIDEO_EXTENSIONS


def media_marker(path: Path) -> str:
    return "[video]" if is_video_path(path) else "[image]"


def build_attachment(path: Path) -> dict[str, str]:
    mime, _ = mimetypes.guess_type(str(path))
    return {
        "filePath": str(path),
        "fileName": path.name,
        "mimeType": mime or "application/octet-stream",
    }


def format_attachment_labels(paths: list[Path]) -> str:
    if not paths:
        return ""
    labels = [f"{media_marker(p)} {p.name}" for p in paths[:3]]
    if len(paths) > 3:
        labels.append(f"+{len(paths) - 3} more")
    return "  ".join(labels)


def render_message_with_attachments(text: str, paths: list[Path]) -> str:
    text = text.strip()
    if not paths:
        return text
    prefix = " ".join(media_marker(path) for path in paths)
    return f"{prefix} {text}".strip()


def detect_media_drop(
    raw: str,
    *,
    base_dir: Path | None = None,
    allow_bare: bool = False,
) -> FileDrop | None:
    return _detect_drop(raw, base_dir=base_dir, allow_bare=allow_bare, kinds=("image", "video"))


def detect_image_drop(
    raw: str,
    *,
    base_dir: Path | None = None,
    allow_bare: bool = False,
) -> FileDrop | None:
    """Detect a pasted/dropped image path at the start of ``raw``.

    Terminal drag-and-drop usually arrives as pasted text, not as a real OS
    drop event. This parser accepts quoted paths, file:// URLs, escaped spaces,
    absolute paths, home-relative paths and common relative forms.
    """
    return _detect_drop(raw, base_dir=base_dir, allow_bare=allow_bare, kinds=("image",))


def detect_video_drop(
    raw: str,
    *,
    base_dir: Path | None = None,
    allow_bare: bool = False,
) -> FileDrop | None:
    return _detect_drop(raw, base_dir=base_dir, allow_bare=allow_bare, kinds=("video",))


def _detect_drop(
    raw: str,
    *,
    base_dir: Path | None,
    allow_bare: bool,
    kinds: tuple[str, ...],
) -> FileDrop | None:
    text = raw.strip()
    if not text or (not _looks_path_like(text) and not allow_bare):
        return None

    for candidate, remainder in _candidate_prefixes(text):
        path = _resolve_path(candidate, base_dir=base_dir)
        if not path or not path.is_file():
            continue
        if "image" in kinds and is_image_path(path):
            return FileDrop(path=path, remainder=remainder.strip(), kind="image")
        if "video" in kinds and is_video_path(path):
            return FileDrop(path=path, remainder=remainder.strip(), kind="video")
    return None


def _looks_path_like(text: str) -> bool:
    probe = text.lstrip()
    if not probe:
        return False
    if probe[0] in ("'", '"') and len(probe) > 1:
        probe = probe[1:].lstrip()
    return (
        probe.startswith(("/", "~", "./", "../", "file://"))
        or bool(re.match(r"^[A-Za-z]:[\\/]", probe))
    )


def _candidate_prefixes(text: str) -> list[tuple[str, str]]:
    candidates: list[tuple[str, str]] = []
    quoted = _split_quoted(text)
    if quoted:
        candidates.append(quoted)
    simple = _split_simple(text)
    if simple:
        candidates.append(simple)

    # Some terminals paste paths with literal spaces. Walk longest to shortest
    # so "/tmp/My Image.png describe" can still bind "/tmp/My Image.png".
    parts = text.split()
    for i in range(len(parts), 0, -1):
        prefix = " ".join(parts[:i])
        remainder = " ".join(parts[i:])
        candidates.append((prefix, remainder))

    seen: set[tuple[str, str]] = set()
    unique: list[tuple[str, str]] = []
    for candidate in candidates:
        if candidate[0] and candidate not in seen:
            seen.add(candidate)
            unique.append(candidate)
    return unique


def _split_quoted(text: str) -> tuple[str, str] | None:
    quote = text[0]
    if quote not in ("'", '"'):
        return None
    escaped = False
    buf: list[str] = []
    for idx, ch in enumerate(text[1:], start=1):
        if escaped:
            buf.append(ch)
            escaped = False
            continue
        if ch == "\\":
            escaped = True
            continue
        if ch == quote:
            return "".join(buf), text[idx + 1 :]
        buf.append(ch)
    return None


def _split_simple(text: str) -> tuple[str, str] | None:
    escaped = False
    buf: list[str] = []
    for idx, ch in enumerate(text):
        if escaped:
            buf.append(ch)
            escaped = False
            continue
        if ch == "\\":
            escaped = True
            continue
        if ch.isspace():
            return "".join(buf), text[idx + 1 :]
        buf.append(ch)
    return "".join(buf), ""


def _resolve_path(raw: str, *, base_dir: Path | None = None) -> Path | None:
    value = raw.strip()
    if not value:
        return None
    if value.startswith("file://"):
        parsed = urlparse(value)
        value = unquote(parsed.path)
    value = os.path.expandvars(value)
    try:
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = (base_dir or Path.cwd()) / path
        return path.resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        return None
