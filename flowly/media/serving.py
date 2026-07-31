"""Serving generated media off this machine's disk — the shared rules.

Two transports hand media to clients: the gateway's HTTP streaming route and
the relay bridge (a client asks the relay, the relay asks this bot over its
agent socket). Both must answer the same questions the same way — is this id
safe, does the file exist, may this type be served — so the rules live here
once instead of drifting apart in two places.

The relay side works in WINDOWS rather than streams on purpose. A window
request ("give me bytes 4194304..8388607 of vid-abc.mp4") is stateless: the bot
reads, replies with one frame, and forgets. No open file handles across
messages, no per-connection stream state to leak when a socket drops, and
backpressure falls out for free — the relay simply doesn't ask for the next
window until the previous one has been written to the client.
"""

from __future__ import annotations

import mimetypes
from dataclasses import dataclass
from pathlib import Path

# Mirror of the gateway's streaming ceiling: bounds what one file may be, not
# what one request may read.
MAX_SERVE_BYTES = 2 * 1024 * 1024 * 1024

# What may be served at all. Generated media only — this is not a file server.
SERVABLE_MIME_PREFIXES = ("image/", "video/", "audio/")

# One window must fit in one relay frame after base64 (+33%) and JSON overhead.
# The relay caps agent frames at 10 MB; 4 MB raw → ~5.5 MB encoded. One frame
# per window is what keeps the protocol stateless.
MAX_WINDOW_BYTES = 4 * 1024 * 1024


def resolve_media_id(
    name: str, media_dir: Path | None = None
) -> tuple[Path | None, str, int]:
    """Resolve a media id (a bare basename) to a file inside the media dir.

    Returns ``(path, "", 0)`` on success or ``(None, error, http_status)``.
    ``media_dir`` lets a transport pin the directory it already knows (and lets
    tests isolate one); the default is this profile's media directory. The
    containment rules, in one place for every transport:

      * the id is a BASENAME — any separator, ``..`` or leading dot is rejected
        before it touches the filesystem;
      * the RESOLVED target must still sit inside the RESOLVED media dir, which
        is what stops a symlink planted in the media dir from reading elsewhere.
    """
    if not name or "/" in name or "\\" in name or ".." in name or name.startswith("."):
        return None, "invalid id", 400
    if media_dir is None:
        from flowly.profile import get_flowly_home

        media_dir = get_flowly_home() / "media"
    media_dir = media_dir.resolve()
    try:
        target = (media_dir / name).resolve()
    except (OSError, RuntimeError):
        return None, "invalid id", 400
    if target != media_dir and media_dir not in target.parents:
        return None, "forbidden", 403
    if not target.is_file():
        return None, "not found", 404
    return target, "", 0


@dataclass(frozen=True, slots=True)
class MediaWindow:
    """One answered window request."""

    ok: bool
    size: int = 0
    mime_type: str = ""
    data: bytes = b""
    eof: bool = False
    error: str = ""


def read_media_window(
    media_id: str, offset: int = 0, length: int = 0, media_dir: Path | None = None
) -> MediaWindow:
    """Read one bounded window of a media file, or explain why not.

    ``length == 0`` is a metadata probe: size and mime only, no bytes — what a
    HEAD request or a Range validation needs. Errors come back as values, not
    exceptions, because every caller is about to serialize the answer onto a
    wire anyway.
    """
    target, error, _status = resolve_media_id(media_id, media_dir)
    if target is None:
        return MediaWindow(ok=False, error="invalid_id" if error == "invalid id" else (
            "forbidden" if error == "forbidden" else "not_found"
        ))

    mime, _ = mimetypes.guess_type(str(target))
    mime = mime or ""
    if not mime.startswith(SERVABLE_MIME_PREFIXES):
        return MediaWindow(ok=False, error="unsupported_type")

    try:
        size = target.stat().st_size
    except OSError:
        return MediaWindow(ok=False, error="not_found")
    if size > MAX_SERVE_BYTES:
        return MediaWindow(ok=False, error="too_large")

    if length <= 0:
        return MediaWindow(ok=True, size=size, mime_type=mime, eof=offset >= size)

    offset = max(0, int(offset))
    if offset >= size:
        # Past the end is an answerable question, not a failure: the caller
        # learns the real size and can 416 with a correct Content-Range.
        return MediaWindow(ok=True, size=size, mime_type=mime, eof=True)

    window = min(int(length), MAX_WINDOW_BYTES, size - offset)
    try:
        with target.open("rb") as handle:
            handle.seek(offset)
            data = handle.read(window)
    except OSError:
        return MediaWindow(ok=False, error="not_found")

    return MediaWindow(
        ok=True,
        size=size,
        mime_type=mime,
        data=data,
        eof=offset + len(data) >= size,
    )
