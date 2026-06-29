"""Web search/extract provider registry + active-provider resolution.

Providers register here from their plugin's ``register()`` entry point via
``ctx.register_web_search_provider(...)``. The web tools
(``web_search`` / ``web_extract``) ask this module for the active provider
for the capability they need.

Active selection precedence (per capability):

1. ``tools.web.search.searchBackend`` / ``extractBackend`` (per-capability
   override), else ``tools.web.search.backend`` (shared). An explicitly
   configured-but-unavailable backend is still returned so the tool can
   surface a precise "X not configured" error instead of silently routing
   elsewhere.
2. If exactly one registered provider supports the capability AND is
   available, use it.
3. Default preference order (:data:`_DEFAULT_PREFERENCE`), filtered by
   availability.
4. Otherwise ``None`` — the tool surfaces a setup hint.
"""

from __future__ import annotations

import threading
from typing import Optional

from loguru import logger

from flowly.agent.tools.web_providers.base import WebSearchProvider

_providers: dict[str, WebSearchProvider] = {}
_lock = threading.RLock()
_loaded = False

# Default order when no backend is configured. Brave is first to preserve
# Flowly's historical default — existing installs that only set a Brave key
# keep landing on Brave. Availability filtering means a provider only wins
# when the user actually has its credentials/config.
_DEFAULT_PREFERENCE = (
    "brave",
    "firecrawl",
    "parallel",
    "tavily",
    "exa",
    "searxng",
    "ddgs",
    "xai",
)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register_provider(provider: WebSearchProvider) -> None:
    """Register a web search/extract provider.

    Re-registration under the same ``name`` overwrites the previous entry
    (predictable for hot-reload / tests).
    """
    if not isinstance(provider, WebSearchProvider):
        raise TypeError(
            "register_provider() expects a WebSearchProvider instance, "
            f"got {type(provider).__name__}"
        )
    name = provider.name
    if not isinstance(name, str) or not name.strip():
        raise ValueError("Web provider .name must be a non-empty string")
    with _lock:
        existing = _providers.get(name)
        _providers[name] = provider
    if existing is not None:
        logger.debug("Web provider '{}' re-registered", name)
    else:
        logger.debug("Registered web provider '{}'", name)


def get_provider(name: str) -> Optional[WebSearchProvider]:
    """Return the provider registered under *name*, or None."""
    if not isinstance(name, str):
        return None
    with _lock:
        return _providers.get(name.strip())


def list_providers() -> list[WebSearchProvider]:
    """Return all registered providers, sorted by name."""
    with _lock:
        items = list(_providers.values())
    return sorted(items, key=lambda p: p.name)


# ---------------------------------------------------------------------------
# Plugin discovery
# ---------------------------------------------------------------------------


def _ensure_loaded() -> None:
    """Trigger plugin discovery so bundled web providers register.

    The web tools can be reached from contexts that haven't already booted
    the plugin manager (subprocess agent runs, the Codex tool server,
    standalone scripts). Without discovery the registry is empty and the
    configured backend resolves to ``None``. This is idempotent and cheap
    on repeat calls. When no plugin manager exists in the current context
    it is a no-op — the caller falls back to its own direct behaviour.
    """
    global _loaded
    if _loaded:
        return
    try:
        from flowly.plugins import get_plugin_manager

        mgr = get_plugin_manager()
    except RuntimeError:
        # Manager not initialised in this context — retry on a later call
        # (it may come up after the first web tool reference in tests).
        return
    except Exception as exc:  # pragma: no cover — defensive
        logger.debug("web provider plugin discovery unavailable: {}", exc)
        return
    try:
        mgr.discover_and_load()
        _loaded = True
    except Exception as exc:  # pragma: no cover — defensive
        logger.debug("web provider plugin discovery failed: {}", exc)


# ---------------------------------------------------------------------------
# Active-provider resolution
# ---------------------------------------------------------------------------


def _read_config_backend(capability: str) -> str:
    """Resolve the configured backend name for a capability.

    Reads ``tools.web.search.{capability}_backend`` then falls back to
    ``tools.web.search.backend``. Returns "" when unset or unreadable.
    """
    try:
        from flowly.config.loader import load_config

        search = load_config().tools.web.search
    except Exception:
        return ""
    per_cap = getattr(search, f"{capability}_backend", "") or ""
    shared = getattr(search, "backend", "") or ""
    return (per_cap or shared or "").strip()


def _capable(provider: WebSearchProvider, capability: str) -> bool:
    if capability == "search":
        return bool(provider.supports_search())
    if capability == "extract":
        return bool(provider.supports_extract())
    return False


def _is_available_safe(provider: WebSearchProvider) -> bool:
    """Wrap ``is_available()`` so a buggy provider can't kill resolution."""
    try:
        return bool(provider.is_available())
    except Exception as exc:  # noqa: BLE001
        logger.debug("provider {}.is_available() raised {}", provider.name, exc)
        return False


def _resolve(configured: str, *, capability: str) -> Optional[WebSearchProvider]:
    """Resolve the active provider for a capability ("search" | "extract")."""
    with _lock:
        snapshot = dict(_providers)

    # 1. Explicit config wins — return regardless of availability so the
    #    tool can emit a precise "not configured" error downstream.
    if configured:
        provider = snapshot.get(configured)
        if provider is not None and _capable(provider, capability):
            return provider
        if provider is None:
            logger.debug(
                "web backend '{}' configured but not registered; falling back",
                configured,
            )
        else:
            logger.debug(
                "web backend '{}' configured but lacks '{}'; falling back",
                configured,
                capability,
            )

    # 2. + 3. Availability-filtered fallback.
    eligible = [
        p
        for p in snapshot.values()
        if _capable(p, capability) and _is_available_safe(p)
    ]
    if len(eligible) == 1:
        return eligible[0]

    for name in _DEFAULT_PREFERENCE:
        provider = snapshot.get(name)
        if (
            provider is not None
            and _capable(provider, capability)
            and _is_available_safe(provider)
        ):
            return provider

    return None


def get_active_search_provider() -> Optional[WebSearchProvider]:
    """Resolve the currently-active web search provider."""
    _ensure_loaded()
    return _resolve(_read_config_backend("search"), capability="search")


def get_active_extract_provider() -> Optional[WebSearchProvider]:
    """Resolve the currently-active web extract provider."""
    _ensure_loaded()
    return _resolve(_read_config_backend("extract"), capability="extract")


def _reset_for_tests() -> None:
    """Clear the registry and discovery flag. **Test-only.**"""
    global _loaded
    with _lock:
        _providers.clear()
    _loaded = False
