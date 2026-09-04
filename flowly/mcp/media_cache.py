"""Safely cache MCP binary content blocks and surface media as MEDIA tags.

MCP tool results may contain image, audio, or embedded binary blocks. The
agent's messaging adapters
render local files referenced by a ``MEDIA:<absolute-path>`` token in
the tool result text, so we decode the base64 payload, write it under
``$FLOWLY_HOME/media/mcp/`` and return that token.

Errors are swallowed (logged at debug): a single bad binary block must
not sink an otherwise-useful tool result. The caller falls through to
whatever text blocks parsed.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import mimetypes
import os
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_MAX_BINARY_BYTES = 25 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class CachedMCPPayload:
    """A validated MCP base64 payload persisted under Flowly's media root."""

    path: Path
    mime_type: str
    size: int

    @property
    def media_tag(self) -> str:
        return f"MEDIA:{self.path}"


def _media_dir() -> Path:
    from flowly.profile import get_flowly_home
    path = get_flowly_home() / "media" / "mcp"
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        path.chmod(0o700)
    except OSError:
        pass
    return path


def _extension_for(mime_type: str) -> str:
    normalized = (mime_type or "").split(";", 1)[0].strip().lower()
    explicit = {
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
        "audio/mpeg": ".mp3",
        "audio/mp3": ".mp3",
        "audio/wav": ".wav",
        "audio/x-wav": ".wav",
        "audio/ogg": ".ogg",
        "audio/flac": ".flac",
        "audio/mp4": ".m4a",
    }
    if normalized in explicit:
        return explicit[normalized]
    return mimetypes.guess_extension(normalized) or ".bin"


def cache_base64_payload(
    data: object,
    mime_type: object,
    *,
    max_bytes: int = DEFAULT_MAX_BINARY_BYTES,
) -> CachedMCPPayload | None:
    """Validate, size-bound, and persist one base64-encoded payload.

    Content-addressed filenames prevent identical repeated tool results from
    growing the cache.  Files are created with user-only permissions.  Invalid
    or oversized inputs return ``None`` and never partially write a file.
    """
    normalized = str(mime_type or "").split(";", 1)[0].strip().lower()
    if not isinstance(data, (str, bytes)) or not normalized or max_bytes < 1:
        return None

    try:
        encoded = data.encode("ascii", errors="strict") if isinstance(data, str) else data
    except UnicodeError as exc:
        logger.debug("MCP binary decode failed (%s): %s", normalized, exc)
        return None
    # Base64 expands input by roughly 4/3. Reject obviously oversized values
    # before decoding so an untrusted server cannot force a large allocation.
    max_encoded_bytes = ((max_bytes + 2) // 3) * 4 + 4
    if len(encoded) > max_encoded_bytes:
        logger.warning("MCP binary payload exceeds the configured %d-byte limit", max_bytes)
        return None

    try:
        raw = base64.b64decode(encoded, validate=True)
    except (TypeError, ValueError, UnicodeError) as exc:
        logger.debug("MCP binary decode failed (%s): %s", normalized, exc)
        return None
    if not raw or len(raw) > max_bytes:
        if raw:
            logger.warning("MCP binary payload exceeds the configured %d-byte limit", max_bytes)
        return None

    digest = hashlib.sha256(raw).hexdigest()
    out = _media_dir() / f"mcp-{digest}{_extension_for(normalized)}"
    try:
        fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        # Never trust a pre-existing symlink or a path whose bytes do not
        # match the content-addressed name.
        try:
            if out.is_symlink() or not out.is_file() or out.read_bytes() != raw:
                logger.warning("MCP media cache collision rejected at %s", out)
                return None
            out.chmod(0o600)
        except OSError as exc:
            logger.debug("MCP media cache validation failed: %s", exc)
            return None
        return CachedMCPPayload(path=out, mime_type=normalized, size=len(raw))
    except OSError as exc:
        logger.debug("MCP media cache create failed: %s", exc)
        return None

    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
    except OSError as exc:
        logger.debug("MCP media cache write failed: %s", exc)
        try:
            out.unlink(missing_ok=True)
        except OSError:
            pass
        return None
    return CachedMCPPayload(path=out, mime_type=normalized, size=len(raw))


def cache_media_block(
    block: object,
    *,
    max_bytes: int = DEFAULT_MAX_BINARY_BYTES,
) -> CachedMCPPayload | None:
    """Cache an MCP image or audio content block, if usable."""
    data = getattr(block, "data", None)
    mime_type = getattr(block, "mime_type", None)
    if mime_type is None:
        mime_type = getattr(block, "mimeType", None)
    normalized = str(mime_type or "").split(";", 1)[0].strip().lower()
    if not (normalized.startswith("image/") or normalized.startswith("audio/")):
        return None
    return cache_base64_payload(data, normalized, max_bytes=max_bytes)


def cache_image_block(
    block: object,
    *,
    max_bytes: int = DEFAULT_MAX_BINARY_BYTES,
) -> str | None:
    """Decode an MCP ImageContent block to disk; return its ``MEDIA:`` token.

    Returns ``None`` when *block* is not a usable image (no data, wrong
    MIME, decode failure). Never raises.
    """
    mime_type = getattr(block, "mime_type", None)
    if mime_type is None:
        mime_type = getattr(block, "mimeType", None)
    normalized = str(mime_type or "").split(";", 1)[0].strip().lower()
    if not normalized.startswith("image/"):
        return None
    cached = cache_media_block(block, max_bytes=max_bytes)
    return cached.media_tag if cached else None
