"""Embedding provider abstraction — OpenAI, Gemini, or disabled (FTS5-only)."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from loguru import logger


@dataclass
class EmbeddingProvider:
    provider: str   # "openai", "gemini", "none"
    model: str
    dims: int


# Default models per provider
_DEFAULT_MODELS = {
    "openai": "text-embedding-3-small",
    "gemini": "gemini/text-embedding-004",
}

_DEFAULT_DIMS = {
    "text-embedding-3-small": 1536,
    "text-embedding-3-large": 3072,
    "text-embedding-ada-002": 1536,
    "text-embedding-004": 768,       # Gemini
    "gemini/text-embedding-004": 768,
}


def _resolve_provider_and_model(
    provider: str,
    model: str,
    api_key: str,
    config: Any,  # flowly Config object
) -> tuple[str, str] | tuple[None, None]:
    """
    Resolve which provider+model to use.

    Returns (provider_id, model_name) or (None, None) if nothing available.
    """
    if provider == "none":
        return None, None

    if provider == "auto":
        # Try openai key first, then gemini
        candidates = []
        if api_key:
            # api_key was explicitly passed — detect provider from it
            if api_key.startswith("sk-"):
                candidates = [("openai", _DEFAULT_MODELS["openai"])]
            else:
                candidates = [("gemini", _DEFAULT_MODELS["gemini"])]
        else:
            openai_key = getattr(getattr(config, "providers", None), "openai", None)
            gemini_key = getattr(getattr(config, "providers", None), "gemini", None)
            if openai_key and getattr(openai_key, "api_key", None):
                candidates.append(("openai", _DEFAULT_MODELS["openai"]))
            if gemini_key and getattr(gemini_key, "api_key", None):
                candidates.append(("gemini", _DEFAULT_MODELS["gemini"]))

        return candidates[0] if candidates else (None, None)

    resolved_model = model or _DEFAULT_MODELS.get(provider, "")
    return provider, resolved_model


def get_embedding_dims(model: str) -> int:
    """Return known embedding dimensions for a model, default 1536."""
    return _DEFAULT_DIMS.get(model, 1536)


async def embed_texts(
    texts: list[str],
    provider: str,
    model: str,
    api_key: str = "",
    api_base: str = "",
) -> list[list[float]] | None:
    """
    Embed a list of texts.

    Supports:
      - ``openai``: native openai SDK (text-embedding-3-small, etc.)
      - ``gemini``: not yet implemented post-litellm migration, returns None.
      - ``none``: disabled, returns None.

    Returns list of float vectors, or None on failure.
    """
    if not texts or provider == "none":
        return None

    if provider == "gemini":
        logger.warning(
            "[Memory] Gemini embeddings are not implemented after the "
            "litellm migration — falling back to FTS5-only search"
        )
        return None

    if provider != "openai":
        logger.warning(f"[Memory] Unknown embedding provider '{provider}'")
        return None

    try:
        from openai import AsyncOpenAI

        client_kwargs: dict[str, Any] = {"api_key": api_key or "placeholder"}
        if api_base:
            client_kwargs["base_url"] = api_base
        client = AsyncOpenAI(**client_kwargs)

        response = await asyncio.wait_for(
            client.embeddings.create(model=model, input=texts),
            timeout=60.0,
        )
        return [item.embedding for item in response.data]

    except asyncio.TimeoutError:
        logger.warning(f"[Memory] Embedding timeout for provider={provider}")
        return None
    except Exception as e:
        logger.warning(f"[Memory] Embedding failed ({provider}/{model}): {e}")
        return None


async def embed_single(
    text: str,
    provider: str,
    model: str,
    api_key: str = "",
    api_base: str = "",
) -> list[float] | None:
    """Embed a single text string."""
    results = await embed_texts([text], provider, model, api_key, api_base)
    return results[0] if results else None
