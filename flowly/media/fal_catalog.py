"""fal's model catalog over HTTP.

Flowly used to ship a hand-written list of six image models. That was a fine
stopgap while there was no catalog API, but it meant every new model — and
every video model, of which there were none in the list — was invisible until
someone edited the source and cut a release.

fal does publish a catalog: ``GET https://api.fal.ai/v1/models`` returns cursor
-paginated entries of ``{endpoint_id, metadata}``, filterable by category and
status, and ``expand=openapi-3.0`` attaches the endpoint's full OpenAPI
document. This module is the transport for it; :mod:`flowly.media.catalog`
holds the caching and the decisions.

Listing deliberately does NOT expand schemas. There are hundreds of models and
their OpenAPI documents are large — the picker only needs names and categories,
and the schema is fetched for the one model the user actually chose.
"""

from __future__ import annotations

from typing import Any

import httpx

_CATALOG_BASE = "https://api.fal.ai/v1/models"
_TIMEOUT = httpx.Timeout(30.0, connect=10.0)
_UA = "flowly/media-catalog"

# fal's own category vocabulary, in the order a picker should show them.
CATEGORIES: tuple[str, ...] = (
    "text-to-image",
    "image-to-image",
    "text-to-video",
    "image-to-video",
)

VIDEO_CATEGORIES = frozenset({"text-to-video", "image-to-video"})
IMAGE_CATEGORIES = frozenset({"text-to-image", "image-to-image"})

# One page of the catalog. fal caps this server-side; we ask for a large page so
# a full category sync is a handful of round-trips rather than dozens.
_PAGE_LIMIT = 100
# Hard stop on pagination so a misbehaving cursor can't loop forever.
_MAX_PAGES = 40


class CatalogError(RuntimeError):
    """The catalog could not be read (network, auth, or malformed response)."""


def _headers(api_key: str | None) -> dict[str, str]:
    headers = {"User-Agent": _UA, "Accept": "application/json"}
    # The catalog is public, but sending the key when we have one keeps the
    # request attributable and out of stricter anonymous rate limits.
    if api_key:
        headers["Authorization"] = f"Key {api_key}"
    return headers


async def fetch_page(
    *,
    api_key: str | None = None,
    category: str | None = None,
    query: str | None = None,
    cursor: str | None = None,
    limit: int = _PAGE_LIMIT,
    expand_openapi: bool = False,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """One page: ``{"models": [...], "next_cursor": str|None, "has_more": bool}``."""
    params: dict[str, Any] = {"limit": max(1, min(int(limit), _PAGE_LIMIT)), "status": "active"}
    if category:
        params["category"] = category
    if query:
        params["q"] = query
    if cursor:
        params["cursor"] = cursor
    if expand_openapi:
        params["expand"] = "openapi-3.0"

    async def _get(c: httpx.AsyncClient) -> dict[str, Any]:
        try:
            response = await c.get(_CATALOG_BASE, params=params, headers=_headers(api_key))
        except httpx.HTTPError as exc:
            raise CatalogError(f"network error reading the model catalog: {exc}") from exc
        if response.status_code in (401, 403):
            raise CatalogError("fal rejected the API key while reading the model catalog.")
        if response.status_code != 200:
            raise CatalogError(f"model catalog returned HTTP {response.status_code}")
        try:
            data = response.json()
        except ValueError as exc:
            raise CatalogError(f"malformed model catalog response: {exc}") from exc
        if not isinstance(data, dict) or not isinstance(data.get("models"), list):
            raise CatalogError("model catalog response had no models array.")
        return data

    if client is not None:
        return await _get(client)
    async with httpx.AsyncClient(timeout=_TIMEOUT) as owned:
        return await _get(owned)


async def fetch_category(
    category: str,
    *,
    api_key: str | None = None,
    client: httpx.AsyncClient | None = None,
    max_pages: int = _MAX_PAGES,
) -> list[dict[str, Any]]:
    """Every active model in one category, following the cursor to the end."""
    out: list[dict[str, Any]] = []
    cursor: str | None = None
    seen_cursors: set[str] = set()

    async def _run(c: httpx.AsyncClient) -> None:
        nonlocal cursor
        for _ in range(max_pages):
            page = await fetch_page(
                api_key=api_key, category=category, cursor=cursor, client=c
            )
            out.extend(m for m in page["models"] if isinstance(m, dict))
            if not page.get("has_more"):
                return
            next_cursor = page.get("next_cursor")
            if not isinstance(next_cursor, str) or not next_cursor:
                return
            # A cursor that repeats means the server is looping us; stop rather
            # than spin until max_pages.
            if next_cursor in seen_cursors:
                return
            seen_cursors.add(next_cursor)
            cursor = next_cursor

    if client is not None:
        await _run(client)
    else:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as owned:
            await _run(owned)
    return out


async def fetch_openapi(
    endpoint_id: str,
    *,
    api_key: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any] | None:
    """The OpenAPI document for one endpoint, or ``None`` when fal has none.

    Fetched only for a model somebody is about to run — it is what tells us
    which inputs the endpoint actually takes, and therefore whether Flowly can
    drive it at all.
    """
    if not endpoint_id:
        return None
    page = await fetch_page(
        api_key=api_key,
        query=endpoint_id,
        limit=20,
        expand_openapi=True,
        client=client,
    )
    for entry in page["models"]:
        if isinstance(entry, dict) and entry.get("endpoint_id") == endpoint_id:
            openapi = entry.get("openapi")
            return openapi if isinstance(openapi, dict) else None
    return None
