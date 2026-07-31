"""Media upload helpers for the local TUI composer.

The upload call itself now lives in :mod:`flowly.media.hosted`: generated video
needs the identical request on its way OUT to a relay client, and two copies of
an upload contract is one copy too many. What stays here is the composer's own
policy — which attachments need uploading at all, and what to tell a signed-out
user who just dropped a large clip into the prompt.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from flowly.media import hosted
from flowly.media.hosted import MAX_UPLOAD_BYTES, UPLOAD_TIMEOUT
from flowly.tui.attachments import build_attachment, is_video_path

if TYPE_CHECKING:
    from flowly.account.auth import Account


MAX_SIGNED_OUT_INLINE_VIDEO_BYTES = 10 * 1024 * 1024

__all__ = [
    "MAX_UPLOAD_BYTES",
    "MAX_SIGNED_OUT_INLINE_VIDEO_BYTES",
    "UPLOAD_TIMEOUT",
    "AttachmentPreparationError",
    "MediaUploadAuthRequiredError",
    "MediaUploadTooLargeError",
    "MediaUploadFailedError",
    "MediaUploadAuthRequired",
    "MediaUploadTooLarge",
    "MediaUploadFailed",
    "upload_media",
    "prepare_media_attachments",
]


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


_size_mb = hosted.size_mb
_mime_for = hosted.guess_mime
_upload_ready = hosted.upload_ready


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
    client: Any | None = None,
) -> dict[str, Any]:
    """Upload a media file to Flowly and return a chat attachment payload.

    Thin wrapper over :func:`flowly.media.hosted.upload_media` that re-raises
    in the composer's own exception vocabulary, which the TUI catches to show
    an inline hint rather than a traceback.
    """
    try:
        return await hosted.upload_media(
            path, account=account, conversation_id=conversation_id, client=client
        )
    except hosted.HostedUploadTooLargeError as exc:
        raise MediaUploadTooLargeError(str(exc)) from exc
    except hosted.HostedUploadAuthError as exc:
        raise MediaUploadAuthRequiredError(str(exc)) from exc
    except hosted.HostedUploadError as exc:
        raise MediaUploadFailedError(f"video upload failed: {exc}") from exc


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
