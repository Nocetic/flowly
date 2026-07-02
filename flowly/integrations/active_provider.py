"""Single source of truth for "which LLM provider will the next request use?".

The answer threads through three places — gateway boot, the integrations
catalog (to paint a ``★ default`` badge), and the setup modal (to call
out the current selection). Keeping the logic here means there's exactly
one priority order to reason about.

Priority (first match wins)
---------------------------
1. **Explicit default** — ``providers.active`` names a provider AND that
   provider is *usable* (has credentials / signed-in account). This is
   one global choice that the user sets via the UI or ``/provider``
   slash command, not inferred from credential presence.
2. **Flowly hosted (cascade)** — ``providers.flowly.enabled`` is True
   AND the user is signed in. Used when ``providers.active`` is empty.
3. **External cascade** — openrouter → anthropic → openai → xai →
   xai_oauth → gemini → groq → zhipu → vllm. First usable provider wins.

The explicit ``active`` choice is sticky: it survives until the user
changes it (or until that provider becomes unusable, e.g. signs out of
Flowly). When the named provider can't be used right now, we fall
through the cascade and surface a warning at boot rather than crashing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from flowly.config.schema import Config

logger = logging.getLogger(__name__)

_BYOK_PRIORITY = (
    "openrouter",
    "anthropic",
    "openai",
    "openai_codex",
    "xai",
    "xai_oauth",
    "gemini",
    "groq",
    "zhipu",
    "sakana",
    "vllm",
)


@dataclass(frozen=True)
class ActiveProvider:
    """Resolved provider choice for the next LLM request.

    ``key`` is the registry/config key (e.g. ``"flowly"``, ``"anthropic"``).
    ``api_key`` and ``api_base`` are what to pass into the runtime provider.
    ``source`` describes WHY this won the priority cascade so the UI can
    explain itself (e.g. "Flowly account signed in, hosted enabled").
    """
    key: str
    api_key: str
    api_base: str | None
    source: str
    # Extra credential some providers need beyond the bearer. Currently only
    # ``openai_codex`` uses it (the ``ChatGPT-Account-Id`` header value).
    account_id: str = ""


def resolve_active_provider(config: Config) -> ActiveProvider | None:
    """Return the provider that will serve the next LLM request.

    Returns ``None`` if no provider is usable — the caller should surface
    this as "configure a provider in /integrations".
    """
    # 1. Explicit default — sticky until the user changes it.
    # If the named provider is no longer usable
    # (e.g. they signed out of Flowly), we transparently fall through
    # to the cascade rather than crashing.
    active_key = (config.providers.active or "").strip()
    if active_key:
        explicit = _build_for(config, active_key)
        if explicit is not None:
            return explicit
        # else: fall through to cascade and let the caller see the
        # cascade-resolved provider. Logging is the caller's job.

    # 2. Flowly hosted (cascade) — gated on both the toggle and an account.
    flowly = _build_for(config, "flowly")
    if flowly is not None:
        return flowly

    # 3. External cascade.
    for name in _BYOK_PRIORITY:
        candidate = _build_for(config, name)
        if candidate is not None:
            return candidate

    return None


def _build_for(config: Config, name: str) -> ActiveProvider | None:
    """Construct an ``ActiveProvider`` for ``name``, or ``None`` if not usable.

    Lets ``resolve_active_provider`` share one resolver between the explicit
    pick and the cascade so they can't drift apart. ``None`` means "this
    provider isn't ready to serve a request right now" — empty key,
    missing account, etc.
    """
    if name == "flowly":
        if not config.providers.flowly.enabled:
            return None
        # Source 0 — Flowly account credential pushed by the Desktop app (the
        # only minter). Used ONLY as the LLM-proxy bearer; it does NOT touch
        # channels.web, so the bot stays a pure gateway (no relay) unless the
        # user separately enables relay reach.
        #   • account_key (flw_…) → proxy resolves the account directly, no
        #     server record. Canonical path.
        #   • server_id:auth_token → legacy/relay-registered path.
        # account_key wins when both are present.
        _fl = config.providers.flowly
        _akey = (getattr(_fl, "account_key", "") or "").strip()
        if _akey:
            return ActiveProvider(
                key="flowly",
                api_key=_akey,
                api_base="https://useflowlyapp.com/api/v1",
                source="Flowly account · Desktop",
            )
        _sid = (getattr(_fl, "server_id", "") or "").strip()
        _tok = (getattr(_fl, "auth_token", "") or "").strip()
        if _sid and _tok:
            return ActiveProvider(
                key="flowly",
                api_key=f"{_sid}:{_tok}",
                api_base="https://useflowlyapp.com/api/v1",
                source="Flowly account · Desktop",
            )
        # Flowly proxy auths on a ``serverId:gatewayAuthToken`` bearer
        # — the Firebase id_token isn't part of that auth, it only gates
        # signup / server registration / settings endpoints.
        #
        # Two places hold those credentials, depending on how the user
        # got them:
        #   1. ``~/.flowly/credentials/account.json`` (or keychain),
        #      written by the TUI ``/login`` slash command. Carries the
        #      full Account (email, id_token, refresh, server_id, …).
        #   2. ``channels.web`` in config.json, written by Desktop's
        #      pair flow (``writeRelayConfig``). Desktop never writes
        #      account.json — its Firebase tokens live in the renderer's
        #      secureStore — so Desktop-only users never had option 1.
        #
        # We try (1) first because it carries the richer source label
        # for the UI ("Flowly account (you@example.com)"), then fall
        # back to (2) so a fresh Desktop install with no CLI involvement
        # still resolves to a usable Flowly provider instead of dropping
        # into the BYOK cascade and reporting "no provider configured".
        from flowly.account.auth import load_account_sync
        account = load_account_sync()
        if (
            account is not None
            and account.server_id
            and account.gateway_auth_token
        ):
            bearer = f"{account.server_id}:{account.gateway_auth_token}"
            source = f"Flowly account ({account.email or account.user_id})"
        else:
            web = config.channels.web
            if not (web.enabled and web.server_id and web.auth_token):
                return None
            bearer = f"{web.server_id}:{web.auth_token}"
            source = "Flowly server (Desktop pair)"
        # api_base is HARDCODED — picking Flowly always means the
        # canonical proxy URL. No saved override is consulted, same
        # rule as BYOK providers below.
        return ActiveProvider(
            key="flowly",
            api_key=bearer,
            api_base="https://useflowlyapp.com/api/v1",
            source=source,
        )
    if name == "openai_codex":
        codex_cfg = getattr(config.providers, "openai_codex", None)
        if codex_cfg is not None and not getattr(codex_cfg, "enabled", True):
            return None
        try:
            from flowly.auth.openai_codex import resolve_runtime_credentials
            creds = resolve_runtime_credentials(config=config)
        except Exception as exc:
            logger.debug("openai_codex provider unavailable: %s", exc)
            return None
        if creds is None or not creds.api_key or not creds.account_id:
            return None
        plan = f" · {creds.plan}" if creds.plan else ""
        email = f" ({creds.email})" if creds.email else ""
        return ActiveProvider(
            key="openai_codex",
            api_key=creds.api_key,
            api_base=creds.base_url,
            source=f"ChatGPT subscription{email}{plan}",
            account_id=creds.account_id,
        )

    if name == "xai_oauth":
        oauth_cfg = getattr(config.providers, "xai_oauth", None)
        if oauth_cfg is not None and not getattr(oauth_cfg, "enabled", True):
            return None
        try:
            from flowly.auth.xai_oauth import (
                DEFAULT_XAI_OAUTH_BASE_URL,
                resolve_runtime_credentials,
                validate_xai_oauth_base_url,
            )
            creds = resolve_runtime_credentials(config=config)
        except Exception as exc:
            logger.debug("xai_oauth provider unavailable: %s", exc)
            return None
        if creds is None or not creds.api_key:
            return None
        configured_base = getattr(oauth_cfg, "api_base", "") if oauth_cfg is not None else ""
        base_url = validate_xai_oauth_base_url(configured_base or creds.base_url or DEFAULT_XAI_OAUTH_BASE_URL)
        email = f" ({creds.email})" if creds.email else ""
        return ActiveProvider(
            key="xai_oauth",
            api_key=creds.api_key,
            api_base=base_url,
            source=f"xAI Grok OAuth{email}",
        )

    if name not in _BYOK_PRIORITY:
        return None
    provider_cfg = getattr(config.providers, name, None)
    if provider_cfg is None:
        return None
    # Strip whitespace — users routinely paste keys with leading/trailing
    # space from terminal copy-paste, and an OpenAI SDK ``Bearer  sk-xxx``
    # request gets rejected as malformed before the key is even checked
    # against the provider's records.
    key = (getattr(provider_cfg, "api_key", "") or "").strip()
    if not key:
        return None
    # api_base: saved value wins, fall back to canonical default.
    #
    # Why "saved wins": legacy Desktop installs wrote the Flowly Cloud
    # proxy URL into ``providers.openrouter.apiBase`` along with a
    # ``serverId:authToken`` bearer in ``apiKey``. If we ignored the
    # saved value (an earlier draft of this resolver did), those users'
    # bots would route to ``openrouter.ai`` with a Flowly bearer and
    # 401 on every request — a silent break for everyone upgrading from
    # the pre-BYOK Desktop. By respecting the saved value we keep
    # legacy installs working until they re-pair or run setup again,
    # at which point the canonical URL gets written back.
    #
    # New installs / TUI setup wizard write the canonical URL up front,
    # so the saved value matches ``_default_base_for(name)`` and this
    # branch is a no-op. The ``_default_base_for`` fallback only kicks
    # in when the slot was created without an ``apiBase`` (e.g. raw
    # JSON edit, partial defaults).
    saved_base = (getattr(provider_cfg, "api_base", None) or "").strip()
    return ActiveProvider(
        key=name,
        api_key=key,
        api_base=saved_base or _default_base_for(name),
        source=f"BYOK · {name}",
    )


# Curated per-provider default models — applied automatically when the user
# switches to a provider whose API can't serve the currently-configured model
# (e.g. Anthropic BYOK can't serve "moonshotai/kimi-k2.5"). Picking a safe
# default + notifying beats failing the user's first message.
DEFAULT_MODELS: dict[str, str] = {
    "flowly": "anthropic/claude-haiku-4.5",   # proxy default, in every plan
    "openrouter": "anthropic/claude-haiku-4.5",
    "anthropic": "claude-haiku-4-5",
    "openai": "gpt-4o-mini",
    "gemini": "gemini-2.5-flash",             # present in the live models.dev catalogue
    "groq": "llama-3.3-70b-versatile",
    "zhipu": "glm-4.6",
    "sakana": "fugu",                         # Fugu orchestrator (also: fugu-ultra)
    "xai": "grok-4.3",                        # matches model_catalog._XAI_TOP_MODEL
    "xai_oauth": "grok-4.20-reasoning",       # matches DEFAULT_XAI_RESPONSES_MODEL
    "openai_codex": "gpt-5.5",                # matches DEFAULT_CODEX_MODEL
}

# Cheap offline "does this model plausibly belong to this provider?" check —
# accepted model-id prefixes per provider. Aggregators (flowly/openrouter)
# accept any vendor-prefixed id; vllm is self-hosted (anything goes); an
# unknown provider is left untouched. Heuristic on purpose: a false
# "doesn't fit" just lands on a working curated default.
_MODEL_PREFIX_HINTS: dict[str, tuple[str, ...]] = {
    "anthropic": ("claude",),
    "openai": ("gpt", "o1", "o3", "o4", "chatgpt"),
    "gemini": ("gemini", "models/gemini"),
    # NB: no "moonshotai/" — Groq serves kimi-k2-instruct but NOT the schema
    # default "moonshotai/kimi-k2.5"; a false "fits" would fail the user's
    # first message, while a false "doesn't fit" just lands on llama + a note.
    "groq": ("llama", "meta-llama/", "mixtral", "gemma", "qwen", "deepseek", "openai/gpt-oss"),
    "zhipu": ("glm",),
    "sakana": ("fugu",),
    "xai": ("grok",),
    "xai_oauth": ("grok",),
    # ChatGPT subscription serves the current-generation general GPT-5.x
    # models (gpt-5.4 / gpt-5.5 families) — not codex-suffixed or older ids.
    "openai_codex": ("gpt-5.4", "gpt-5.5"),
}


def model_fits_provider(model: str, key: str) -> bool:
    """True when ``model`` can plausibly be served by provider ``key``.

    Exact when possible: the cached models.dev catalogue answers "does this
    provider serve this id?" definitively (offline, never blocks). Only when
    no catalogue data is cached do we fall back to the prefix heuristics.
    """
    model = (model or "").strip().lower()
    if not model:
        return False
    if key == "flowly":
        # Account proxy — plan-aware, not in models.dev. Vendor-prefixed ids.
        return "/" in model
    try:
        from flowly.integrations.models_dev import model_known
        known = model_known(key, model)
        if known is not None:
            return known
    except Exception:  # noqa: BLE001
        pass
    if key == "openrouter":
        # Aggregator — vendor-prefixed ids ("vendor/model").
        return "/" in model
    hints = _MODEL_PREFIX_HINTS.get(key)
    if hints is None:
        return True  # vllm / unknown — don't second-guess
    return model.startswith(hints)


def set_active_provider(key: str) -> str | None:
    """Persist ``providers.active`` atomically. Empty string = clear.

    Provider switches are non-destructive: neither ``apiKey`` nor
    ``apiBase`` of the target slot is touched. The setup wizard writes
    the canonical ``apiBase`` (e.g. ``https://openrouter.ai/api/v1``
    for OpenRouter) so the on-disk value is meaningful when the user
    inspects the file. The resolver still uses :func:`_default_base_for`
    as the runtime source of truth — saved ``apiBase`` is informational,
    not authoritative.

    When the currently-configured default model can't be served by the new
    provider, it is switched to the provider's curated default in the same
    write (otherwise the user's very first message after switching fails).
    Returns the auto-applied model id, or ``None`` when the model was left
    alone — callers surface it as "model → X".
    """
    if key and key != "flowly" and key not in _BYOK_PRIORITY:
        raise ValueError(f"unknown provider key: {key}")
    from flowly.integrations.config_io import _atomic_write_json, _load_raw, _set_path
    raw = _load_raw()
    _set_path(raw, "providers.active", key, merge=False)

    model_changed: str | None = None
    if key:
        # Only rewrite the model if the target provider can ACTUALLY serve the
        # next request. If it isn't usable yet (no key / no account),
        # resolve_active_provider falls through to a DIFFERENT provider in the
        # cascade — and rewriting the model to the target's bare default (e.g.
        # "claude-haiku-4-5") would then 404 on that other provider on the user's
        # very first message. Target usability is independent of providers.active,
        # so the on-disk config (which already holds the target's credential) is
        # the right thing to check.
        from flowly.config.loader import load_config
        try:
            target_usable = _build_for(load_config(), key) is not None
        except Exception:
            target_usable = False
        current = str(
            ((raw.get("agents") or {}).get("defaults") or {}).get("model") or ""
        )
        fallback = DEFAULT_MODELS.get(key)
        # No-op guard: if the current model IS the curated fallback, keep it
        # without rewriting (avoids a repeating "model → X" notification when
        # the catalogue doesn't list the curated alias verbatim).
        if (
            target_usable
            and fallback
            and current.strip().lower() != fallback.lower()
            and not model_fits_provider(current, key)
        ):
            _set_path(raw, "agents.defaults.model", fallback, merge=False)
            model_changed = fallback

    from flowly.config.loader import get_config_path
    _atomic_write_json(get_config_path(), raw)
    return model_changed


def clear_active_if_matches(key: str) -> bool:
    """If ``providers.active == key``, clear it. Returns True if cleared.

    Used by sign-out / Disconnect handlers so the default doesn't dangle
    on a provider that can no longer serve requests."""
    from flowly.config.loader import load_config
    try:
        current = (load_config().providers.active or "").strip()
    except Exception:
        return False
    if current != key:
        return False
    set_active_provider("")
    return True


def _default_base_for(provider_name: str) -> str | None:
    """Per-provider canonical ``api_base`` — the **single source of truth**.

    Picking ``/provider <name>`` always uses this URL; the per-slot
    ``api_base`` field is no longer exposed in the setup form (so users
    can't accidentally override it with a wrong host).

    Returning ``None`` means "let the OpenAI SDK pick its own default".
    Used only for ``openai`` (SDK already targets api.openai.com) and
    ``vllm`` (self-hosted; runtime would fail loudly without a URL,
    but vllm is exotic enough that we punt on the form for now).

    Most endpoints listed here speak the OpenAI Chat-Completions wire
    protocol and are handled by :class:`OpenRouterProvider`. Anthropic is
    the exception: direct Anthropic BYOK uses the native Messages API via
    :class:`flowly.providers.anthropic_provider.AnthropicProvider`.
    """
    return {
        "openrouter": "https://openrouter.ai/api/v1",
        "anthropic":  "https://api.anthropic.com/v1",
        "openai":     "https://api.openai.com/v1",
        # ChatGPT subscription (Codex OAuth) — the backend base; the provider
        # posts to <base>/responses. Not the metered api.openai.com.
        "openai_codex": "https://chatgpt.com/backend-api/codex",
        "xai":        "https://api.x.ai/v1",
        "xai_oauth":  "https://api.x.ai/v1",
        "groq":       "https://api.groq.com/openai/v1",
        "gemini":     "https://generativelanguage.googleapis.com/v1beta/openai",
        "zhipu":      "https://open.bigmodel.cn/api/paas/v4",
        "sakana":     "https://api.sakana.ai/v1",
        # vllm is self-hosted; user must set api_base manually if they
        # use this slot. Returning None means resolver still works
        # (it just hands None to the SDK, which then uses its default).
    }.get(provider_name)
