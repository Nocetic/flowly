"""models.dev catalogue — universal per-provider model lists.

Instead of writing a bespoke ``/models`` fetcher per provider, we consume the
community catalogue at https://models.dev/api.json: 100+ providers with model
ids, display names, context windows, pricing, and capability flags
(``tool_call`` etc.). Two jobs:

  1. **Picker catalogues** — :func:`fetch_provider_models` fills the model
     picker for providers that have no bespoke fetcher (anthropic, openai,
     gemini, groq, zhipu). Filtered to tool-capable models, noise excluded.
  2. **Exact fit-check** — :func:`model_known` answers "does THIS provider
     serve THIS model id?" from cached data only, replacing prefix heuristics
     in ``model_fits_provider``. Returns ``None`` when no data is cached so the
     caller can fall back to its heuristic — and NEVER touches the network
     (it runs inside synchronous config writes, which must stay instant).

Resilience: 1 h in-memory cache → disk cache
(``<flowly home>/models_dev_cache.json``, served stale on network failure) →
caller's fallback (bespoke fetcher result / prefix heuristic). The catalogue is
read-only metadata — it is never on the inference path.
"""
from __future__ import annotations

import json
import re
import time
from typing import Any

from flowly.integrations.model_catalog import Model

MODELS_DEV_URL = "https://models.dev/api.json"
_MEM_TTL_S = 3600           # serve from memory for 1 h
_DISK_STALE_OK_S = 7 * 86400  # on network failure, accept disk cache up to 7 days old

# flowly provider key → models.dev provider id. Aggregator/account providers
# (flowly) and self-hosted (vllm) are deliberately absent — no catalogue
# applies; fit-checks fall back to the caller's heuristic.
PROVIDER_TO_MODELS_DEV: dict[str, str] = {
    "anthropic": "anthropic",
    "openai": "openai",
    "gemini": "google",
    "groq": "groq",
    "zhipu": "zai",
    "xai": "xai",
    "xai_oauth": "xai",
    "openrouter": "openrouter",
}

# Model ids that are noise for an agent picker (TTS, embeddings, dated preview
# snapshots, image-only…). The ``tool_call``
# capability flag already excludes most of these.
_NOISE_PATTERNS = re.compile(
    r"-tts\b|embedding|live-|-(preview|exp)-\d{2,4}[-_]|"
    r"-image\b|-image-preview\b|-customtools\b",
    re.IGNORECASE,
)

# In-memory cache of the full payload.
_mem_data: dict[str, Any] | None = None
_mem_time: float = 0.0


def _cache_path():
    from flowly.profile import get_flowly_home
    return get_flowly_home() / "models_dev_cache.json"


def _read_disk_cache(max_age_s: float) -> dict[str, Any] | None:
    try:
        p = _cache_path()
        if not p.exists():
            return None
        if (time.time() - p.stat().st_mtime) > max_age_s:
            return None
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) and data else None
    except Exception:  # noqa: BLE001
        return None


def _write_disk_cache(data: dict[str, Any]) -> None:
    try:
        _cache_path().write_text(json.dumps(data), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


async def _fetch_payload() -> dict[str, Any] | None:
    """Full models.dev payload: memory → fresh disk → network → stale disk."""
    global _mem_data, _mem_time
    if _mem_data is not None and (time.time() - _mem_time) < _MEM_TTL_S:
        return _mem_data

    fresh_disk = _read_disk_cache(_MEM_TTL_S)
    if fresh_disk is not None:
        _mem_data, _mem_time = fresh_disk, time.time()
        return fresh_disk

    try:
        import httpx
        async with httpx.AsyncClient(timeout=15.0) as c:
            r = await c.get(MODELS_DEV_URL, headers={"Accept": "application/json"})
        r.raise_for_status()
        data = r.json()
        if isinstance(data, dict) and data:
            _mem_data, _mem_time = data, time.time()
            _write_disk_cache(data)
            return data
    except Exception:  # noqa: BLE001
        pass

    stale = _read_disk_cache(_DISK_STALE_OK_S)
    if stale is not None:
        _mem_data, _mem_time = stale, time.time()
    return stale


def _provider_models_from(data: dict[str, Any] | None, provider_key: str) -> dict[str, Any] | None:
    if data is None:
        return None
    mdev_id = PROVIDER_TO_MODELS_DEV.get(provider_key)
    if not mdev_id:
        return None
    pdata = data.get(mdev_id)
    if not isinstance(pdata, dict):
        return None
    models = pdata.get("models")
    return models if isinstance(models, dict) and models else None


async def fetch_provider_models(provider_key: str) -> list[Model]:
    """Picker catalogue for ``provider_key`` from models.dev.

    Tool-capable models only, noise filtered, sorted by name. Empty list on
    any failure — the picker shows its "no catalogue" hint as before.
    """
    models = _provider_models_from(await _fetch_payload(), provider_key)
    if not models:
        return []
    out: list[Model] = []
    for mid, raw in models.items():
        if not isinstance(raw, dict):
            continue
        if not raw.get("tool_call", False):
            continue
        if _NOISE_PATTERNS.search(mid):
            continue
        limit = raw.get("limit") if isinstance(raw.get("limit"), dict) else {}
        cost = raw.get("cost") if isinstance(raw.get("cost"), dict) else {}
        ctx = limit.get("context")
        tags: list[str] = ["tools"]
        if raw.get("reasoning"):
            tags.append("reasoning")
        if raw.get("attachment"):
            tags.append("vision")
        out.append(Model(
            id=str(mid),
            name=str(raw.get("name") or mid),
            description="",
            context_window=int(ctx) if isinstance(ctx, (int, float)) and ctx > 0 else None,
            pricing_in=float(cost["input"]) if isinstance(cost.get("input"), (int, float)) else None,
            pricing_out=float(cost["output"]) if isinstance(cost.get("output"), (int, float)) else None,
            tags=tags,
        ))
    out.sort(key=lambda m: m.id)
    return out


def model_known(provider_key: str, model_id: str) -> bool | None:
    """Exact "does this provider serve this model?" from CACHED data only.

    Tri-state: True/False when the cached catalogue can answer, ``None`` when
    no data is available (unknown provider, never fetched, cache too old) —
    the caller falls back to its prefix heuristic. Synchronous and offline by
    design: this runs inside config writes which must never block on network.
    """
    model_id = (model_id or "").strip().lower()
    if not model_id:
        return None
    data = _mem_data if _mem_data is not None else _read_disk_cache(_DISK_STALE_OK_S)
    models = _provider_models_from(data, provider_key)
    if not models:
        return None
    return any(model_id == str(mid).strip().lower() for mid in models)
