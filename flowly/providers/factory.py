"""Provider construction helpers shared by CLI entry points."""

from __future__ import annotations

from flowly.integrations.active_provider import ActiveProvider, is_local_endpoint
from flowly.providers.anthropic_provider import AnthropicProvider
from flowly.providers.base import LLMProvider
from flowly.providers.codex_responses_provider import CodexResponsesProvider
from flowly.providers.effort import normalize_effort
from flowly.providers.openrouter_provider import OpenRouterProvider
from flowly.providers.service_tier import normalize_service_tier
from flowly.providers.xai_responses_provider import XAIResponsesProvider

# Local model servers (Ollama, LM Studio, vLLM, llama.cpp on loopback/LAN)
# routinely need longer than the cloud-tuned 120s default: prefill + decode of
# a full response on CPU / a modest GPU can run for minutes. When the user
# hasn't pinned an explicit timeout, local endpoints get this roomier default
# so long generations aren't cut off mid-stream. Not unbounded — a hung server
# should still eventually fail rather than wedge the UI forever.
_LOCAL_DEFAULT_TIMEOUT_SECONDS = 600.0

# The stock ``agents.defaults.llmTimeoutSeconds``. Read dynamically so it can't
# drift from the schema. This value is treated as "unset" (not an explicit
# user choice), so a fresh config on the stock default still lets the local
# bump above apply — the config field always carries a concrete number, so
# there's otherwise no way to tell "user picked 120" from "nobody touched it".
try:
    from flowly.config.schema import AgentDefaults as _AgentDefaults
    _STOCK_LLM_TIMEOUT = float(_AgentDefaults.model_fields["llm_timeout_seconds"].default)
except Exception:  # pragma: no cover - defensive; schema field renamed/removed
    _STOCK_LLM_TIMEOUT = 120.0


def build_provider(
    active: ActiveProvider,
    *,
    default_model: str,
    fallback_keys: list[str] | None = None,
    config: object | None = None,
) -> LLMProvider:
    provider = _construct_provider(
        active, default_model=default_model, fallback_keys=fallback_keys, config=config,
    )
    provider.reasoning_effort = configured_effort(config)
    provider.service_tier = configured_service_tier(config)
    return provider


def configured_effort(config: object | None) -> str:
    """The agent's reasoning effort (``agents.defaults.reasoningEffort``);
    empty when unset or not a known level."""
    try:
        return normalize_effort(config.agents.defaults.reasoning_effort)  # type: ignore[union-attr]
    except AttributeError:
        return ""


def configured_service_tier(config: object | None) -> str:
    """The agent's service tier (``agents.defaults.serviceTier``); empty when
    unset or not a valid tier id."""
    try:
        return normalize_service_tier(config.agents.defaults.service_tier)  # type: ignore[union-attr]
    except AttributeError:
        return ""


def _construct_provider(
    active: ActiveProvider,
    *,
    default_model: str,
    fallback_keys: list[str] | None,
    config: object | None,
) -> LLMProvider:
    # Per-request LLM timeout from config (agents.defaults.llmTimeoutSeconds),
    # set via the desktop Settings UI. None → provider falls back to the
    # FLOWLY_LLM_TIMEOUT_SECONDS env var, then its built-in default.
    timeout_seconds: float | None = None
    if config is not None:
        try:
            timeout_seconds = float(config.agents.defaults.llm_timeout_seconds)
        except (AttributeError, TypeError, ValueError):
            timeout_seconds = None

    # A local model server on the stock timeout gets the roomier local default:
    # the stock value is what every config carries, not a choice, and a local
    # model's prefill + decode routinely outlasts it. A value the owner
    # changed, or FLOWLY_LLM_TIMEOUT_SECONDS (honored inside the provider),
    # still wins. Only the local slots: a cloud provider pointed at a proxy on
    # this machine (a dev server, a LiteLLM) keeps exactly what it had.
    if (
        active.key in ("ollama", "vllm")
        and is_local_endpoint(active.api_base)
        and timeout_seconds in (None, _STOCK_LLM_TIMEOUT)
    ):
        timeout_seconds = _LOCAL_DEFAULT_TIMEOUT_SECONDS

    if active.key == "xai_oauth":
        providers = getattr(config, "providers", None) if config is not None else None
        oauth_cfg = getattr(providers, "xai_oauth", None) if providers is not None else None
        client_id = str(getattr(oauth_cfg, "client_id", "") or "")
        return XAIResponsesProvider(
            api_key=active.api_key,
            api_base=active.api_base,
            default_model=default_model,
            provider_name=active.key,
            client_id=client_id,
            request_timeout_seconds=timeout_seconds,
        )
    if active.key == "openai_codex":
        providers = getattr(config, "providers", None) if config is not None else None
        codex_cfg = (
            getattr(providers, "openai_codex", None)
            if providers is not None
            else None
        )
        return CodexResponsesProvider(
            api_key=active.api_key,
            account_id=active.account_id,
            api_base=active.api_base,
            default_model=default_model,
            provider_name=active.key,
            request_timeout_seconds=timeout_seconds,
            native_compaction=bool(
                getattr(codex_cfg, "native_compaction", True)
            ),
            native_compaction_threshold=int(
                getattr(codex_cfg, "native_compaction_threshold", 200_000)
            ),
        )
    if active.key == "anthropic":
        return AnthropicProvider(
            api_key=active.api_key,
            api_base=active.api_base,
            default_model=default_model,
            fallback_keys=fallback_keys,
            provider_name=active.key,
            request_timeout_seconds=timeout_seconds,
        )
    return OpenRouterProvider(
        api_key=active.api_key,
        api_base=active.api_base,
        default_model=default_model,
        fallback_keys=fallback_keys,
        provider_name=active.key,
        request_timeout_seconds=timeout_seconds,
    )


def build_named_provider(
    config: object,
    provider_name: str,
    *,
    default_model: str,
) -> LLMProvider | None:
    """Build one configured provider by name, or ``None`` when unavailable."""
    from flowly.integrations.active_provider import resolve_named_provider

    active = resolve_named_provider(config, provider_name)  # type: ignore[arg-type]
    if active is None:
        return None
    provider_cfg = getattr(getattr(config, "providers", None), active.key, None)
    fallback_keys = list(getattr(provider_cfg, "fallback_keys", []) or [])
    return build_provider(
        active,
        default_model=default_model,
        fallback_keys=fallback_keys,
        config=config,
    )
