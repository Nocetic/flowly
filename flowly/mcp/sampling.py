"""MCP sampling — server-initiated LLM requests (Faz 3d, P1/P2).

A few MCP servers ask the *client's* LLM to do work mid-tool via
``sampling/createMessage`` (summarize, classify, etc.). When a server has
``sampling.enabled`` in its config, we install a callback on its
``ClientSession`` that routes the request through Flowly's provider.

Guardrails (opt-in, off by default):
- **rate limit**: sliding-window ``max_rpm`` requests/minute per server.
- **model allowlist**: if set, only those models may be requested.
- **token cap**: the server's ``maxTokens`` is clamped to ``max_tokens_cap``.

Scope: text completions (the common case). The callback runs on the MCP
event loop and awaits the async provider directly. Errors return an MCP
``ErrorData`` so the server sees a clean failure, never an exception
across the JSON-RPC boundary.
"""

from __future__ import annotations

import logging
import time
from typing import Any

logger = logging.getLogger(__name__)


_SAMPLING_TYPES = False
try:
    from mcp.types import CreateMessageResult, TextContent, ErrorData  # type: ignore
    _SAMPLING_TYPES = True
except ImportError:  # pragma: no cover
    pass


def _safe_int(value: Any, default: int, minimum: int = 0) -> int:
    try:
        return max(int(value), minimum)
    except (TypeError, ValueError):
        return default


class SamplingHandler:
    """Per-server ``sampling/createMessage`` callback for a ClientSession.

    Constructed from a server's ``sampling`` config block. Callable with
    the SDK's ``(context, params)`` signature.
    """

    def __init__(self, server_name: str, config: dict) -> None:
        self.server_name = server_name
        self.max_rpm = _safe_int(config.get("max_rpm", 10), 10, minimum=1)
        self.max_tokens_cap = _safe_int(config.get("max_tokens_cap", 4096), 4096, minimum=1)
        self.model_override = str(config.get("model") or "") or None
        self.allowed_models = [str(m) for m in (config.get("allowed_models") or [])]
        self._timestamps: list[float] = []

    # -- guards ---------------------------------------------------------

    def _rate_ok(self) -> bool:
        now = time.time()
        window = now - 60
        self._timestamps[:] = [t for t in self._timestamps if t > window]
        if len(self._timestamps) >= self.max_rpm:
            return False
        self._timestamps.append(now)
        return True

    def _resolve_model(self, params: Any) -> str | None:
        if self.model_override:
            return self.model_override
        prefs = getattr(params, "modelPreferences", None)
        hints = getattr(prefs, "hints", None) if prefs else None
        if hints:
            for hint in hints:
                name = getattr(hint, "name", None)
                if name:
                    return name
        return None

    # -- message conversion --------------------------------------------

    @staticmethod
    def _to_provider_messages(params: Any) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        system = getattr(params, "systemPrompt", None)
        if system:
            messages.append({"role": "system", "content": str(system)})
        for msg in getattr(params, "messages", []) or []:
            content = getattr(msg, "content", None)
            text = getattr(content, "text", None)
            if not text and isinstance(content, list):
                text = "\n".join(
                    getattr(b, "text", "") for b in content if getattr(b, "text", None)
                )
            messages.append({"role": getattr(msg, "role", "user"), "content": text or ""})
        return messages

    def _error(self, message: str) -> Any:
        logger.warning("MCP sampling (%s): %s", self.server_name, message)
        if _SAMPLING_TYPES:
            return ErrorData(code=-1, message=message)
        raise RuntimeError(message)

    # -- callback -------------------------------------------------------

    async def __call__(self, context: Any, params: Any) -> Any:
        if not _SAMPLING_TYPES:
            return self._error("sampling types unavailable in this SDK build")

        if not self._rate_ok():
            return self._error(
                f"sampling rate limit exceeded ({self.max_rpm}/min)"
            )

        model = self._resolve_model(params)
        if self.allowed_models and (model or "") not in self.allowed_models:
            return self._error(
                f"model {model!r} not in allowed_models {self.allowed_models}"
            )

        max_tokens = min(
            _safe_int(getattr(params, "maxTokens", self.max_tokens_cap),
                      self.max_tokens_cap, minimum=1),
            self.max_tokens_cap,
        )
        messages = self._to_provider_messages(params)

        try:
            provider = _build_provider(model)
        except Exception as exc:
            return self._error(f"no LLM provider available: {exc}")

        temperature = getattr(params, "temperature", None)
        try:
            response = await provider.chat(
                messages=messages,
                model=model or None,
                max_tokens=max_tokens,
                temperature=temperature if temperature is not None else 0.7,
            )
        except Exception as exc:
            return self._error(f"LLM call failed: {type(exc).__name__}: {exc}")

        text = getattr(response, "content", "") or ""
        return CreateMessageResult(
            role="assistant",
            content=TextContent(type="text", text=text),
            model=model or "flowly",
            stopReason="endTurn",
        )


def _build_provider(model: str | None) -> Any:
    """Build an OpenRouter provider from the active Flowly config."""
    from flowly.config.loader import load_config
    from flowly.providers.openrouter_provider import OpenRouterProvider

    config = load_config()
    api_key = config.get_api_key()
    if not api_key:
        raise RuntimeError("no API key configured")
    return OpenRouterProvider(
        api_key=api_key,
        api_base=config.get_api_base(),
        default_model=model or config.agents.defaults.model,
        fallback_keys=config.get_fallback_keys(),
        provider_name=config.get_active_provider_name(),
    )


def build_sampling_callback(server_name: str, sampling_cfg: dict) -> Any | None:
    """Return a SamplingHandler when sampling is enabled + supported, else None."""
    if not _SAMPLING_TYPES:
        return None
    if not sampling_cfg or not sampling_cfg.get("enabled"):
        return None
    return SamplingHandler(server_name, sampling_cfg)
