"""The model catalog Flowly picks from — cached, searchable, honest about fit.

Three rules shape this module.

**The list is discovered, not written down.** A hard-coded list goes stale the
day it ships and cannot grow a video section without a release. The catalog is
fetched from the provider and cached on disk.

**A stale answer beats no answer.** The cache is served past its TTL whenever
the network fails, because a user picking a model on a flaky connection wants
yesterday's list, not an error. Only a cold cache with no network falls back to
the small built-in manifest, which exists to make a first run work — never as
the primary source.

**Listable is not the same as runnable.** Hundreds of models are visible; Flowly
can only drive the ones whose required inputs it knows how to fill. Every model
therefore carries a compatibility verdict, and an ``unsupported`` one is shown
but refused rather than sent a payload it will reject.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from loguru import logger

from flowly.media.fal_catalog import (
    CATEGORIES,
    IMAGE_CATEGORIES,
    VIDEO_CATEGORIES,
    CatalogError,
)

# Six hours: long enough that a chatty session never re-syncs, short enough that
# a model added this morning is pickable this afternoon.
CACHE_TTL_SECONDS = 6 * 60 * 60

COMPAT_READY = "ready"
COMPAT_COMPATIBLE = "compatible"
COMPAT_UNSUPPORTED = "unsupported"

__all__ = [
    "CATEGORIES",
    "CACHE_TTL_SECONDS",
    "COMPAT_READY",
    "COMPAT_COMPATIBLE",
    "COMPAT_UNSUPPORTED",
    "IMAGE_CATEGORIES",
    "VIDEO_CATEGORIES",
    "MediaModel",
    "ModelCatalog",
    "FALLBACK_MODELS",
]


@dataclass(frozen=True, slots=True)
class MediaModel:
    """One runnable endpoint, as the picker and the tools see it."""

    endpoint_id: str
    label: str
    category: str
    provider: str = "fal"
    status: str = "active"
    description: str = ""
    tags: tuple[str, ...] = ()
    thumbnail_url: str = ""
    updated_at: str = ""
    compatibility: str = COMPAT_COMPATIBLE
    # Populated only after the endpoint's OpenAPI is fetched (on selection/run).
    input_schema: dict[str, Any] | None = field(default=None, compare=False)

    @property
    def is_video(self) -> bool:
        return self.category in VIDEO_CATEGORIES

    @property
    def is_runnable(self) -> bool:
        return self.compatibility != COMPAT_UNSUPPORTED

    def to_dict(self) -> dict[str, Any]:
        """Wire/cache shape. The OpenAPI schema is deliberately excluded — it is
        large, it is refetched on demand, and no client needs it."""
        return {
            "endpointId": self.endpoint_id,
            "label": self.label,
            "category": self.category,
            "provider": self.provider,
            "status": self.status,
            "description": self.description,
            "tags": list(self.tags),
            "thumbnailUrl": self.thumbnail_url,
            "updatedAt": self.updated_at,
            "compatibility": self.compatibility,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MediaModel | None":
        if not isinstance(data, dict):
            return None
        endpoint_id = data.get("endpointId") or data.get("endpoint_id")
        if not isinstance(endpoint_id, str) or not endpoint_id:
            return None
        tags = data.get("tags")
        return cls(
            endpoint_id=endpoint_id,
            label=str(data.get("label") or endpoint_id),
            category=str(data.get("category") or ""),
            provider=str(data.get("provider") or "fal"),
            status=str(data.get("status") or "active"),
            description=str(data.get("description") or ""),
            tags=tuple(t for t in (tags or []) if isinstance(t, str)),
            thumbnail_url=str(data.get("thumbnailUrl") or ""),
            updated_at=str(data.get("updatedAt") or ""),
            compatibility=str(data.get("compatibility") or COMPAT_COMPATIBLE),
        )


def model_from_catalog_entry(entry: dict[str, Any]) -> MediaModel | None:
    """Map one ``{endpoint_id, metadata}`` catalog row to a :class:`MediaModel`."""
    if not isinstance(entry, dict):
        return None
    endpoint_id = entry.get("endpoint_id")
    if not isinstance(endpoint_id, str) or not endpoint_id:
        return None
    meta = entry.get("metadata")
    meta = meta if isinstance(meta, dict) else {}
    tags = meta.get("tags")
    return MediaModel(
        endpoint_id=endpoint_id,
        label=str(meta.get("display_name") or endpoint_id),
        category=str(meta.get("category") or ""),
        status=str(meta.get("status") or "active"),
        description=str(meta.get("description") or ""),
        tags=tuple(t for t in (tags or []) if isinstance(t, str)),
        thumbnail_url=str(meta.get("thumbnail_url") or ""),
        updated_at=str(meta.get("updated_at") or ""),
        compatibility=COMPAT_COMPATIBLE,
    )


# Enough to make a fresh machine with no network useful, and no more. Anything
# longer would quietly become the real catalog again — the failure mode this
# module exists to end.
FALLBACK_MODELS: tuple[MediaModel, ...] = (
    MediaModel("fal-ai/flux/schnell", "FLUX.1 [schnell]", "text-to-image",
               description="fastest and cheapest"),
    MediaModel("fal-ai/flux/dev", "FLUX.1 [dev]", "text-to-image",
               description="balanced quality and speed"),
    MediaModel("fal-ai/flux/dev/image-to-image", "FLUX.1 [dev] image-to-image",
               "image-to-image", description="edit an existing image"),
    MediaModel("fal-ai/kling-video/v3/standard/text-to-video",
               "Kling Video v3 [standard]", "text-to-video",
               description="text prompt to clip"),
    MediaModel("fal-ai/kling-video/v3/standard/image-to-video",
               "Kling Video v3 image-to-video", "image-to-video",
               description="animate a still image"),
)


class ModelCatalog:
    """Cached access to the provider's model list.

    One instance per agent process. The disk cache is shared across processes,
    so a gateway restart does not re-sync.
    """

    def __init__(self, *, api_key: str = "", cache_path: Path | None = None,
                 ttl_seconds: int = CACHE_TTL_SECONDS) -> None:
        self._api_key = api_key
        self._ttl = ttl_seconds
        self._cache_path = cache_path
        self._memory: dict[str, list[MediaModel]] = {}
        self._fetched_at: float = 0.0

    # -- cache file -------------------------------------------------------

    @property
    def cache_path(self) -> Path:
        if self._cache_path is not None:
            return self._cache_path
        from flowly.profile import get_flowly_home

        return get_flowly_home() / "media" / "model-catalog.json"

    def _read_cache(self) -> tuple[dict[str, list[MediaModel]], float]:
        try:
            raw = json.loads(self.cache_path.read_text())
        except (OSError, json.JSONDecodeError, ValueError):
            return {}, 0.0
        if not isinstance(raw, dict):
            return {}, 0.0
        fetched_at = raw.get("fetchedAt")
        fetched_at = float(fetched_at) if isinstance(fetched_at, (int, float)) else 0.0
        models: dict[str, list[MediaModel]] = {}
        for category, entries in (raw.get("categories") or {}).items():
            if not isinstance(entries, list):
                continue
            parsed = [MediaModel.from_dict(e) for e in entries]
            models[str(category)] = [m for m in parsed if m is not None]
        return models, fetched_at

    def _write_cache(self, models: dict[str, list[MediaModel]], fetched_at: float) -> None:
        payload = {
            "version": 1,
            "fetchedAt": fetched_at,
            "categories": {c: [m.to_dict() for m in ms] for c, ms in models.items()},
        }
        path = self.cache_path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            # Atomic: a half-written catalog must never be read back as truth.
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload))
            tmp.replace(path)
        except OSError as exc:
            logger.debug("[media] could not write the model cache: {}", exc)

    def _is_fresh(self, fetched_at: float) -> bool:
        return bool(fetched_at) and (time.time() - fetched_at) < self._ttl

    # -- loading ----------------------------------------------------------

    async def _sync(self) -> dict[str, list[MediaModel]]:
        """Pull every category from the provider."""
        import httpx

        from flowly.media.fal_catalog import fetch_category

        out: dict[str, list[MediaModel]] = {}
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0)) as client:
            for category in CATEGORIES:
                entries = await fetch_category(category, api_key=self._api_key, client=client)
                parsed = [model_from_catalog_entry(e) for e in entries]
                out[category] = [m for m in parsed if m is not None and m.category]
        return out

    async def load(self, *, force: bool = False) -> dict[str, list[MediaModel]]:
        """Categories → models, from memory, disk, network, or the manifest.

        Never raises: a picker that cannot be opened is worse than a picker
        showing a day-old list.
        """
        if not force and self._memory and self._is_fresh(self._fetched_at):
            return self._memory

        cached, fetched_at = self._read_cache()
        if not force and cached and self._is_fresh(fetched_at):
            self._memory, self._fetched_at = cached, fetched_at
            return cached

        try:
            fresh = await self._sync()
            if any(fresh.values()):
                now = time.time()
                self._memory, self._fetched_at = fresh, now
                self._write_cache(fresh, now)
                return fresh
            logger.warning("[media] model catalog sync returned nothing; keeping what we have")
        except CatalogError as exc:
            logger.warning("[media] model catalog sync failed: {}", exc)
        except Exception as exc:  # noqa: BLE001 - a picker must still open
            logger.warning("[media] model catalog sync error: {}", exc)

        if cached:
            # Stale, and better than nothing. Serve it and say so.
            logger.info("[media] serving a stale model catalog (sync unavailable)")
            self._memory, self._fetched_at = cached, fetched_at
            return cached

        logger.info("[media] no model catalog available; using the built-in shortlist")
        fallback: dict[str, list[MediaModel]] = {c: [] for c in CATEGORIES}
        for model in FALLBACK_MODELS:
            fallback.setdefault(model.category, []).append(model)
        return fallback

    # -- queries ----------------------------------------------------------

    async def list_models(
        self, *, category: str | None = None, force: bool = False
    ) -> list[MediaModel]:
        catalog = await self.load(force=force)
        if category:
            return list(catalog.get(category, []))
        return [m for models in catalog.values() for m in models]

    async def search(
        self, query: str, *, category: str | None = None, limit: int = 50
    ) -> list[MediaModel]:
        """Substring match over id, label, description and tags.

        Ranked so an id/label hit beats a description hit — typing "kling"
        should surface Kling models, not everything that mentions Kling.
        """
        models = await self.list_models(category=category)
        needle = (query or "").strip().lower()
        if not needle:
            return models[:limit]

        scored: list[tuple[int, MediaModel]] = []
        for model in models:
            if needle in model.endpoint_id.lower():
                scored.append((0, model))
            elif needle in model.label.lower():
                scored.append((1, model))
            elif any(needle in tag.lower() for tag in model.tags):
                scored.append((2, model))
            elif needle in model.description.lower():
                scored.append((3, model))
        scored.sort(key=lambda pair: (pair[0], pair[1].label.lower()))
        return [model for _rank, model in scored[:limit]]

    async def get(self, endpoint_id: str, *, with_schema: bool = False) -> MediaModel | None:
        """One model by id, optionally with its input schema and a real verdict.

        Compatibility is only meaningful once the schema is known: without it
        every model looks plausible. Callers that are about to RUN a model must
        pass ``with_schema=True``.
        """
        if not endpoint_id:
            return None
        for model in await self.list_models():
            if model.endpoint_id == endpoint_id:
                return await self._with_schema(model) if with_schema else model
        if not with_schema:
            return None
        # Not in the cached list — it may be brand new, or the cache may be the
        # built-in shortlist, or the list may simply be stale. Ask the provider
        # directly before declaring the id unknown.
        return await self._fetch_one(endpoint_id)

    async def _fetch_one(self, endpoint_id: str) -> MediaModel | None:
        """Resolve one model straight from the provider, schema included.

        Returns ``None`` ONLY when the provider does not know the endpoint id.
        A model it does know but whose schema we couldn't read comes back with
        ``input_schema=None``. Anything that stopped us from asking at all
        raises :class:`~flowly.media.fal_catalog.CatalogError`.
        """
        from flowly.media.fal_catalog import fetch_model

        # Deliberately NOT swallowed: a throttled or unreachable catalog is not
        # evidence that the model is missing, and reporting it as "unknown id"
        # sends the user off renaming a model that was fine all along.
        entry = await fetch_model(endpoint_id, api_key=self._api_key)
        if entry is None:
            return None
        model = model_from_catalog_entry(entry)
        if model is None:
            return None
        return self._apply_openapi(model, entry.get("openapi"))

    async def _with_schema(self, model: MediaModel) -> MediaModel:
        """Attach the schema to a model already known from the cached list."""
        from flowly.media.fal_catalog import fetch_openapi

        try:
            openapi = await fetch_openapi(model.endpoint_id, api_key=self._api_key)
        except CatalogError as exc:
            logger.debug("[media] schema fetch failed for {}: {}", model.endpoint_id, exc)
            return model
        return self._apply_openapi(model, openapi)

    @staticmethod
    def _apply_openapi(model: MediaModel, openapi: Any) -> MediaModel:
        from flowly.media.adapters import input_schema_from_openapi, verdict_for

        schema = input_schema_from_openapi(openapi if isinstance(openapi, dict) else None)
        if schema is None:
            return model
        return replace(
            model,
            input_schema=schema,
            compatibility=verdict_for(model.category, schema),
        )
