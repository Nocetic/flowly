"""Base LLM provider interface."""

from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, AsyncIterator


@dataclass
class ToolCallRequest:
    """A tool call request from the LLM."""
    id: str
    name: str
    arguments: dict[str, Any]
    # Provider-specific metadata that must round-trip back on the next request.
    # Gemini thinking models attach ``{"google": {"thought_signature": ...}}``
    # here (the OpenAI-compat ``extra_content`` on each tool call); the API
    # rejects a follow-up turn with HTTP 400 if it isn't replayed. None for
    # providers that don't use it. See flowly/providers/openrouter_provider.py.
    extra_content: dict[str, Any] | None = None


@dataclass(frozen=True)
class LLMErrorInfo:
    """Provider error signals that are safe to route on programmatically.

    Raw provider payloads remain in server logs. Only stable status/type/code
    fields cross the provider boundary so retry and compaction policy never
    has to infer a 413 or 429 from user-facing text when the SDK exposed it.
    """

    status_code: int | None = None
    code: str | None = None
    type: str | None = None
    retry_after_seconds: float | None = None


class ProviderHTTPError(RuntimeError):
    """HTTP failure with structured metadata for non-SDK providers."""

    def __init__(
        self,
        provider: str,
        status_code: int,
        detail: str,
        *,
        code: str | None = None,
        error_type: str | None = None,
        retry_after_seconds: float | None = None,
    ) -> None:
        super().__init__(f"{provider} HTTP {status_code}: {detail}")
        mapping = _error_mapping(detail)
        self.status_code = status_code
        self.code = code or mapping.get("code")
        self.type = error_type or mapping.get("type")
        self.retry_after_seconds = retry_after_seconds


def _error_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        nested = value.get("error")
        return nested if isinstance(nested, dict) else value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
        return _error_mapping(parsed)
    return {}


def error_info_from_exception(exc: Exception) -> LLMErrorInfo | None:
    """Extract stable routing metadata from SDK or direct-HTTP exceptions."""

    response = getattr(exc, "response", None)
    status = getattr(exc, "status_code", None)
    if status is None and response is not None:
        status = getattr(response, "status_code", None)
    try:
        status_code = int(status) if status is not None else None
    except (TypeError, ValueError):
        status_code = None
    if status_code is None:
        match = re.search(r"\bHTTP\s+(\d{3})\b", str(exc), re.IGNORECASE)
        if match:
            status_code = int(match.group(1))

    body = getattr(exc, "body", None)
    mapping = _error_mapping(body)
    code = getattr(exc, "code", None) or mapping.get("code")
    error_type = getattr(exc, "type", None) or mapping.get("type")

    retry_after = getattr(exc, "retry_after_seconds", None)
    headers = getattr(response, "headers", None)
    if retry_after is None and headers is not None:
        try:
            retry_after = headers.get("retry-after") or headers.get("Retry-After")
        except Exception:
            retry_after = None
    try:
        retry_after_seconds = float(retry_after) if retry_after is not None else None
    except (TypeError, ValueError):
        retry_after_seconds = None

    info = LLMErrorInfo(
        status_code=status_code,
        code=str(code) if code is not None else None,
        type=str(error_type) if error_type is not None else None,
        retry_after_seconds=retry_after_seconds,
    )
    if all(value is None for value in (
        info.status_code,
        info.code,
        info.type,
        info.retry_after_seconds,
    )):
        return None
    return info


@dataclass
class LLMResponse:
    """Response from an LLM provider."""
    content: str | None
    tool_calls: list[ToolCallRequest] = field(default_factory=list)
    finish_reason: str = "stop"
    usage: dict[str, int] = field(default_factory=dict)
    error_info: LLMErrorInfo | None = None
    # True when text from this attempt already reached a live stream callback.
    # Retrying an error after that point would duplicate or contradict visible
    # output, so context recovery must fail closed and preserve the partial.
    partial_content_delivered: bool = False

    @property
    def has_tool_calls(self) -> bool:
        """Check if response contains tool calls."""
        return len(self.tool_calls) > 0


class LLMProvider(ABC):
    """
    Abstract base class for LLM providers.

    Implementations should handle the specifics of each provider's API
    while maintaining a consistent interface.
    """

    def __init__(self, api_key: str | None = None, api_base: str | None = None):
        self.api_key = api_key
        self.api_base = api_base

    @abstractmethod
    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        timeout: float | None = None,
        purpose: str | None = None,
    ) -> LLMResponse:
        """
        Send a chat completion request.

        Args:
            messages: List of message dicts with 'role' and 'content'.
            tools: Optional list of tool definitions.
            model: Model identifier (provider-specific).
            max_tokens: Maximum tokens in response.
            temperature: Sampling temperature.
            purpose: Optional non-OpenAI field carried through to the
                Flowly backend (``useflowlyapp.com/api/v1/chat/completions``)
                so coaching calls hit the coaching rate-limit bucket and
                screen-capture observability rather than the chat ones.
                ``"coaching"`` is the only value the backend currently
                routes on; anything else is treated as chat.

        Returns:
            LLMResponse with content and/or tool calls.
        """
        pass

    async def chat_stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        tool_choice: str = "auto",
    ) -> AsyncIterator[LLMResponse]:
        """
        Stream a chat completion request, yielding partial LLMResponse objects.

        Each yielded item has either:
          - content set (text delta, tool_calls empty) — a text token
          - tool_calls set (content None) — accumulated tool calls when stream ends

        Default implementation falls back to non-streaming chat() for providers
        that don't support streaming.
        """
        response = await self.chat(
            messages=messages,
            tools=tools,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        yield response

    @abstractmethod
    def get_default_model(self) -> str:
        """Get the default model for this provider."""
        pass
