"""Cache MCP image content blocks to disk and surface them as MEDIA tags.

MCP tool results may contain ``ImageContent`` blocks (Playwright
screenshots, chart renderers, etc.). The agent's messaging adapters
render local files referenced by a ``MEDIA:<absolute-path>`` token in
the tool result text, so we decode the base64 payload, write it under
``$FLOWLY_HOME/media/mcp/`` and return that token.

Errors are swallowed (logged at debug): a single bad image block must
not sink an otherwise-useful tool result. The caller falls through to
whatever text blocks parsed.
"""

from __future__ import annotations

import base64
import logging
import mimetypes
import secrets
from pathlib import Path


logger = logging.getLogger(__name__)


def _media_dir() -> Path:
    from flowly.profile import get_flowly_home
    path = get_flowly_home() / "media" / "mcp"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _extension_for(mime_type: str) -> str:
    normalized = (mime_type or "").split(";", 1)[0].strip().lower()
    if normalized in {"image/jpeg", "image/jpg"}:
        return ".jpg"
    return mimetypes.guess_extension(normalized) or ".png"


def cache_image_block(block: object) -> str | None:
    """Decode an MCP ImageContent block to disk; return its ``MEDIA:`` token.

    Returns ``None`` when *block* is not a usable image (no data, wrong
    MIME, decode failure). Never raises.
    """
    data = getattr(block, "data", None)
    mime_type = getattr(block, "mimeType", None)
    normalized = str(mime_type or "").split(";", 1)[0].strip().lower()
    if data is None or not normalized.startswith("image/"):
        return None

    try:
        raw = base64.b64decode(data)
    except (TypeError, ValueError) as exc:
        logger.debug("MCP image decode failed (%s): %s", normalized, exc)
        return None
    if not raw:
        return None

    try:
        out = _media_dir() / f"mcp-{secrets.token_hex(8)}{_extension_for(normalized)}"
        out.write_bytes(raw)
    except OSError as exc:
        logger.debug("MCP image cache write failed: %s", exc)
        return None

    return f"MEDIA:{out}"
