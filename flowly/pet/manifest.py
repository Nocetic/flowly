"""Petdex manifest fetch + short-lived in-memory cache.

The manifest lists available pets (slug, name, spritesheet/thumbnail URLs, state
metadata). We cache it for a few minutes so the gallery is snappy and we don't
hammer petdex.dev. Network/parse failures raise :class:`PetManifestError` so the
caller can fall back to installed pets.
"""

from __future__ import annotations

import time
from typing import Any

from flowly.pet.store import USER_AGENT

MANIFEST_URL = "https://petdex.dev/api/manifest"
CACHE_TTL_SECONDS = 300  # 5 minutes

_cache: dict[str, Any] | None = None
_cache_at: float = 0.0


class PetManifestError(Exception):
    """The Petdex manifest could not be fetched or parsed."""


def _now() -> float:
    return time.monotonic()


def clear_cache() -> None:
    """Drop the cached manifest (used by tests and after a forced refresh)."""
    global _cache, _cache_at
    _cache = None
    _cache_at = 0.0


async def fetch_manifest(*, force: bool = False, client: Any = None) -> dict[str, Any]:
    """Return the Petdex manifest dict, cached for ``CACHE_TTL_SECONDS``.

    Raises :class:`PetManifestError` on network/parse failure. ``client`` may be
    an injected ``httpx.AsyncClient`` (tests); when omitted one is created here.
    """
    global _cache, _cache_at
    if not force and _cache is not None and (_now() - _cache_at) < CACHE_TTL_SECONDS:
        return _cache

    import httpx

    own_client = client is None
    if own_client:
        client = httpx.AsyncClient(timeout=15.0, headers={"user-agent": USER_AGENT})
    try:
        resp = await client.get(MANIFEST_URL)
        resp.raise_for_status()
        data = resp.json()
    except httpx.HTTPError as exc:
        raise PetManifestError(f"failed to fetch Petdex manifest: {exc}") from exc
    except ValueError as exc:  # JSON decode error
        raise PetManifestError(f"invalid Petdex manifest JSON: {exc}") from exc
    finally:
        if own_client:
            await client.aclose()

    if not isinstance(data, dict):
        raise PetManifestError("Petdex manifest is not a JSON object")

    _cache = data
    _cache_at = _now()
    return data


def pets_from_manifest(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract the list of pet entries from a manifest, tolerant of shape."""
    pets = manifest.get("pets")
    if isinstance(pets, list):
        return [p for p in pets if isinstance(p, dict)]
    return []
