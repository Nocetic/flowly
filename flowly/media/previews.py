"""A document's preview: its first page, small, for the bubble.

A file the agent gives shows what it is before anyone opens it, as an image
does: a PDF's first page drawn with pymupdf (already a dependency, for reading
PDFs); an Office document's through Quick Look where there is one (macOS
``qlmanage``). Anywhere else, and for every other type, there is none and the
apps draw the file's type instead.

The preview is a JPEG of at most ``MAX_DIMENSION`` pixels and about
``TARGET_BYTES``, the size of an image's inline thumbnail. Cached by path,
size and time, so a history reload does not draw it again.
"""

from __future__ import annotations

import base64
import io
import shutil
import subprocess
import sys
import tempfile
from collections import OrderedDict
from pathlib import Path

from loguru import logger

from flowly.media.assets import guess_mime

MAX_DIMENSION = 512
TARGET_BYTES = 48 * 1024
#: Quick Look on a document that never answers must not hold a reply.
QUICK_LOOK_TIMEOUT_SECONDS = 10

_OFFICE_TYPES = {
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "application/msword",
    "application/vnd.ms-excel",
    "application/vnd.ms-powerpoint",
    "application/rtf",
}

_CACHE: "OrderedDict[tuple, str | None]" = OrderedDict()
_CACHE_MAX = 128


def document_preview_b64(path: Path) -> str | None:
    """Base64 JPEG of a document's first page, or None when there is none."""
    try:
        info = path.stat()
    except OSError:
        return None
    key = (str(path), info.st_size, info.st_mtime_ns)
    if key in _CACHE:
        _CACHE.move_to_end(key)
        return _CACHE[key]
    mime = guess_mime(path)
    try:
        if mime == "application/pdf":
            image = _pdf_first_page(path)
        elif mime in _OFFICE_TYPES:
            image = _quick_look(path)
        else:
            image = None
        preview = _jpeg_b64(image) if image is not None else None
    except Exception as exc:  # a broken file has no preview, never a broken reply
        logger.info(f"[media] no preview for {path.name}: {type(exc).__name__}")
        preview = None
    _CACHE[key] = preview
    while len(_CACHE) > _CACHE_MAX:
        _CACHE.popitem(last=False)
    return preview


def _pdf_first_page(path: Path):
    import pymupdf
    from PIL import Image

    with pymupdf.open(path) as document:
        if document.needs_pass or document.page_count == 0:
            return None
        page = document[0]
        scale = MAX_DIMENSION / max(page.rect.width, page.rect.height, 1)
        pixmap = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False)
        return Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)


def _quick_look(path: Path):
    """The thumbnail Finder shows, from macOS's own Quick Look; None elsewhere."""
    if sys.platform != "darwin" or not shutil.which("qlmanage"):
        return None
    from PIL import Image

    with tempfile.TemporaryDirectory(prefix="flowly-preview-") as out:
        subprocess.run(
            ["qlmanage", "-t", "-s", str(MAX_DIMENSION), "-o", out, str(path)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=QUICK_LOOK_TIMEOUT_SECONDS, check=False,
        )
        made = Path(out) / f"{path.name}.png"
        if not made.is_file():
            return None
        with Image.open(made) as image:
            return image.convert("RGB")


def _jpeg_b64(image) -> str | None:
    image.thumbnail((MAX_DIMENSION, MAX_DIMENSION))
    for quality in (75, 60, 45, 30):
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=quality, optimize=True)
        if buffer.tell() <= TARGET_BYTES:
            return base64.b64encode(buffer.getvalue()).decode("ascii")
    return None
