"""Media assets — one durable descriptor per file a tool produced.

Before this module the only thing that travelled with generated media was its
local path (``_reply_media: [path]``). That is enough for an image — a client
can guess the rest — but not for video: a player needs the duration to draw a
scrubber, the dimensions to reserve the right aspect box, and a poster frame to
show before the first byte of the clip arrives.

:class:`MediaAsset` is the internal record. :func:`attachment_v2` is the wire
shape sent to Desktop/iOS. The two are deliberately separate: the asset knows
local paths and provider request ids, and **neither of those may ever reach a
client**, so the conversion is the place that decides what is safe to publish.

Attachment V2 is purely additive over the V1 shape (``fileName`` / ``mimeType``
/ ``thumbnail`` / ``mediaId`` / ``cdnUrl``). An old client ignores ``version``
and the new fields and keeps rendering images exactly as before.
"""

from __future__ import annotations

import mimetypes
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

ATTACHMENT_VERSION = 2

# Metadata key carrying asset descriptors alongside ``OutboundMessage.media``.
# The path list stays the canonical delivery contract (every channel adapter
# reads it), so assets ride beside it rather than replacing it.
ASSETS_META_KEY = "media_assets"

KIND_IMAGE = "image"
KIND_VIDEO = "video"
KIND_AUDIO = "audio"
KIND_FILE = "file"

STATUS_READY = "ready"
STATUS_PENDING = "pending"
STATUS_FAILED = "failed"
# The descriptor outlived the bytes: retention pruned the file, but chat history
# still references it. Clients show a "no longer available" placeholder instead
# of a broken bubble.
STATUS_EXPIRED = "expired"


def new_asset_id() -> str:
    return f"media_{uuid.uuid4().hex[:16]}"


def kind_for_mime(mime_type: str) -> str:
    mime = (mime_type or "").lower()
    if mime.startswith("image/"):
        return KIND_IMAGE
    if mime.startswith("video/"):
        return KIND_VIDEO
    if mime.startswith("audio/"):
        return KIND_AUDIO
    return KIND_FILE


def guess_mime(path: Path | str) -> str:
    mime, _ = mimetypes.guess_type(str(path))
    return mime or "application/octet-stream"


@dataclass(frozen=True, slots=True)
class MediaAsset:
    """A file produced by a generation tool, plus everything a player needs.

    ``path`` and ``poster_path`` are LOCAL paths and never leave the server.
    ``source_url`` is the provider's (often signed) download URL, kept for audit
    only — it is likewise never published.

    ``prompt`` is what the user actually asked for. It is not part of the chat
    wire (see :func:`attachment_v2`) — the bubble already sits underneath the
    message that requested it. It exists so the media library can be *searched*
    by intent months later, which is the only way to find a picture whose
    filename is ``img-9f3a71c2b804.png``.
    """

    path: str
    kind: str = KIND_FILE
    file_name: str = ""
    mime_type: str = ""
    size: int = 0
    width: int | None = None
    height: int | None = None
    duration_ms: int | None = None
    poster_path: str | None = None
    source_url: str | None = None
    provider: str = ""
    model: str = ""
    request_id: str = ""
    prompt: str = ""
    id: str = ""
    status: str = STATUS_READY

    # -- serialization (session history / job files) ----------------------

    def to_dict(self) -> dict[str, Any]:
        """snake_case dict for on-disk persistence (sessions, job files)."""
        out: dict[str, Any] = {
            "id": self.id,
            "path": self.path,
            "kind": self.kind,
            "file_name": self.file_name,
            "mime_type": self.mime_type,
            "size": self.size,
            "status": self.status,
        }
        for key in ("width", "height", "duration_ms", "poster_path", "source_url"):
            value = getattr(self, key)
            if value is not None:
                out[key] = value
        for key in ("provider", "model", "request_id", "prompt"):
            value = getattr(self, key)
            if value:
                out[key] = value
        return out

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MediaAsset | None":
        """Rebuild an asset from :meth:`to_dict`. ``None`` when unusable.

        Tolerant on purpose: a session written by an older build, or a
        hand-edited job file, must not crash history rendering.
        """
        if not isinstance(data, dict):
            return None
        path = data.get("path")
        if not isinstance(path, str) or not path:
            return None

        def _int_or_none(key: str) -> int | None:
            value = data.get(key)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return None
            return int(value)

        def _str_or_none(key: str) -> str | None:
            value = data.get(key)
            return value if isinstance(value, str) and value else None

        mime = data.get("mime_type")
        mime = mime if isinstance(mime, str) and mime else guess_mime(path)
        kind = data.get("kind")
        size = data.get("size")
        return cls(
            path=path,
            kind=kind if isinstance(kind, str) and kind else kind_for_mime(mime),
            file_name=str(data.get("file_name") or Path(path).name),
            mime_type=mime,
            size=int(size) if isinstance(size, int) and not isinstance(size, bool) else 0,
            width=_int_or_none("width"),
            height=_int_or_none("height"),
            duration_ms=_int_or_none("duration_ms"),
            poster_path=_str_or_none("poster_path"),
            source_url=_str_or_none("source_url"),
            provider=str(data.get("provider") or ""),
            model=str(data.get("model") or ""),
            request_id=str(data.get("request_id") or ""),
            prompt=str(data.get("prompt") or ""),
            id=str(data.get("id") or "") or new_asset_id(),
            status=str(data.get("status") or STATUS_READY),
        )


def describe(
    path: Path | str,
    *,
    provider: str = "",
    model: str = "",
    request_id: str = "",
    prompt: str = "",
    source_url: str | None = None,
    poster_path: str | None = None,
    asset_id: str | None = None,
    status: str = STATUS_READY,
    probe_media: bool = True,
) -> MediaAsset:
    """Build an asset for an existing local file, probing what it can.

    A file that does not exist yields ``status=expired`` with zero size rather
    than raising: history may legitimately reference media that retention has
    already reclaimed, and that must render as "gone", not as a crash.
    """
    p = Path(path)
    mime = guess_mime(p)
    kind = kind_for_mime(mime)
    asset = MediaAsset(
        path=str(p),
        kind=kind,
        file_name=p.name,
        mime_type=mime,
        poster_path=poster_path,
        source_url=source_url,
        provider=provider,
        model=model,
        request_id=request_id,
        prompt=prompt,
        id=asset_id or new_asset_id(),
        status=status,
    )

    try:
        stat = p.stat()
    except OSError:
        return replace(asset, status=STATUS_EXPIRED)

    asset = replace(asset, size=int(stat.st_size))
    if not probe_media:
        return asset

    from flowly.media.probe import probe as _probe

    measured = _probe(p, mime)
    return replace(
        asset,
        width=measured.width,
        height=measured.height,
        duration_ms=measured.duration_ms,
    )


def attachment_v2(
    asset: MediaAsset,
    *,
    thumbnail: str | None = None,
    media_id: str | None = None,
    cdn_url: str | None = None,
    status: str | None = None,
) -> dict[str, Any]:
    """Render an asset as the Attachment V2 wire object.

    Only ONE of ``media_id`` / ``cdn_url`` is normally set: a direct gateway
    serves bytes itself (``mediaId``), the relay path hosts them (``cdnUrl``).
    Absent fields are omitted entirely so the payload stays small and an old
    client sees exactly the V1 keys it already understands.

    Local paths, provider request ids, signed source URLs and the originating
    ``prompt`` are deliberately NOT included — this function is the publication
    boundary. (The media library publishes the prompt on its own list surface,
    where it is the search key; a chat bubble does not need it because the
    request is the message directly above it.)
    """
    out: dict[str, Any] = {
        "version": ATTACHMENT_VERSION,
        "id": asset.id or new_asset_id(),
        "kind": asset.kind or kind_for_mime(asset.mime_type),
        "fileName": asset.file_name or Path(asset.path).name,
        "mimeType": asset.mime_type or guess_mime(asset.path),
        "status": status or asset.status or STATUS_READY,
    }
    if asset.size:
        out["size"] = asset.size
    if asset.width is not None:
        out["width"] = asset.width
    if asset.height is not None:
        out["height"] = asset.height
    if asset.duration_ms is not None:
        out["durationMs"] = asset.duration_ms
    if thumbnail:
        out["thumbnail"] = thumbnail
    if cdn_url:
        out["cdnUrl"] = cdn_url
    if media_id:
        out["mediaId"] = media_id
    return out


def assets_to_meta(assets: list[MediaAsset]) -> list[dict[str, Any]]:
    return [a.to_dict() for a in assets]


def assets_from_meta(raw: Any) -> list[MediaAsset]:
    """Parse persisted asset dicts, skipping anything unusable."""
    if not isinstance(raw, list):
        return []
    out: list[MediaAsset] = []
    for entry in raw:
        asset = MediaAsset.from_dict(entry) if isinstance(entry, dict) else None
        if asset is not None:
            out.append(asset)
    return out


def index_by_path(assets: list[MediaAsset]) -> dict[str, MediaAsset]:
    """Map ``path -> asset`` so a path-keyed delivery list can be enriched."""
    return {a.path: a for a in assets if a.path}
