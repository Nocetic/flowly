"""FAL image-generation client.

Calls FAL's synchronous endpoint (``POST https://fal.run/{model}``), then
downloads the result image(s) into ``<flowly home>/media`` so they flow through
Flowly's existing media delivery (``/api/media`` for remote/web/iOS clients,
direct upload for messaging channels). Returns local paths + source URLs.

Sync is fine for image models (a few seconds); long jobs/video can move to the
queue API later. Network failures and auth errors are raised as ``FalError`` with
a short, user-facing message.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import httpx

_FAL_SYNC = "https://fal.run"
# Generation can take a while; the download leg is quick.
_TIMEOUT = httpx.Timeout(180.0, connect=10.0)
_UA = "flowly/media-fal"


class FalError(RuntimeError):
    """A FAL request failed (auth, network, or malformed response)."""


def media_dir() -> Path:
    from flowly.profile import get_flowly_home

    d = get_flowly_home() / "media"
    d.mkdir(parents=True, exist_ok=True)
    return d


async def generate_image(
    *,
    api_key: str,
    model: str,
    prompt: str,
    image_size: str = "landscape_4_3",
    num_images: int = 1,
    output_format: str = "png",
) -> dict[str, Any]:
    """Generate image(s) and download them to the media dir.

    Returns ``{"paths": [...], "urls": [...], "model": str, "seed": Any}``.
    """
    if not (api_key or "").strip():
        raise FalError("FAL API key is missing — set it in setup.")
    if not (prompt or "").strip():
        raise FalError("prompt is empty.")

    payload: dict[str, Any] = {
        "prompt": prompt.strip(),
        "num_images": max(1, min(int(num_images or 1), 4)),
        "output_format": output_format,
    }
    if image_size:
        payload["image_size"] = image_size

    headers = {"Authorization": f"Key {api_key}", "Content-Type": "application/json", "User-Agent": _UA}
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        try:
            r = await client.post(f"{_FAL_SYNC}/{model}", headers=headers, json=payload)
        except httpx.HTTPError as exc:
            raise FalError(f"network error contacting FAL: {exc}") from exc
        if r.status_code in (401, 403):
            raise FalError("FAL rejected the API key (401/403).")
        if r.status_code != 200:
            raise FalError(f"FAL returned HTTP {r.status_code}: {r.text[:200]}")
        try:
            data = r.json()
        except Exception as exc:  # noqa: BLE001
            raise FalError(f"malformed FAL response: {exc}") from exc

    images = data.get("images") or []
    if not images:
        raise FalError("FAL returned no images.")

    ext = "png" if output_format == "png" else "jpg"
    paths: list[str] = []
    urls: list[str] = []
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        for img in images:
            src = (img or {}).get("url")
            if not src:
                continue
            urls.append(src)
            try:
                resp = await client.get(src)
                resp.raise_for_status()
            except httpx.HTTPError:
                continue
            dest = media_dir() / f"img-{uuid.uuid4().hex[:12]}.{ext}"
            dest.write_bytes(resp.content)
            paths.append(str(dest))

    if not paths:
        raise FalError("generated image(s) could not be downloaded.")
    return {"paths": paths, "urls": urls, "model": model, "seed": data.get("seed")}
