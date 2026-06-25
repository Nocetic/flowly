"""BlueBubbles outbound transport for the iMessage channel.

When the user runs a BlueBubbles server (a separate signed macOS app
that holds the Automation + Full Disk Access permissions), Flowly sends
iMessages by POSTing to its REST API instead of driving Messages.app
itself. This sidesteps the TCC wall a background/gateway process hits
when calling AppleScript directly (``-10004`` privilege violation): the
BlueBubbles app is the responsible, permission-holding process, and
Flowly just talks HTTP to ``127.0.0.1``.

Inbound still flows through the direct chat.db reader — only sending is
delegated here, so the user keeps the same allowlist/session behaviour.

API reference: https://bluebubbles.app/ (Server → API).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from urllib.parse import quote

import httpx
from loguru import logger

# BlueBubbles' AppleScript send method works because the BB server app
# holds the Automation grant. "private-api" unlocks tapbacks/typing but
# needs the helper install; "apple-script" is the no-extra-setup default.
_SEND_METHOD = "apple-script"
# AppleScript-backed sends drive Messages.app synchronously on the server
# and can take 20-40s on the first send / cold Messages — be generous on
# read, short on connect (a wrong URL should fail fast).
_TIMEOUT = httpx.Timeout(60.0, connect=5.0)
_PING_TIMEOUT = httpx.Timeout(5.0)


class BlueBubblesError(Exception):
    """A BlueBubbles request failed (network or API-level)."""


def chat_guid_for(target: str) -> str:
    """BlueBubbles chat GUID for a send target.

    A raw GUID (already contains ``;``) passes through. A bare DM handle
    becomes the canonical ``iMessage;-;<handle>`` GUID that BlueBubbles
    addresses 1:1 conversations by.
    """
    if ";" in target:
        return target
    return f"iMessage;-;{target}"


def _next_temp_guid(seed: str) -> str:
    """A per-send id BlueBubbles uses to dedupe. Stable-but-unique enough
    without RNG (avoids importing uuid in hot paths / restricted envs)."""
    import time

    return f"flowly-{abs(hash(seed)) & 0xFFFFFFFF:x}-{time.time_ns()}"


async def _post(
    server_url: str, password: str, path: str, payload: dict
) -> dict:
    url = f"{server_url.rstrip('/')}{path}?password={quote(password)}"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(url, json=payload)
    except httpx.HTTPError as e:
        raise BlueBubblesError(f"network: {type(e).__name__}: {e}") from e
    if resp.status_code in (401, 403):
        raise BlueBubblesError("BlueBubbles rejected the password")
    if resp.status_code >= 500:
        raise BlueBubblesError(f"BlueBubbles server error HTTP {resp.status_code}")
    try:
        data = resp.json()
    except ValueError:
        raise BlueBubblesError(f"non-JSON response (HTTP {resp.status_code})") from None
    if resp.status_code >= 400 or data.get("status", 200) >= 400:
        message = data.get("message") or data.get("error") or f"HTTP {resp.status_code}"
        raise BlueBubblesError(f"BlueBubbles: {message}")
    return data


async def send_text(
    server_url: str, password: str, target: str, text: str
) -> None:
    """Send a text bubble via BlueBubbles. Raises :class:`BlueBubblesError`."""
    chat_guid = chat_guid_for(target)
    await _post(
        server_url,
        password,
        "/api/v1/message/text",
        {
            "chatGuid": chat_guid,
            "tempGuid": _next_temp_guid(chat_guid + text),
            "message": text,
            "method": _SEND_METHOD,
        },
    )


async def send_file(
    server_url: str, password: str, target: str, file_path: Path
) -> None:
    """Send an attachment via BlueBubbles' multipart endpoint."""
    chat_guid = chat_guid_for(target)
    url = (
        f"{server_url.rstrip('/')}/api/v1/message/attachment"
        f"?password={quote(password)}"
    )
    name = file_path.name
    try:
        data = await asyncio.to_thread(file_path.read_bytes)
    except OSError as e:
        raise BlueBubblesError(f"cannot read attachment: {e}") from e
    payload = {
        "chatGuid": chat_guid,
        "tempGuid": _next_temp_guid(chat_guid + name),
        "name": name,
    }
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(
                url, data=payload, files={"attachment": (name, data)}
            )
    except httpx.HTTPError as e:
        raise BlueBubblesError(f"network: {type(e).__name__}") from e
    if resp.status_code >= 400:
        raise BlueBubblesError(f"attachment send failed (HTTP {resp.status_code})")


async def health_check(server_url: str, password: str) -> bool:
    """True if the BlueBubbles server answers its ping endpoint."""
    url = f"{server_url.rstrip('/')}/api/v1/ping?password={quote(password)}"
    try:
        async with httpx.AsyncClient(timeout=_PING_TIMEOUT) as client:
            resp = await client.get(url)
        return resp.status_code == 200
    except httpx.HTTPError as e:
        logger.debug(f"BlueBubbles ping failed: {e}")
        return False


# ── inbound: webhook registration + attachment download ─────────────────


async def register_webhook(
    server_url: str, password: str, webhook_url: str
) -> bool:
    """Register ``webhook_url`` with BlueBubbles for new-message events.

    Idempotent: skips if an identical URL is already registered. Returns
    True if the webhook is registered (newly or already), False on error
    (the user can add it manually in the BlueBubbles UI).
    """
    base = server_url.rstrip("/")
    pw = quote(password)
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            existing = await client.get(f"{base}/api/v1/webhook?password={pw}")
            if existing.status_code == 200:
                try:
                    hooks = existing.json().get("data", []) or []
                    if any(h.get("url") == webhook_url for h in hooks):
                        return True
                except ValueError:
                    pass
            resp = await client.post(
                f"{base}/api/v1/webhook?password={pw}",
                json={
                    "url": webhook_url,
                    "events": ["new-message", "updated-message"],
                },
            )
        if resp.status_code < 400:
            return True
        logger.warning(
            f"BlueBubbles webhook registration HTTP {resp.status_code}; "
            f"add {webhook_url} manually in BlueBubbles → Settings → API"
        )
        return False
    except httpx.HTTPError as e:
        logger.warning(f"BlueBubbles webhook registration failed: {e}")
        return False


async def download_attachment(
    server_url: str, password: str, attachment_guid: str, dest: Path
) -> bool:
    """Download an attachment's bytes to ``dest``. True on success."""
    url = (
        f"{server_url.rstrip('/')}/api/v1/attachment/{quote(attachment_guid)}"
        f"/download?password={quote(password)}"
    )
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(url)
        if resp.status_code != 200:
            logger.warning(
                f"BlueBubbles attachment {attachment_guid} HTTP {resp.status_code}"
            )
            return False
        await asyncio.to_thread(dest.write_bytes, resp.content)
        return True
    except httpx.HTTPError as e:
        logger.warning(f"BlueBubbles attachment download failed: {e}")
        return False
