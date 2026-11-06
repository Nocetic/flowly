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
fields for the picker UI. Flowly's account-specific ``plan`` and
``default_model`` response metadata is retained separately so login/startup
can reconcile a stale configured model without changing this public list API.
"""

from __future__ import annotations

import asyncio
import re as _re
from dataclasses import dataclass
from dataclasses import field as dc_field
from typing import Any

import httpx

from flowly.providers.effort import (
    FREE_PLAN_EFFORT_CAP,
    above_cap,
    anthropic_efforts,
    normalize_effort,
    openrouter_efforts,
    ordered_efforts,
    xai_efforts,
    xai_twin,
    XAI_SWITCH_LEVELS,
)
from flowly.providers.service_tier import ServiceTier, parse_service_tiers


@dataclass
class Model:
    """One row of the picker list."""
    id: str                              # canonical model id sent to the API
    name: str                            # human-friendly name (often same as id)
    description: str = ""                # tag like "free", "tools", "vision"
    context_window: int | None = None    # tokens, if reported
    pricing_in: float | None = None      # USD per 1M input tokens, if reported
    pricing_out: float | None = None     # USD per 1M output tokens
    # USD per 1M input tokens that came from the provider's prompt cache.
    # A cache hit is typically a tenth of a miss, and ``prompt_tokens`` counts
    # cached bytes at full weight, so pricing input alone overstates any
    # conversation with a stable prefix — which is every long chat and, most of
    # all, a group room where six members re-send the same room preamble.
    # ``None`` means the catalog didn't say; bill those tokens at input price
    # rather than inventing a discount.
    pricing_cache_read: float | None = None
    tags: list[str] = dc_field(default_factory=list)
    # Tri-state capability: True/False only when the upstream catalog states
    # it explicitly; None means unknown and must never be used to block a call.
    supports_vision: bool | None = None
    # Reasoning efforts the model takes, lowest first (flowly/providers/effort.py),
    # and the one it runs at when none is set. Empty: no effort picker.
    efforts: list[str] = dc_field(default_factory=list)
    default_effort: str | None = None
    # Whether the model thinks when no level is sent; False (an OpenRouter row's
    # ``default_enabled: false``) shows thinking off until the agent picks a level.
    default_thinking: bool = True
    # Faster tiers the model offers (flowly/providers/service_tier.py); empty:
    # standard speed only, no Fast switch.
    service_tiers: list[ServiceTier] = dc_field(default_factory=list)


@dataclass(frozen=True)
class FlowlyModelPolicy:
    """Account-specific metadata returned by Flowly's authenticated catalog."""

    plan: str
    default_model: str


_TIMEOUT = httpx.Timeout(8.0, connect=3.0)
_UA = "flowly-tui/model-catalog"

# Session cache: provider_key → list[Model]. Cleared on flush_cache().
_CACHE: dict[str, list[Model]] = {}
# The context a local model server runs a model with now (model id → tokens),
# kept current between turns (flowly/integrations/local_window.py). It
# outranks the catalogue, which was read once and goes stale when the owner
# reloads the model with another context.
_RUNTIME_WINDOWS: dict[str, int] = {}
_FLOWLY_POLICY: FlowlyModelPolicy | None = None


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
        if provider_key == "anthropic":
            _with_anthropic_efforts(models)
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
    global _FLOWLY_POLICY
    _CACHE.clear()
    _RUNTIME_WINDOWS.clear()
    _FLOWLY_POLICY = None


def get_flowly_model_policy() -> FlowlyModelPolicy | None:
    """Return metadata from the latest authenticated Flowly catalog fetch."""
    return _FLOWLY_POLICY


def cached_ids(provider_key: str) -> frozenset[str]:
    """The model ids the cached catalogue lists for ``provider_key`` (empty
    when it was not read): what a request may be switched to."""
    return frozenset(model.id for model in _CACHE.get(provider_key) or [])


def cached_efforts(provider_key: str, model_id: str) -> list[str] | None:
    """The efforts the cached catalogue lists for ``model_id``; ``None`` when
    that catalogue is not cached or does not list the model (nothing known,
    so nothing to hold a request against)."""
    for model in _CACHE.get(provider_key) or ():
        if model.id == model_id:
            return list(model.efforts)
    return None


def cached_service_tiers(provider_key: str, model_id: str) -> list[str] | None:
    """The tier ids the cached catalogue lists for ``model_id``; ``None``
    when nothing is known (as ``cached_efforts``)."""
    for model in _CACHE.get(provider_key) or ():
        if model.id == model_id:
            return [tier.id for tier in model.service_tiers]
    return None


def _catalogue_row(models: list[Model], model_id: str) -> Model | None:
    """``model_id``'s row, matching a dotted or dashed version either way."""
    candidates = {model_id, _dash_to_dot_version(model_id), _dot_to_dash_version(model_id)}
    return next((model for model in models if model.id in candidates), None)


def flowly_effort_cap() -> str | None:
    """The highest effort the account's Flowly credit runs at: ``medium`` on
    the free plan, none on the others or before the plan is known (the proxy
    caps the free plan either way)."""
    policy = _FLOWLY_POLICY
    if policy is not None and policy.plan.strip().lower() == "free":
        return FREE_PLAN_EFFORT_CAP
    return None


@dataclass(frozen=True)
class EffortChoice:
    """What an agent may set as its reasoning effort for one model."""

    #: The levels the model takes; ``None`` when the catalogue does not know
    #: the model (offline, or a model typed by hand): nothing to check against.
    levels: list[str] | None
    default: str | None
    #: The highest level the account's plan runs at (free Flowly plan only).
    cap: str | None


async def effort_choice(provider_key: str, model_id: str) -> EffortChoice:
    """The efforts ``model_id`` takes on ``provider_key``, from its catalogue."""
    models = await fetch_models(provider_key) if provider_key else []
    # After the fetch: Flowly's catalogue is where the plan is learned.
    cap = flowly_effort_cap() if provider_key == "flowly" else None
    row = _catalogue_row(models, model_id)
    if row is None:
        return EffortChoice(None, None, cap)
    return EffortChoice(list(row.efforts), row.default_effort, cap)


def effort_refusal(effort: str, choice: EffortChoice) -> tuple[str, str] | None:
    """Why ``effort`` (a known level) cannot be set, as ``(code, message)``;
    ``None`` when it can."""
    if choice.levels is not None and effort not in choice.levels:
        if not choice.levels:
            return "EFFORT_UNSUPPORTED", "This model takes no reasoning effort."
        return "EFFORT_UNSUPPORTED", f"This model takes {', '.join(choice.levels)}, not {effort}."
    if above_cap(effort, choice.cap):
        return "EFFORT_NOT_IN_PLAN", f"Your plan's credit goes up to {choice.cap} effort."
    return None


async def service_tier_choice(provider_key: str, model_id: str) -> list[str] | None:
    """The tier ids ``model_id`` offers on ``provider_key``, from its
    catalogue; ``None`` when the catalogue does not know the model."""
    models = await fetch_models(provider_key) if provider_key else []
    row = _catalogue_row(models, model_id)
    return None if row is None else [tier.id for tier in row.service_tiers]


def service_tier_refusal(tier: str, tiers: list[str] | None) -> tuple[str, str] | None:
    """Why ``tier`` (a valid id) cannot be set, as ``(code, message)``;
    ``None`` when it can. A model the catalogue does not know is refused too:
    no catalogue has ever offered it a tier."""
    if tiers is not None and tier in tiers:
        return None
    if not tiers:
        return "SERVICE_TIER_UNSUPPORTED", "This model has no faster tier."
    return "SERVICE_TIER_UNSUPPORTED", f"This model offers {', '.join(tiers)}, not {tier}."


def _with_anthropic_efforts(models: list[Model]) -> None:
    """Anthropic's catalogue (models.dev) says nothing of effort: the levels
    follow the model family (``anthropic_efforts``)."""
    for model in models:
        if not model.efforts:
            model.efforts, model.default_effort = anthropic_efforts(model.id)


def _openrouter_efforts(item: dict[str, Any]) -> tuple[list[str], str | None, bool]:
    """An OpenRouter row's levels, default and default thinking
    (``openrouter_efforts``); the Flowly proxy passes its rows through."""
    return openrouter_efforts(item)


# Rough context window by model family, for when no catalogue can place an id.
#
# THE fallback table — the compaction budget and the TUI's token bar each
# carried their own, and they had already drifted: the TUI's did not know
# ``gpt-4.1`` or ``gpt-3.5``, matched only the dated Claude 4 slugs rather than
# the family, and defaulted to 200K where compaction defaulted to 128K. So the
# same model could be budgeted at one size and drawn at another.
#
# Deliberately conservative, and it stays that way: guessing small compacts a
# bit early, guessing large sails past the provider's limit and 413s mid-turn.
# This is a last resort — every caller consults the live catalogue first — so
# it is not the place to be clever. Entries are only added with a verified
# number behind them; a plausible guess here outlives the person who made it.
_FAMILY_WINDOWS: dict[str, int] = {
    "gemini": 1_000_000,
    "kimi": 262_144,
    "claude": 200_000,
    "sonnet": 200_000,
    "opus": 200_000,
    "haiku": 200_000,
    "gpt-4-turbo": 128_000,
    "gpt-4.1": 128_000,
    "gpt-4o": 128_000,
    "gpt-3.5": 16_385,
}

# Longest key first, so a specific entry always beats a broader one that
# happens to be a substring of the same id. Nothing in the table needs it
# today; it is here so that adding e.g. ``"gpt-4": 8_191`` alongside
# ``"gpt-4o"`` cannot silently start answering for the wrong model.
_FAMILY_WINDOWS_BY_SPECIFICITY: tuple[tuple[str, int], ...] = tuple(
    sorted(_FAMILY_WINDOWS.items(), key=lambda kv: len(kv[0]), reverse=True)
)


def family_context_window(model_id: str) -> int | None:
    """Best guess at ``model_id``'s window from its family name.

    ``None`` means "no idea" — never a default. The caller owns that choice,
    because the right default differs: compaction answers to a proxy that
    413s, the TUI only has to draw a bar.
    """
    m = (model_id or "").lower()
    if not m:
        return None
    for needle, window in _FAMILY_WINDOWS_BY_SPECIFICITY:
        if needle in m:
            return window
    return None


def get_context_window(model_id: str) -> int | None:
    """Look up a model's reported context_window from any cached catalog.

    Synchronous: only consults the in-memory cache (no network). Returns
    ``None`` if the model isn't in any cached catalog yet — caller falls
    back to its own heuristics. The status bar uses this to size its
    token-budget bar without baking in per-model magic numbers.

    Matching goes through :func:`_id_candidates`, which covers the LiteLLM
    dash convention (``claude-sonnet-4-5``) against OpenRouter's dot
    convention (``claude-sonnet-4.5``) — the Flowly proxy rewrites dashes to
    dots when forwarding, so a config can hold either form — and the dated
    snapshot suffix a provider pins onto an id while its catalogue lists the
    undated entry (``deepseek/deepseek-v4-flash-0731``). Mirrors
    ``normalizeModelForOpenRouter`` in
    ``flowly-app/app/api/v1/chat/completions/route.ts``.
    """
    if not model_id:
        return None
    runtime = _RUNTIME_WINDOWS.get(model_id)
    if runtime:
        return runtime
    candidates = _id_candidates(model_id)
    for models in _CACHE.values():
        for m in models:
            if m.id in candidates and m.context_window:
                return m.context_window
    return None


def set_runtime_context_window(model_id: str, tokens: int) -> None:
    """Record the context a local server runs ``model_id`` with now; it
    outranks the catalogue in :func:`get_context_window`."""
    if model_id and isinstance(tokens, int) and not isinstance(tokens, bool) and tokens > 0:
        _RUNTIME_WINDOWS[model_id] = tokens


def get_pricing(model_id: str) -> tuple[float | None, float | None] | None:
    """Look up a model's (USD per 1M input, USD per 1M output) from any cached
    catalog. Synchronous, cache-only (no network) — mirrors
    :func:`get_context_window`, including the dash/dot version normalization.

    Returns ``None`` when the model isn't in any cached catalog or reports no
    pricing (e.g. BYOK native providers whose catalog omits it) — the caller
    then shows tokens without a cost estimate.
    """
    priced = _priced_model(model_id)
    return None if priced is None else (priced.pricing_in, priced.pricing_out)


def get_cache_read_pricing(model_id: str) -> float | None:
    """USD per 1M input tokens served from the provider's prompt cache.

    Separate from :func:`get_pricing` rather than widening its tuple: the
    picker renders that pair as "$in/$out per 1M" and a third number there
    would be noise. Cost arithmetic is the only caller that needs this.

    ``None`` means the catalog did not report a cache price — NOT that
    caching is free. A caller must then bill cached tokens at the input
    price, which is what every caller did before this existed; the estimate
    stays too high rather than becoming too low.
    """
    priced = _priced_model(model_id)
    return None if priced is None else priced.pricing_cache_read


def _priced_model(model_id: str) -> Model | None:
    """First cached catalog entry for ``model_id`` that carries any price.

    Skipping price-less entries matters: the same id can appear in two cached
    catalogs (a BYOK provider list that omits pricing and the proxy list that
    has it), and answering from the silent one would drop the estimate for a
    model we can in fact price.
    """
    if not model_id:
        return None
    candidates = _id_candidates(model_id)
    for models in _CACHE.values():
        for m in models:
            if m.id in candidates and (m.pricing_in is not None or m.pricing_out is not None):
                return m
    return None


def get_vision_support(model_id: str) -> bool | None:
    """Return cached image-input support without touching the network.

    A model can appear in more than one cached provider catalog. An explicit
    ``True`` wins; otherwise ``False`` is returned only when at least one exact
    match explicitly reports no image support. Unknown/missing metadata stays
    ``None`` so stale or incomplete catalogs never reject a valid request.
    """
    if not model_id:
        return None
    candidates = _id_candidates(model_id)
    found_false = False
    for models in _CACHE.values():
        for model in models:
            if model.id not in candidates:
                continue
            if model.supports_vision is True:
                return True
            if model.supports_vision is False:
                found_false = True
    return False if found_false else None


# ── id normalization ──────────────────────────────────────────────

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


# A dated snapshot pinned onto a model id: ``-0731``, ``-20250929``, ``-0613``.
# FOUR digits minimum, deliberately. A shorter run is a version component, not
# a date, and stripping it would silently answer for a DIFFERENT model —
# ``claude-sonnet-4-5`` would degrade to ``claude-sonnet-4``. Dates are the
# only suffix a catalogue reliably omits while still describing the same
# model's window.
_DATED_SNAPSHOT_RE = _re.compile(r"-\d{4,}$")


def _without_dated_snapshot(mid: str) -> str:
    """``deepseek/deepseek-v4-flash-0731`` → ``deepseek/deepseek-v4-flash``.

    Providers pin a snapshot date onto an id while their catalogue lists the
    undated family entry. The two share a context window; only the exact id
    differs, so an exact-match lookup misses and the caller falls back to a
    guess. Returns the input unchanged when there is no dated suffix.
    """
    if not mid:
        return mid
    return _DATED_SNAPSHOT_RE.sub("", mid)


def _id_candidates(model_id: str) -> set[str]:
    """Every spelling of ``model_id`` worth trying against a cached catalogue.

    Covers the dash/dot version convention in both directions and the dated
    snapshot suffix, including their combination (``claude-sonnet-4-5-20250929``
    → ``claude-sonnet-4.5``).
    """
    if not model_id:
        return set()
    out: set[str] = set()
    for base in (model_id, _without_dated_snapshot(model_id)):
        if not base:
            continue
        out.update({base, _dash_to_dot_version(base), _dot_to_dash_version(base)})
    out.discard("")
    return out


async def warm_cache(provider_key: str) -> None:
    """Background prefetch of a provider's catalog. Swallows errors so
    a failed warm-up never crashes the app — the lookup just falls
    back to heuristics until the user opens /model and the picker
    fetches it on demand."""
    try:
        await fetch_models(provider_key)
    except Exception:
        pass


# Providers whose model id is defined by whatever the local server currently
# serves (no cloud catalogue to validate against). Kept here rather than
# importing the resolver's tuple to avoid a cycle (active_provider is sync).
_LOCAL_PROVIDER_KEYS = ("ollama", "vllm")


async def autofill_model_for_local(provider_key: str) -> str | None:
    """Ensure the configured default model is one the local endpoint serves.

    Switching to a local provider (via ``/provider``, the picker, or the
    desktop RPC) leaves ``agents.defaults.model`` untouched — so a cloud model
    id like ``anthropic/claude-haiku-4.5`` left over from the previous provider
    would 404 on the first message. This queries ``/v1/models`` and, when the
    current model isn't served, persists a served one (the CLI wizards already
    do this at setup time; this covers the switch paths).

    Returns the newly-set model id, or ``None`` when nothing changed — the
    current model is already served, the provider isn't local, or the endpoint
    has no models / is unreachable (in which case the switch still proceeds and
    the user can fix the model manually).
    """
    if provider_key not in _LOCAL_PROVIDER_KEYS:
        return None
    try:
        models = await fetch_models(provider_key, force_refresh=True)
    except Exception:
        models = []
    if not models:
        return None
    served = {m.id for m in models}
    from flowly.config.loader import load_config, save_config
    cfg = load_config()
    current = (cfg.agents.defaults.model or "").strip()
    if current in served:
        return None
    new_model = models[0].id
    cfg.agents.defaults.model = new_model
    save_config(cfg)
    return new_model


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
        vision = _vision_capability(item)
        efforts, default_effort, default_thinking = _openrouter_efforts(item)
        out.append(Model(
            id=mid,
            name=item.get("name") or mid,
            description=str(item.get("description") or "")[:140],
            context_window=int(ctx) if isinstance(ctx, int) and ctx > 0 else None,
            pricing_in=_per_million(pricing.get("prompt")),
            pricing_out=_per_million(pricing.get("completion")),
            pricing_cache_read=_per_million(pricing.get("input_cache_read")),
            tags=_openrouter_tags(item, pricing),
            supports_vision=vision,
            efforts=efforts,
            default_effort=default_effort,
            default_thinking=default_thinking,
        ))
    # Sort: free first, then alphabetical
    out.sort(key=lambda m: (0 if "free" in m.tags else 1, m.id.lower()))
    return out


def _flowly_catalog_row(item: object) -> Model | None:
    """One row of the Flowly proxy's ``/v1/models`` as a picker row (``locked``
    and ``plan:<id>`` tags for a model the account's plan does not have)."""
    if not isinstance(item, dict):
        return None
    mid = str(item.get("id") or "").strip()
    if not mid:
        return None
    pricing = item.get("pricing") or {}
    # Same defensive read as OpenRouter direct — Flowly proxy passes
    # through OR's schema, but a future proxy change could move ctx
    # into top_provider only.
    ctx = (
        item.get("context_length")
        or (item.get("top_provider") or {}).get("context_length")
    )
    tags = _openrouter_tags(item, pricing)
    # Plan-aware: the proxy marks each entry ``allowed`` for the
    # caller's tier. Greyed-out entries get a ``locked`` tag so the
    # picker can render them differently — and we sort allowed
    # entries to the top so the user's working choices come first.
    if item.get("allowed") is False:
        tags.append("locked")
        # The plan that opens it (the proxy's per-plan lists): "plan:pro".
        required = str(item.get("required_plan") or "").strip().lower()
        if required in ("starter", "pro", "business"):
            tags.append(f"plan:{required}")
    if pricing.get("prompt") in (None, "", "0") and pricing.get("completion") in (None, "", "0"):
        if "free" not in tags:
            tags.append("free")
    vision = _vision_capability(item)
    efforts, default_effort, default_thinking = _openrouter_efforts(item)
    return Model(
        id=mid,
        name=item.get("name") or mid,
        description=str(item.get("description") or "")[:140],
        context_window=int(ctx) if isinstance(ctx, int) else None,
        pricing_in=_per_million(pricing.get("prompt")),
        pricing_out=_per_million(pricing.get("completion")),
        pricing_cache_read=_per_million(pricing.get("input_cache_read")),
        tags=tags,
        supports_vision=vision,
        efforts=efforts,
        default_effort=default_effort,
        default_thinking=default_thinking,
    )


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
    global _FLOWLY_POLICY
    _FLOWLY_POLICY = None

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
    payload = r.json()
    if not isinstance(payload, dict):
        return []
    raw_plan = payload.get("plan")
    raw_default = payload.get("default_model")
    if isinstance(raw_plan, str) and raw_plan.strip() and isinstance(raw_default, str) and raw_default.strip():
        _FLOWLY_POLICY = FlowlyModelPolicy(
            plan=raw_plan.strip(),
            default_model=raw_default.strip(),
        )

    out: list[Model] = []
    for item in payload.get("data", []):
        row = _flowly_catalog_row(item)
        if row is not None:
            out.append(row)
    # Allowed first, then alphabetical within each bucket.
    out.sort(key=lambda m: (1 if "locked" in m.tags else 0, m.id.lower()))
    return out


def _catalog_contains(models: list[Model], model_id: str) -> bool:
    """True when an allowed catalog row matches ``model_id`` (dash/dot aliases)."""
    candidates = {
        model_id,
        _dash_to_dot_version(model_id),
        _dot_to_dash_version(model_id),
    }
    return any("locked" not in model.tags and model.id in candidates for model in models)


# The ONLY values reconcile may rewrite without the user asking. These are old
# *schema defaults* a config can still carry from before the account catalog
# existed — not choices anyone made. Everything else is a user's explicit
# selection, and validating it against the catalog is not safe grounds to
# replace it: the TUI runs this against a possibly-stale cached catalog at
# boot, so a freshly released model that's absent from yesterday's cache would
# otherwise be silently reverted to the backend default on every restart.
_LEGACY_SCHEMA_DEFAULTS = frozenset({"moonshotai/kimi-k2.5"})


def _is_legacy_schema_default(model_id: str) -> bool:
    m = model_id.strip().lower()
    return any(
        cand in _LEGACY_SCHEMA_DEFAULTS
        for cand in (m, _dash_to_dot_version(m), _dot_to_dash_version(m))
    )


async def reconcile_flowly_model(*, force_refresh: bool = True) -> str | None:
    """Migrate a stale legacy default to the account catalog's default model.

    Scope is deliberately narrow: only an old schema default (or an empty
    model) is ever replaced, and only when the authenticated catalog confirms
    it's unavailable while the backend default is. A model the user actually
    chose is NEVER rewritten here — if their plan can't serve it, the request
    itself will say so, which is honest; a background rewrite that keeps
    snapping the config back to the default is not. Network/auth/catalog
    failures are fail-safe no-ops.
    """
    models = await fetch_models("flowly", force_refresh=force_refresh)
    policy = _FLOWLY_POLICY
    if policy is None or not models:
        return None

    from flowly.config.loader import load_config

    current = (load_config().agents.defaults.model or "").strip()
    if current and not _is_legacy_schema_default(current):
        return None
    if current and _catalog_contains(models, current):
        return None
    if not _catalog_contains(models, policy.default_model):
        return None

    from flowly.config.loader import get_config_path
    from flowly.integrations.config_io import _atomic_write_json, _load_raw, _set_path

    raw = _load_raw()
    _set_path(raw, "agents.defaults.model", policy.default_model, merge=False)
    _atomic_write_json(get_config_path(), raw)
    return policy.default_model


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
        efforts, default_effort = xai_efforts(mid)
        out.append(Model(
            id=mid,
            name=mid,
            description=f"xAI · {item.get('owned_by') or 'xai'}",
            tags=tags,
            efforts=efforts,
            default_effort=default_effort,
        ))
    # A reasoning model that takes no level but has its non-reasoning twin in
    # this account's list: thinking can be switched off (to the twin).
    listed = {m.id for m in out}
    for row in out:
        if not row.efforts and xai_twin(row.id) in listed:
            row.efforts, row.default_effort = list(XAI_SWITCH_LEVELS), "high"
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


# The ChatGPT Codex backend exposes the same account-authenticated catalogue
# used by the Codex CLI. Keep a small fallback so a transient failure does not
# make the picker empty; the live response remains the source of truth.
_CODEX_FALLBACK_CONTEXT_WINDOW = 272_000
_CODEX_FALLBACK_MODELS: list[tuple[str, str, str, list[str]]] = [
    ("gpt-5.6-sol", "GPT-5.6-Sol", "Latest frontier agentic coding model.",
     ["reasoning", "vision"]),
    ("gpt-5.6-terra", "GPT-5.6-Terra", "Balanced agentic coding model for everyday work.",
     ["reasoning", "vision"]),
    ("gpt-5.6-luna", "GPT-5.6-Luna", "Fast and affordable agentic coding model.",
     ["reasoning", "fast", "vision"]),
    ("gpt-5.5", "GPT-5.5", "Frontier model for complex coding, research, and real-world work.",
     ["reasoning", "vision"]),
    ("gpt-5.4", "GPT-5.4", "Strong model for everyday coding.", ["reasoning", "vision"]),
    ("gpt-5.4-mini", "GPT-5.4-Mini", "Small, fast, and cost-efficient model.",
     ["reasoning", "fast", "vision"]),
]


def _static_codex_models() -> list[Model]:
    """Return fresh picker rows for the offline Codex catalogue fallback."""
    return [
        Model(
            id=mid,
            name=name,
            description=description,
            context_window=_CODEX_FALLBACK_CONTEXT_WINDOW,
            tags=list(tags),
            supports_vision="vision" in tags,
        )
        for mid, name, description, tags in _CODEX_FALLBACK_MODELS
    ]


def _codex_models_from_payload(payload: Any) -> list[Model]:
    """Map the ChatGPT Codex ``/models`` response into picker rows."""
    if not isinstance(payload, dict):
        return []
    raw_models = payload.get("models")
    if not isinstance(raw_models, list):
        # Defensive compatibility if the backend adopts the conventional
        # OpenAI ``data`` wrapper in a future response revision.
        raw_models = payload.get("data")
    if not isinstance(raw_models, list):
        return []

    ranked: list[tuple[int, str, Model]] = []
    for index, item in enumerate(raw_models):
        if not isinstance(item, dict):
            continue
        # The authenticated response is authoritative for ChatGPT OAuth.
        # ``supported_in_api`` concerns API-key access, not this provider.
        if str(item.get("visibility") or "list").lower() != "list":
            continue
        mid = str(item.get("slug") or item.get("id") or "").strip()
        if not mid:
            continue

        levels = item.get("supported_reasoning_levels")
        tags: list[str] = ["reasoning"] if isinstance(levels, list) and levels else []
        efforts = ordered_efforts(levels)
        default_effort = normalize_effort(item.get("default_reasoning_level"))
        if default_effort not in efforts:
            default_effort = "medium" if "medium" in efforts else (efforts[0] if efforts else "")
        description = str(item.get("description") or "")[:140]
        low_id = mid.lower()
        if "fast" in description.lower() or "mini" in low_id or low_id.endswith("-luna"):
            tags.append("fast")

        modalities = item.get("input_modalities")
        if isinstance(modalities, list):
            supports_vision: bool | None = any(
                "image" in str(modality).lower() for modality in modalities
            )
        elif isinstance(modalities, str):
            supports_vision = "image" in modalities.lower()
        else:
            supports_vision = None
        if supports_vision is True:
            tags.append("vision")

        raw_context = item.get("context_window") or item.get("max_context_window")
        context_window = (
            int(raw_context)
            if isinstance(raw_context, int) and not isinstance(raw_context, bool) and raw_context > 0
            else None
        )
        raw_priority = item.get("priority")
        priority = (
            int(raw_priority)
            if isinstance(raw_priority, (int, float)) and not isinstance(raw_priority, bool)
            else 1_000_000 + index
        )
        model = Model(
            id=mid,
            name=str(item.get("display_name") or item.get("name") or mid),
            description=description,
            context_window=context_window,
            tags=tags,
            supports_vision=supports_vision,
            efforts=efforts,
            default_effort=default_effort or None,
            service_tiers=parse_service_tiers(item.get("service_tiers")),
        )
        ranked.append((priority, low_id, model))

    ranked.sort(key=lambda entry: (entry[0], entry[1]))
    return [model for _, _, model in ranked]


def _codex_cache_path():
    from flowly.profile import get_flowly_home
    return get_flowly_home() / "codex_models_cache.json"


def _codex_account_tag(account_id: str) -> str:
    """The account a cached catalogue belongs to, as a digest (never the id)."""
    import hashlib
    return hashlib.sha256(account_id.encode("utf-8")).hexdigest()


def _write_codex_cache(account_id: str, payload: Any) -> None:
    """Keep the last live catalogue this account got, for when the backend
    cannot be reached: its models' levels and tiers then still show."""
    import json
    try:
        path = _codex_cache_path()
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"account": _codex_account_tag(account_id), "payload": payload}), encoding="utf-8")
        tmp.replace(path)
    except Exception:
        pass


def _cached_codex_models(account_id: str) -> list[Model]:
    """The last live catalogue of this account (none for another account)."""
    import json
    try:
        kept = json.loads(_codex_cache_path().read_text(encoding="utf-8"))
    except Exception:
        return []
    if not isinstance(kept, dict) or kept.get("account") != _codex_account_tag(account_id):
        return []
    return _codex_models_from_payload(kept.get("payload"))


async def _fetch_openai_codex() -> list[Model]:
    """Fetch the signed-in account's live ChatGPT Codex model catalogue.

    ``client_version`` is required by this endpoint. A 401 refreshes OAuth
    once. A live answer is kept on disk for this account; a network or
    schema failure serves that, then the curated fallback (which knows no
    levels).
    """
    from flowly import __version__
    from flowly.auth.openai_codex import resolve_runtime_credentials

    try:
        creds = await asyncio.to_thread(resolve_runtime_credentials)
    except Exception:
        return []
    if creds is None or not creds.api_key or not creds.account_id:
        return []

    for attempt in range(2):
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                response = await client.get(
                    f"{creds.base_url.rstrip('/')}/models",
                    params={"client_version": __version__},
                    headers={
                        "Authorization": f"Bearer {creds.api_key}",
                        "ChatGPT-Account-Id": creds.account_id,
                        "Accept": "application/json",
                        "User-Agent": _UA,
                        "originator": "flowly",
                    },
                )
            if response.status_code == 401 and attempt == 0:
                creds = await asyncio.to_thread(resolve_runtime_credentials, force_refresh=True)
                if creds is None or not creds.api_key or not creds.account_id:
                    return _static_codex_models()
                continue
            response.raise_for_status()
            payload = response.json()
            models = _codex_models_from_payload(payload)
            if models:
                _write_codex_cache(creds.account_id, payload)
                return models
            return _cached_codex_models(creds.account_id) or _static_codex_models()
        except Exception:
            return _cached_codex_models(creds.account_id) or _static_codex_models()
    return _cached_codex_models(creds.account_id) or _static_codex_models()


_ZAI_CODING_MODELS: list[tuple[str, str, int, list[str]]] = [
    ("glm-5.2", "GLM-5.2 — coding plan flagship", 1_000_000, ["coding", "reasoning"]),
    ("glm-5-turbo", "GLM-5 Turbo — fast coding", 1_000_000, ["coding", "fast"]),
    ("glm-4.7", "GLM-4.7 — efficient coding", 200_000, ["coding", "efficient"]),
]


async def _fetch_zai_coding() -> list[Model]:
    """Z.AI GLM Coding Plan models.

    The plan endpoint is a subscription surface and may not expose a public
    catalogue, so we return the officially supported coding-plan models only
    when a Flowly/OpenCode/env credential is available.
    """
    from flowly.auth.zai_coding import load_token_payload

    payload = await asyncio.to_thread(load_token_payload)
    if payload is None or not payload.api_key:
        return []
    return [
        Model(id=mid, name=mid, description=desc, context_window=ctx, tags=list(tags))
        for mid, desc, ctx, tags in _ZAI_CODING_MODELS
    ]


async def _fetch_ollama() -> list[Model]:
    """Local Ollama models, from its native API (flowly/integrations/local_models.py).

    Embedding models are left out (they cannot hold a conversation), and each
    model's ``context_window`` is the context Ollama actually runs it with
    when that is known: compaction then keeps the conversation inside it
    instead of the model's trained window, which Ollama would silently cut.
    An Ollama reached only through its OpenAI-compatible route (a proxy in
    front of it) still lists its models, from ``/v1/models``, without them.
    """
    from flowly.config.loader import load_config
    from flowly.integrations import local_models

    cfg = load_config()
    ocfg = getattr(cfg.providers, "ollama", None)
    base = (
        str(getattr(ocfg, "api_base", "") or "http://127.0.0.1:11434/v1")
        .strip()
        .rstrip("/")
    )
    api_key = str(getattr(ocfg, "api_key", "") or "ollama").strip() or "ollama"
    root = base[: -len("/v1")] if base.endswith("/v1") else base
    server = local_models.LocalServer("ollama", "Ollama", "ollama", base, root)
    found: list[local_models.LocalModel] = []
    async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
        try:
            found = await local_models._scan_ollama(c, server)
        except Exception:  # noqa: BLE001 - the native API is optional; the list below is not
            found = []
        if found:
            out = [
                Model(
                    id=m.id,
                    name=m.id,
                    description="Ollama · " + ("cloud" if m.cloud else (m.parameters or "local")),
                    context_window=m.context,
                    tags=["cloud" if m.cloud else "local"] + (["tools"] if m.tools else []),
                )
                for m in found
            ]
        else:
            r = await c.get(
                f"{base}/models",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Accept": "application/json",
                    "User-Agent": _UA,
                },
            )
            r.raise_for_status()
            out = []
            for item in r.json().get("data", []):
                mid = str(item.get("id") or item.get("model") or "").strip()
                if not mid:
                    continue
                out.append(Model(
                    id=mid,
                    name=mid,
                    description=f"Ollama · {item.get('owned_by') or 'local'}",
                    tags=["cloud" if mid.endswith(":cloud") else "local"],
                ))
    out.sort(key=lambda m: (1 if "cloud" in m.tags else 0, m.id.lower()))
    return out


async def _fetch_vllm() -> list[Model]:
    """Generic local OpenAI-compatible models from ``{api_base}/models`` — LM
    Studio, vLLM, llama.cpp server, text-generation-webui, koboldcpp, etc.

    Unlike Ollama there's no default port to fall back to across those
    backends, so a missing ``api_base`` returns an empty catalog (the caller
    shows a "not configured" hint) rather than guessing a URL.
    """
    from flowly.config.loader import load_config
    cfg = load_config()
    vcfg = getattr(cfg.providers, "vllm", None)
    base = str(getattr(vcfg, "api_base", "") or "").strip().rstrip("/")
    if not base:
        return []
    api_key = str(getattr(vcfg, "api_key", "") or "local").strip() or "local"
    # LM Studio says more about each model through its native API: which are
    # embedding models (left out) and the context a loaded one runs with.
    # Other servers answer 404 there and keep the plain list.
    from flowly.integrations import local_models

    root = base[: -len("/v1")] if base.endswith("/v1") else base
    details: dict[str, local_models.LocalModel] = {}
    skip: set[str] = set()
    async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
        try:
            native = await c.get(f"{root}/api/v1/models", headers={"Accept": "application/json"})
            if native.status_code == 200:
                for item in native.json().get("models") or []:
                    if not isinstance(item, dict) or not isinstance(item.get("key"), str):
                        continue
                    model = local_models._lmstudio_v1_model(item)
                    if model is None:
                        skip.add(item["key"])  # embedding and decision models
                    else:
                        details[model.id] = model
            else:
                native = await c.get(f"{root}/api/v0/models", headers={"Accept": "application/json"})
                if native.status_code == 200:
                    for item in native.json().get("data") or []:
                        if not isinstance(item, dict):
                            continue
                        if item.get("type") == "embeddings" and isinstance(item.get("id"), str):
                            skip.add(item["id"])
                        model = local_models._lmstudio_model(item)
                        if model is not None:
                            details[model.id] = model
        except Exception:  # noqa: BLE001 - only LM Studio has it; the plain list below is enough
            pass
        r = await c.get(
            f"{base}/models",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Accept": "application/json",
                "User-Agent": _UA,
            },
        )
    r.raise_for_status()
    out: list[Model] = []
    for item in r.json().get("data", []):
        mid = str(item.get("id") or item.get("model") or "").strip()
        if not mid or mid in skip:
            continue
        known = details.get(mid)
        out.append(Model(
            id=mid,
            name=mid,
            description=f"Local · {item.get('owned_by') or 'self-hosted'}",
            context_window=known.context if known else None,
            tags=["local"] + (["tools"] if known and known.tools else []),
        ))
    out.sort(key=lambda m: m.id.lower())
    return out


_FETCHERS: dict[str, "Any"] = {
    "openrouter": _fetch_openrouter,
    "flowly": _fetch_flowly_hosted,
    "xai_oauth": _fetch_xai_oauth,
    "xai": _fetch_xai_apikey,
    "openai_codex": _fetch_openai_codex,
    "zai_coding": _fetch_zai_coding,
    "ollama": _fetch_ollama,
    "vllm": _fetch_vllm,
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
    if _vision_capability(item) is True:
        tags.append("vision")
    return tags


def _vision_capability(item: dict[str, Any]) -> bool | None:
    """Read explicit OpenRouter/Flowly architecture image modalities."""
    arch = item.get("architecture")
    if not isinstance(arch, dict):
        return None
    if "input_modalities" in arch:
        modalities = arch.get("input_modalities")
    elif "modality" in arch:
        modalities = arch.get("modality")
    else:
        return None
    if isinstance(modalities, list):
        return any("image" in str(modality).lower() for modality in modalities)
    if isinstance(modalities, str):
        return "image" in modalities.lower()
    return None
