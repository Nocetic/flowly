"""Files an app sent to a home that keeps no chats on the Flowly server.

The server keeps nothing of such a home's chats (server PROTOCOL A11,
chat-storage-choice.md §9.6), and so none of their files either. A file an app
uploads still reaches storage first, so a message can wait while this computer
is away; the server marks it ``keep`` when it hands the message here, and this
computer takes the file into its media dir before the turn starts. From then
on the file is this computer's: the agent reads it from disk (in this turn and
in every later one that looks back at it), and the apps fetch it by its media
id through the server's media bridge, as they fetch the agent's own files.

``chat.send`` answers with each kept file's media id (``files``, one per
attachment, ``None`` for one not marked). That answer lets the server delete
its copy. A marked file that cannot be taken here refuses the message
(:class:`KeptFileError`): it is never run on the server's copy, which would
be gone once the server deletes it; the server delivers it again.
"""

from __future__ import annotations

import asyncio
import base64
import mimetypes
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from loguru import logger

from flowly.media.serving import MAX_SHARE_BYTES

KEEP_KEY = "keep"
# A slow home uplink still moves a large file; a dead link ends the turn's wait.
DOWNLOAD_TIMEOUT_SECONDS = 30.0
DOWNLOAD_CONNECT_SECONDS = 10.0

Fetch = Callable[[str, Path, int], Awaitable[None]]


class KeptFileError(Exception):
    """A file that could not be taken here: the message is refused, and comes again."""


async def download(url: str, destination: Path, limit: int = MAX_SHARE_BYTES, *, transport=None) -> None:
    """Write the bytes at ``url`` (HTTPS only, no redirect) to ``destination``, at most ``limit`` of them."""
    import httpx

    if not isinstance(url, str) or not url.startswith("https://"):
        raise KeptFileError("only an https file is taken")
    timeout = httpx.Timeout(DOWNLOAD_TIMEOUT_SECONDS, connect=DOWNLOAD_CONNECT_SECONDS)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False, transport=transport) as client:
        async with client.stream("GET", url) as response:
            if response.status_code != 200:
                raise KeptFileError(f"the file answered {response.status_code}")
            written = 0
            with destination.open("wb") as handle:
                async for chunk in response.aiter_bytes():
                    written += len(chunk)
                    if written > limit:
                        raise KeptFileError("the file is larger than a chat file may be")
                    handle.write(chunk)


def _extension(attachment: dict[str, Any]) -> str:
    name = attachment.get("fileName")
    suffix = Path(name).suffix if isinstance(name, str) else ""
    if suffix and len(suffix) <= 16 and suffix[1:].isalnum():
        return suffix.lower()
    mime = attachment.get("mimeType")
    return (mimetypes.guess_extension(mime) or "") if isinstance(mime, str) else ""


def _decoded(content: str) -> bytes:
    if content.startswith("data:") and "," in content:
        content = content.split(",", 1)[1]
    data = base64.b64decode(content, validate=True)
    if len(data) > MAX_SHARE_BYTES:
        raise KeptFileError("the file is larger than a chat file may be")
    return data


async def _take(attachment: dict[str, Any], media_dir: Path, access, fetch: Fetch) -> Path:
    from flowly.media.authority import publish_media_bytes, publish_media_file

    destination = media_dir / f"{uuid.uuid4().hex}{_extension(attachment)}"
    content = attachment.get("content")
    if isinstance(content, str) and content:
        data = _decoded(content)
        return await asyncio.to_thread(publish_media_bytes, data, destination, access=access)
    url = attachment.get("cdnUrl")
    if not isinstance(url, str) or not url:
        raise KeptFileError("the file has neither bytes nor a place")
    media_dir.mkdir(parents=True, exist_ok=True)
    temporary = media_dir / f".keep-{uuid.uuid4().hex}.part"
    try:
        await fetch(url, temporary, MAX_SHARE_BYTES)
        return await asyncio.to_thread(publish_media_file, temporary, destination, access)
    finally:
        temporary.unlink(missing_ok=True)


def _media_id(path: Path, media_dir: Path) -> str | None:
    """The id the server's media bridge reads it by: checked as that read is (``media.fetch``)."""
    from flowly.live_voice.authority import HOST_OWNER, request_owner_scope
    from flowly.media.serving import share_in_media_dir

    with request_owner_scope(HOST_OWNER):
        return share_in_media_dir(path, media_dir)


async def keep_files(attachments: list[Any], media_dir: Path, *, fetch: Fetch | None = None
                     ) -> tuple[list[Any], list[str | None] | None]:
    """The attachments with each ``keep`` file taken into ``media_dir``, and their media ids.

    Called within the turn's media access (as ``_save_attachments`` is). The
    ids are ``None`` when no attachment asked to be kept: then nothing was.
    Raises :class:`KeptFileError` when a marked file could not be taken, after
    removing the ones taken before it.
    """
    if not any(isinstance(att, dict) and att.get(KEEP_KEY) is True for att in attachments):
        return attachments, None
    from flowly.media.authority import capture_media_access

    access = capture_media_access()
    fetch = fetch or download
    out: list[Any] = []
    ids: list[str | None] = []
    taken: list[Path] = []
    for att in attachments:
        if not isinstance(att, dict) or KEEP_KEY not in att:
            out.append(att)
            ids.append(None)
            continue
        clean = {key: value for key, value in att.items() if key != KEEP_KEY}
        if att.get(KEEP_KEY) is not True:
            out.append(clean)
            ids.append(None)
            continue
        try:
            path = await _take(clean, media_dir, access, fetch)
            media_id = await asyncio.to_thread(_media_id, path, media_dir)
            if media_id is None:
                path.unlink(missing_ok=True)
                raise KeptFileError("the file is not one the apps could read here")
        except Exception as exc:  # noqa: BLE001 - any failure refuses the message, never half of it
            for done in taken:
                done.unlink(missing_ok=True)
            logger.warning(f"[kept_files] {clean.get('fileName') or 'a file'} was not taken here: {exc}")
            raise KeptFileError(str(exc) or "the file could not be taken here") from exc
        taken.append(path)
        out.append({"fileName": clean.get("fileName") or path.name,
                    "mimeType": clean.get("mimeType") or "", "filePath": str(path)})
        ids.append(media_id)
    return out, ids
