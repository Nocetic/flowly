"""Hosted media upload — the shared client for putting a file behind a CDN URL.

The relay carries chat frames, not payloads. An image can cheat (compressed to
a few hundred KB it rides as base64 inside the message), but a video cannot:
even a short clip is tens of megabytes, and base64 inflates it by a third.

So video takes the same route a user's own attachment already takes — POST to
Flowly's hosted upload endpoint, get back a ``cdnUrl``, and put only that URL on
the wire. This module is that client. It was previously private to the TUI
composer; generated media needs the identical call, and two copies of an upload
contract is one copy too many.

Requires a signed-in, registered machine: the endpoint authenticates with the
account's id token and scopes the object to ``serverId``. That is not a new
constraint for the relay path — a relay conversation only exists for a machine
that is already registered — but it does mean a signed-out user gets a clear
error instead of a silently missing video.
"""

from __future__ import annotations

import mimetypes
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from flowly.account.auth import FLOWLY_API_BASE

if TYPE_CHECKING:
    from flowly.account.auth import Account


# Ceiling enforced by the hosted endpoint. Checked client-side too so an
# oversized file fails fast with a useful message instead of a bare 413.
MAX_UPLOAD_BYTES = 50 * 1024 * 1024

UPLOAD_TIMEOUT = httpx.Timeout(120.0, connect=10.0, read=120.0, write=120.0)


class HostedUploadError(Exception):
    """A hosted upload could not be completed."""


class HostedUploadTooLargeError(HostedUploadError):
    """The file exceeds the hosted upload cap."""


class HostedUploadAuthError(HostedUploadError):
    """No signed-in, registered account to upload on behalf of."""


def upload_ready(account: "Account | None") -> bool:
    """True when *account* can authenticate a hosted upload."""
    return bool(
        account
        and getattr(account, "id_token", "")
        and getattr(account, "server_id", "")
    )


def guess_mime(path: Path) -> str:
    mime, _ = mimetypes.guess_type(str(path))
    return mime or "application/octet-stream"


def size_mb(size: int) -> float:
    return size / (1024 * 1024)


async def upload_media(
    path: Path,
    *,
    account: "Account",
    conversation_id: str,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Upload *path* and return ``{cdnUrl, s3Key?, fileName, mimeType, size}``.

    Raises :class:`HostedUploadError` (or a subclass) on any failure — never
    returns a half-formed attachment, because a client cannot tell the
    difference between "no video" and "video whose URL is missing".
    """
    if not upload_ready(account):
        raise HostedUploadAuthError(
            "hosted upload needs a signed-in Flowly account on a registered machine."
        )

    size = path.stat().st_size
    if size <= 0:
        raise HostedUploadError(f"`{path.name}` is empty.")
    if size > MAX_UPLOAD_BYTES:
        raise HostedUploadTooLargeError(
            f"`{path.name}` is {size_mb(size):.1f} MB. Max upload size is "
            f"{MAX_UPLOAD_BYTES // (1024 * 1024)} MB."
        )

    mime = guess_mime(path)
    url = f"{FLOWLY_API_BASE.rstrip('/')}/api/v1/uploads/media"
    headers = {"Authorization": f"Bearer {account.id_token}"}
    data = {"serverId": account.server_id, "conversationId": conversation_id}

    async def _send(c: httpx.AsyncClient) -> dict[str, Any]:
        with path.open("rb") as fh:
            response = await c.post(
                url, headers=headers, data=data, files={"file": (path.name, fh, mime)}
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
                raise HostedUploadTooLargeError(detail)
            raise HostedUploadError(f"upload failed ({response.status_code}): {detail}")

        body = response.json()
        cdn_url = body.get("cdnUrl") if isinstance(body, dict) else None
        if not isinstance(cdn_url, str) or not cdn_url.startswith(("http://", "https://")):
            raise HostedUploadError("upload response did not include a valid cdnUrl.")
        result: dict[str, Any] = {
            "cdnUrl": cdn_url,
            "fileName": str(body.get("fileName") or path.name),
            "mimeType": str(body.get("mimeType") or mime),
            "size": int(body.get("size") or size),
        }
        s3_key = body.get("s3Key")
        if isinstance(s3_key, str) and s3_key:
            result["s3Key"] = s3_key
        return result

    if client is not None:
        return await _send(client)
    async with httpx.AsyncClient(timeout=UPLOAD_TIMEOUT) as owned:
        return await _send(owned)
