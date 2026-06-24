"""Local, profile-aware storage for pet assets.

Layout (under the active profile via ``get_flowly_home()``):

    <home>/pets/<slug>/pet.json          — metadata (manifest entry + local paths)
    <home>/pets/<slug>/spritesheet.<ext> — the downloaded spritesheet
    <home>/pets/<slug>/thumb.png         — cached thumbnail (optional)

Downloads are **host-pinned to petdex.dev**, **size-capped**, and written
**atomically** through a ``.part`` temp file so a crash mid-download never
leaves a half-written asset in place.
"""

from __future__ import annotations

import json
import os
import re
import secrets
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from flowly.profile import get_flowly_home

# Pet assets may only ever be fetched from Petdex (or a subdomain of it).
_ALLOWED_HOST = "petdex.dev"
# Hard cap on a single downloaded asset (spritesheet / thumbnail).
MAX_ASSET_BYTES = 20 * 1024 * 1024  # 20 MiB
# User-agent for outbound Petdex requests.
USER_AGENT = "flowly-petdex"

_SLUG_RE = re.compile(r"[^a-z0-9_-]")


class PetStoreError(Exception):
    """Storage/download failure: bad slug, blocked host, or oversized asset."""


# ── slug + paths ────────────────────────────────────────────────────────────

def safe_slug(slug: str) -> str:
    """Normalise a pet slug to a filesystem-safe token. Strips path traversal
    and any character outside ``[a-z0-9_-]``. Raises on an empty result."""
    s = _SLUG_RE.sub("", (slug or "").strip().lower())
    if not s:
        raise PetStoreError(f"invalid pet slug: {slug!r}")
    return s


def pets_root() -> Path:
    return get_flowly_home() / "pets"


def pet_dir(slug: str) -> Path:
    return pets_root() / safe_slug(slug)


# ── host pinning ─────────────────────────────────────────────────────────────

def is_allowed_url(url: str) -> bool:
    """True only for ``https`` URLs on ``petdex.dev`` (or a subdomain)."""
    try:
        u = urlparse(url)
    except ValueError:
        return False
    host = (u.hostname or "").lower()
    if u.scheme != "https" or not host:
        return False
    return host == _ALLOWED_HOST or host.endswith("." + _ALLOWED_HOST)


def is_allowed_response_chain(resp: Any) -> bool:
    """True only if the final URL **and every redirect hop** stayed host-pinned.

    Following redirects (petdex.dev → assets.petdex.dev) is required, but a
    redirect must never carry us off ``petdex.dev`` — that would defeat the
    host pin. Validate the whole chain, not just the URL we were handed.
    """
    history = getattr(resp, "history", None) or []
    urls = [str(resp.url)] + [str(h.url) for h in history]
    return all(is_allowed_url(u) for u in urls)


# ── metadata ─────────────────────────────────────────────────────────────────

def list_installed() -> list[str]:
    """Slugs of pets installed on disk (those with a ``pet.json``)."""
    root = pets_root()
    if not root.is_dir():
        return []
    return sorted(
        d.name for d in root.iterdir() if d.is_dir() and (d / "pet.json").is_file()
    )


def read_meta(slug: str) -> dict[str, Any] | None:
    p = pet_dir(slug) / "pet.json"
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def write_meta(slug: str, meta: dict[str, Any]) -> None:
    atomic_write_bytes(
        pet_dir(slug) / "pet.json",
        json.dumps(meta, indent=2).encode("utf-8"),
    )


# ── atomic writes / downloads ────────────────────────────────────────────────

def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write *data* to *path* atomically via a ``.part`` temp + ``os.replace``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".part.{secrets.token_hex(4)}")
    try:
        tmp.write_bytes(data)
        os.replace(tmp, path)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


async def download_asset(url: str, dest: Path, *, client: Any = None) -> Path:
    """Download a host-pinned, size-capped asset to *dest* atomically.

    Raises :class:`PetStoreError` for a blocked host or an oversized asset.
    ``client`` may be an injected ``httpx.AsyncClient`` (used by tests); when
    omitted one is created and closed here.
    """
    if not is_allowed_url(url):
        raise PetStoreError(f"blocked asset host (only {_ALLOWED_HOST}): {url}")

    import httpx

    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + f".part.{secrets.token_hex(4)}")

    own_client = client is None
    if own_client:
        client = httpx.AsyncClient(timeout=30.0, headers={"user-agent": USER_AGENT})
    try:
        total = 0
        async with client.stream("GET", url, follow_redirects=True) as resp:
            resp.raise_for_status()
            if not is_allowed_response_chain(resp):
                raise PetStoreError(f"asset redirected off-host: {resp.url}")
            declared = resp.headers.get("content-length")
            if declared and declared.isdigit() and int(declared) > MAX_ASSET_BYTES:
                raise PetStoreError(f"asset too large: {declared} bytes (cap {MAX_ASSET_BYTES})")
            with open(tmp, "wb") as fh:
                async for chunk in resp.aiter_bytes():
                    total += len(chunk)
                    if total > MAX_ASSET_BYTES:
                        raise PetStoreError(f"asset exceeds {MAX_ASSET_BYTES} bytes")
                    fh.write(chunk)
        os.replace(tmp, dest)
        return dest
    except Exception:
        Path(tmp).unlink(missing_ok=True)
        raise
    finally:
        if own_client:
            await client.aclose()
