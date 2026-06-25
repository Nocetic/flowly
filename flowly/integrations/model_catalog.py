"""Fetch the model catalog from the active LLM provider.

Provider-specific fetchers + a session-scoped in-memory cache. Today
only OpenRouter (and by extension the Flowly proxy, which
exposes the same ``/v1/models`` route) is wired — other providers fall
back to a static catalog or refuse the request with a clear hint.

Public surface
--------------
:func:`fetch_models(provider_key, *, force_refresh=False) -> list[Model]`
    Return the catalog for the given provider key, fetching the network
    only on first call (or when ``force_refresh=True``).

:func:`flush_cache()` — drop the in-memory cache (used after the user
    edits a provider key in case the new key unlocks more models).

The returned ``Model`` objects are intentionally minimal — just enough
fields for the picker UI. Add metadata fields here as the picker grows.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field as dc_field
from typing import Any

import httpx


@dataclass
class Model:
    """One row of the picker list."""
    id: str                              # canonical model id sent to the API
    name: str                            # human-friendly name (often same as id)
    description: str = ""                # tag like "free", "tools", "vision"
    context_window: int | None = None    # tokens, if reported
    pricing_in: float | None = None      # USD per 1M input tokens, if reported
    pricing_out: float | None = None     # USD per 1M output tokens
    tags: list[str] = dc_field(default_factory=list)


_TIMEOUT = httpx.Timeout(8.0, connect=3.0)
_UA = "flowly-tui/model-catalog"

# Session cache: provider_key → list[Model]. Cleared on flush_cache().
_CACHE: dict[str, list[Model]] = {}


async def fetch_models(provider_key: str, *, force_refresh: bool = False) -> list[Model]:
    """Get the model catalog for ``provider_key``.

    Empty list ⇒ "unknown / unsupported provider" — caller should show
    a hint pointing at the provider's website. Raises only on
    programmer error (unknown key with no fallback); network failures
    are swallowed and reported as an empty list.
    """
    if not force_refresh and provider_key in _CACHE:
        return _CACHE[provider_key]
    fetcher = _FETCHERS.get(provider_key)
    if fetcher is None:
        # No bespoke fetcher → models.dev catalogue (anthropic / openai /
        # gemini / groq / zhipu …). Cached + disk-backed; empty on failure,
        # so the picker's "no catalogue" hint still works as the last resort.
        from flowly.integrations import models_dev
        try:
            models = await models_dev.fetch_provider_models(provider_key)
        except Exception:
            models = []
        _CACHE[provider_key] = models
        return models
    try:
        models = await fetcher()
    except Exception:
        models = []
    _CACHE[provider_key] = models
    return models


def flush_cache() -> None:
    """Drop every cached provider catalog. Call after credentials change."""
    _CACHE.clear()


def get_context_window(model_id: str) -> int | None:
    """Look up a model's reported context_window from any cached catalog.

    Synchronous: only consults the in-memory cache (no network). Returns
    ``None`` if the model isn't in any cached catalog yet — caller falls
    back to its own heuristics. The status bar uses this to size its
    token-budget bar without baking in per-model magic numbers.

    Also normalizes between Flowly's LiteLLM dash convention
    (``claude-sonnet-4-5``) and OpenRouter's dot convention
    (``claude-sonnet-4.5``) — the Flowly proxy rewrites dashes to dots
    when forwarding, so the user's config can hold either form and we
    still find the catalog entry. Mirrors ``normalizeModelForOpenRouter``
    in ``flowly-app/app/api/v1/chat/completions/route.ts``.
    """
    if not model_id:
        return None
    candidates = {model_id, _dash_to_dot_version(model_id), _dot_to_dash_version(model_id)}
    candidates.discard("")
    for models in _CACHE.values():
        for m in models:
            if m.id in candidates and m.context_window:
                return m.context_window
    return None


# ── id normalization ──────────────────────────────────────────────


import re as _re

# Match version suffixes like ``-4-5`` or ``-4`` at end of a model id
# component. ``claude-sonnet-4-5`` → group "4-5" → normalised to "4.5".
_VERSION_DASH_RE = _re.compile(r"-(\d+)-(\d+)(?=$|[^0-9])")
_VERSION_DOT_RE = _re.compile(r"\.(\d+)(?=$|[^0-9])")


def _dash_to_dot_version(mid: str) -> str:
    """``claude-sonnet-4-5`` → ``claude-sonnet-4.5``. Idempotent + safe
    when there's no version suffix to convert."""
    if not mid:
        return mid
    return _VERSION_DASH_RE.sub(lambda m: f"-{m.group(1)}.{m.group(2)}", mid)


def _dot_to_dash_version(mid: str) -> str:
    """``claude-sonnet-4.5`` → ``claude-sonnet-4-5`` (the LiteLLM form
    Flowly's config writes by default)."""
    if not mid:
        return mid
    # Only rewrite when the preceding chunk looks like a version anchor
    # (digit). Avoids mangling names with legitimate dots.
    return _re.sub(r"(\d)\.(\d)", r"\1-\2", mid)


async def warm_cache(provider_key: str) -> None:
    """Background prefetch of a provider's catalog. Swallows errors so
    a failed warm-up never crashes the app — the lookup just falls
    back to heuristics until the user opens /model and the picker
    fetches it on demand."""
    try:
        await fetch_models(provider_key)
    except Exception:
        pass


# ── per-provider fetchers ──────────────────────────────────────────


async def _fetch_openrouter() -> list[Model]:
    """OpenRouter exposes a public ``/v1/models`` (no auth required).

    We filter for **tool-capable** models because the agent loop needs
    tool calling for every meaningful turn. Pricing is converted from
    OpenRouter's per-token string ("0.000003") into the per-1M USD float
    everyone else uses, so the picker can sort/display consistently.
    """
    url = "https://openrouter.ai/api/v1/models"
    async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
        r = await c.get(url, headers={"Accept": "application/json", "User-Agent": _UA})
    r.raise_for_status()
    out: list[Model] = []
    for item in r.json().get("data", []):
        mid = str(item.get("id") or "").strip()
        if not mid:
            continue
        # Tool-use filter: OpenRouter lists supported features under
        # ``supported_parameters``. ``tools`` covers function-calling.
        sp = item.get("supported_parameters") or []
        if "tools" not in sp:
            continue
        pricing = item.get("pricing") or {}
        # context_length lives at the root, but for some entries
        # OpenRouter only fills the provider-specific cap inside
        # ``top_provider.context_length``. Fall through both so the bar
        # never shows the wrong number for models whose root field is
        # absent or 0.
        ctx = (
            item.get("context_length")
            or (item.get("top_provider") or {}).get("context_length")
        )
        out.append(Model(
            id=mid,
            name=item.get("name") or mid,
            description=str(item.get("description") or "")[:140],
            context_window=int(ctx) if isinstance(ctx, int) and ctx > 0 else None,
            pricing_in=_per_million(pricing.get("prompt")),
            pricing_out=_per_million(pricing.get("completion")),
            tags=_openrouter_tags(item, pricing),
        ))
    # Sort: free first, then alphabetical
    out.sort(key=lambda m: (0 if "free" in m.tags else 1, m.id.lower()))
    return out


async def _fetch_flowly_hosted() -> list[Model]:
    """Fetch the model catalog from the Flowly proxy.

    The proxy at ``useflowlyapp.com/api/v1/models`` returns a
    **plan-filtered** list (see ``flowly-app/app/api/v1/models/route.ts``
    + ``lib/plans/allowlist.ts``): only models the caller's subscription
    tier can actually call. Each entry carries an ``allowed`` flag so we
    can grey out the rest in the picker (instead of letting the user
    pick one that the proxy then rejects with "not in your plan").

    Auth: whatever bearer the resolved Flowly provider uses — an ``flw_…``
    account key, or the legacy ``{serverId}:{gatewayAuthToken}`` pair. Using the
    SAME credential the completions call uses means the proxy resolves the same
    account and returns the same plan-filtered list. Without a usable Flowly
    credential we fall back to the full OpenRouter catalog so the picker still
    has something browsable.
    """
    from flowly.config.loader import load_config
    from flowly.integrations.active_provider import resolve_active_provider
    cfg = load_config()
    active = resolve_active_provider(cfg)
    if active is None or active.key != "flowly" or not active.api_key:
        # Flowly provider not usable (no key / different active provider) →
        # OpenRouter direct still gives a useful (unfiltered) view.
        return await _fetch_openrouter()
    base = (active.api_base or cfg.providers.flowly.api_base or "https://useflowlyapp.com/api/v1").rstrip("/")
    bearer = active.api_key
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            r = await c.get(
                f"{base}/models",
                headers={
                    "Authorization": f"Bearer {bearer}",
                    "Accept": "application/json",
                    "User-Agent": _UA,
                },
            )
        r.raise_for_status()
    except Exception:
        # Network / 401 / plan lookup failure — degrade to OpenRouter
        # so the picker doesn't open empty.
        return await _fetch_openrouter()
    out: list[Model] = []
    for item in r.json().get("data", []):
        mid = str(item.get("id") or "").strip()
        if not mid:
            continue
        pricing = item.get("pricing") or {}
        # Same defensive read as OpenRouter direct — Flowly proxy passes
        # through OR's schema, but a future proxy change could move ctx
        # into top_provider only.
        ctx = (
            item.get("context_length")
            or (item.get("top_provider") or {}).get("context_length")
        )
        tags: list[str] = []
        # Plan-aware: the proxy marks each entry ``allowed`` for the
        # caller's tier. Greyed-out entries get a ``locked`` tag so the
        # picker can render them differently — and we sort allowed
        # entries to the top so the user's working choices come first.
        if item.get("allowed") is False:
            tags.append("locked")
        if pricing.get("prompt") in (None, "", "0") and pricing.get("completion") in (None, "", "0"):
            tags.append("free")
        out.append(Model(
            id=mid,
            name=item.get("name") or mid,
            description=str(item.get("description") or "")[:140],
            context_window=int(ctx) if isinstance(ctx, int) else None,
            pricing_in=_per_million(pricing.get("prompt")),
            pricing_out=_per_million(pricing.get("completion")),
            tags=tags,
        ))
    # Allowed first, then alphabetical within each bucket.
    out.sort(key=lambda m: (1 if "locked" in m.tags else 0, m.id.lower()))
    return out


# Pin xAI's headline chat model to the top of the picker. Everything
# else is sorted alphabetically.
_XAI_TOP_MODEL = "grok-4.3"


def _xai_models_from_payload(data: Any) -> list[Model]:
    """Map xAI's ``/v1/models`` payload into picker rows.

    xAI returns chat models alongside media generators
    (``grok-imagine-image``, ``grok-imagine-video``…). The picker drives
    the agent's tool-calling chat loop, so we drop the ``imagine`` models
    that can't serve a chat turn — chat-only by intent. Pricing is omitted
    on purpose: OAuth users are on a flat subscription, and the API-key
    path's per-token cost isn't worth surfacing here.
    """
    out: list[Model] = []
    for item in data if isinstance(data, list) else []:
        if not isinstance(item, dict):
            continue
        mid = str(item.get("id") or "").strip()
        if not mid or "imagine" in mid.lower():
            continue
        tags: list[str] = []
        low = mid.lower()
        if "non-reasoning" in low:
            tags.append("fast")
        elif "reasoning" in low:
            tags.append("reasoning")
        if "multi-agent" in low:
            tags.append("multi-agent")
        out.append(Model(
            id=mid,
            name=mid,
            description=f"xAI · {item.get('owned_by') or 'xai'}",
            tags=tags,
        ))
    # grok-4.3 first, then alphabetical.
    out.sort(key=lambda m: (0 if m.id == _XAI_TOP_MODEL else 1, m.id.lower()))
    return out


async def _fetch_xai_models(api_key: str, base_url: str) -> list[Model]:
    """Shared ``/v1/models`` reader for both xAI auth paths."""
    if not api_key:
        return []
    async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
        r = await c.get(
            f"{base_url.rstrip('/')}/models",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Accept": "application/json",
                "User-Agent": _UA,
            },
        )
    r.raise_for_status()
    return _xai_models_from_payload(r.json().get("data"))


async def _fetch_xai_oauth() -> list[Model]:
    """xAI Grok via subscription OAuth.

    The bearer comes from the stored OAuth token (refreshed on demand by
    :func:`resolve_runtime_credentials`), so the picker shows exactly the
    models the signed-in subscription can call — the same source ``flowly
    xai test`` validates against.
    """
    from flowly.auth.xai_oauth import resolve_runtime_credentials
    creds = await asyncio.to_thread(resolve_runtime_credentials)
    if creds is None or not creds.api_key:
        return []
    return await _fetch_xai_models(creds.api_key, creds.base_url)


async def _fetch_xai_apikey() -> list[Model]:
    """xAI Grok via a BYOK ``XAI_API_KEY`` / configured ``providers.xai`` key."""
    import os
    api_key = ""
    base = "https://api.x.ai/v1"
    try:
        from flowly.config.loader import load_config
        xcfg = getattr(load_config().providers, "xai", None)
        api_key = str(getattr(xcfg, "api_key", "") or "").strip()
        base = str(getattr(xcfg, "api_base", "") or "").strip() or base
    except Exception:
        pass
    api_key = api_key or os.getenv("XAI_API_KEY", "").strip()
    return await _fetch_xai_models(api_key, base)


_FETCHERS: dict[str, "Any"] = {
    "openrouter": _fetch_openrouter,
    "flowly": _fetch_flowly_hosted,
    "xai_oauth": _fetch_xai_oauth,
    "xai": _fetch_xai_apikey,
    # anthropic / openai / gemini / groq / zhipu — not implemented yet.
    # Their /v1/models endpoints need the user's API key; we'll plumb
    # that through once the OpenRouter MVP feels good.
}


# ── helpers ────────────────────────────────────────────────────────


def _per_million(raw: Any) -> float | None:
    """OpenRouter ships pricing as USD-per-token strings. Convert to
    USD per 1M tokens (the unit everyone uses in conversation)."""
    try:
        if raw in (None, "", "0"):
            return None
        return float(raw) * 1_000_000.0
    except (TypeError, ValueError):
        return None


def _openrouter_tags(item: dict[str, Any], pricing: dict[str, Any]) -> list[str]:
    tags: list[str] = []
    try:
        if (
            (pricing.get("prompt") in (None, "", "0"))
            and (pricing.get("completion") in (None, "", "0"))
        ):
            tags.append("free")
    except Exception:
        pass
    arch = (item.get("architecture") or {})
    modality = arch.get("input_modalities") or arch.get("modality") or []
    if isinstance(modality, list) and any("image" in str(m).lower() for m in modality):
        tags.append("vision")
    elif isinstance(modality, str) and "image" in modality.lower():
        tags.append("vision")
    return tags
