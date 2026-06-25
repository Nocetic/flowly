"""Base LLM provider interface."""

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


@dataclass
class LLMResponse:
    """Response from an LLM provider."""
    content: str | None
    tool_calls: list[ToolCallRequest] = field(default_factory=list)
    finish_reason: str = "stop"
    usage: dict[str, int] = field(default_factory=dict)

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
