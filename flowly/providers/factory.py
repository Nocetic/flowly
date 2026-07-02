"""Provider construction helpers shared by CLI entry points."""

from __future__ import annotations

from flowly.integrations.active_provider import ActiveProvider
from flowly.providers.anthropic_provider import AnthropicProvider
from flowly.providers.base import LLMProvider
from flowly.providers.codex_responses_provider import CodexResponsesProvider
from flowly.providers.openrouter_provider import OpenRouterProvider
from flowly.providers.xai_responses_provider import XAIResponsesProvider


def build_provider(
    active: ActiveProvider,
    *,
    default_model: str,
    fallback_keys: list[str] | None = None,
    config: object | None = None,
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
        return CodexResponsesProvider(
            api_key=active.api_key,
            account_id=active.account_id,
            api_base=active.api_base,
            default_model=default_model,
            provider_name=active.key,
            request_timeout_seconds=timeout_seconds,
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
