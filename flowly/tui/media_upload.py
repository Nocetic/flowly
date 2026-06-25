"""Media upload helpers for the local TUI composer."""

from __future__ import annotations

import mimetypes
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable

import httpx

from flowly.account.auth import FLOWLY_API_BASE
from flowly.tui.attachments import build_attachment, is_video_path

if TYPE_CHECKING:
    from flowly.account.auth import Account


MAX_UPLOAD_BYTES = 50 * 1024 * 1024
MAX_SIGNED_OUT_INLINE_VIDEO_BYTES = 10 * 1024 * 1024
UPLOAD_TIMEOUT = httpx.Timeout(120.0, connect=10.0, read=120.0, write=120.0)


class AttachmentPreparationError(Exception):
    """Raised when attachments cannot be safely sent."""


class MediaUploadAuthRequiredError(AttachmentPreparationError):
    """Raised when a large video needs authenticated upload first."""


class MediaUploadTooLargeError(AttachmentPreparationError):
    """Raised when a video exceeds the hosted upload cap."""


class MediaUploadFailedError(AttachmentPreparationError):
    """Raised when the hosted upload endpoint rejects or fails the upload."""


MediaUploadAuthRequired = MediaUploadAuthRequiredError
MediaUploadTooLarge = MediaUploadTooLargeError
MediaUploadFailed = MediaUploadFailedError


def _size_mb(size: int) -> float:
    return size / (1024 * 1024)


def _mime_for(path: Path) -> str:
    mime, _ = mimetypes.guess_type(str(path))
    return mime or "application/octet-stream"


def _upload_ready(account: "Account | None") -> bool:
    return bool(
        account
        and getattr(account, "id_token", "")
        and getattr(account, "server_id", "")
    )


def _auth_required_message(path: Path, account: "Account | None") -> str:
    limit_mb = MAX_SIGNED_OUT_INLINE_VIDEO_BYTES // (1024 * 1024)
    size = path.stat().st_size
    if account and not getattr(account, "server_id", ""):
        return (
            f"video upload needs this machine to be registered with your Flowly "
            f"account. `{path.name}` is {_size_mb(size):.1f} MB; local video "
            f"inline is limited to {limit_mb} MB. Run `flowly login --repair` "
            "and try again."
        )
    return (
        f"video upload requires Flowly sign-in for files over {limit_mb} MB. "
        f"`{path.name}` is {_size_mb(size):.1f} MB. Run `/login` to upload "
        "securely, or trim/compress the video."
    )


async def upload_media(
    path: Path,
    *,
    account: "Account",
    conversation_id: str,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Upload a media file to Flowly and return a chat attachment payload."""

    size = path.stat().st_size
    if size <= 0:
        raise MediaUploadFailedError(f"`{path.name}` is empty.")
    if size > MAX_UPLOAD_BYTES:
        raise MediaUploadTooLargeError(
            f"`{path.name}` is {_size_mb(size):.1f} MB. Max upload size is "
            f"{MAX_UPLOAD_BYTES // (1024 * 1024)} MB."
        )

    mime = _mime_for(path)
    url = f"{FLOWLY_API_BASE.rstrip('/')}/api/v1/uploads/media"
    headers = {"Authorization": f"Bearer {account.id_token}"}
    data = {
        "serverId": account.server_id,
        "conversationId": conversation_id,
    }

    async def _send(c: httpx.AsyncClient) -> dict[str, Any]:
        with path.open("rb") as fh:
            response = await c.post(
                url,
                headers=headers,
                data=data,
                files={"file": (path.name, fh, mime)},
            )
        if response.status_code >= 400:
            detail = response.text[:200]
            try:
                body = response.json()
                if isinstance(body, dict) and body.get("error"):
                    detail = str(body["error"])
            except ValueError:
                pass
            if response.status_code == 413:
                raise MediaUploadTooLargeError(detail)
            raise MediaUploadFailedError(f"video upload failed ({response.status_code}): {detail}")

        body = response.json()
        cdn_url = body.get("cdnUrl") if isinstance(body, dict) else None
        if not isinstance(cdn_url, str) or not cdn_url.startswith(("http://", "https://")):
            raise MediaUploadFailedError("video upload response did not include a valid cdnUrl.")
        return {
            "cdnUrl": cdn_url,
            "fileName": str(body.get("fileName") or path.name),
            "mimeType": str(body.get("mimeType") or mime),
            "size": int(body.get("size") or size),
        }

    if client is not None:
        return await _send(client)
    async with httpx.AsyncClient(timeout=UPLOAD_TIMEOUT) as owned_client:
        return await _send(owned_client)


async def prepare_media_attachments(
    paths: list[Path],
    *,
    account: "Account | None",
    conversation_id: str,
    on_upload_start: Callable[[Path], Any] | None = None,
    upload: Callable[[Path, "Account", str], Awaitable[dict[str, Any]]] | None = None,
) -> list[dict[str, Any]]:
    """Build chat attachment payloads, uploading signed-in videos first."""

    prepared: list[dict[str, Any]] = []
    for path in paths:
        if not is_video_path(path):
            prepared.append(build_attachment(path))
            continue

        size = path.stat().st_size
        if _upload_ready(account):
            if on_upload_start:
                on_upload_start(path)
            if upload is not None:
                prepared.append(await upload(path, account, conversation_id))
            else:
                prepared.append(
                    await upload_media(path, account=account, conversation_id=conversation_id)
                )
            continue

        if size > MAX_SIGNED_OUT_INLINE_VIDEO_BYTES:
            raise MediaUploadAuthRequiredError(_auth_required_message(path, account))
        prepared.append(build_attachment(path))

    return prepared
