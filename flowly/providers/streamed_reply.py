"""One streamed model reply, assembled into the response a blocking call returns.

``LLMProvider.chat_stream`` yields text deltas (``content`` set,
``finish_reason=""``), tool-argument previews, and one final chunk carrying
``finish_reason``, the tool calls and the usage (its ``content`` is ``None``
when the text came as deltas, or the error text on ``finish_reason="error"``).
:class:`StreamedReply` takes those chunks one by one and builds the final
:class:`LLMResponse` — the same fields a ``chat()`` call returns: the whole
text, the tool calls as the provider assembled them (``extra_content``
included), the finish reason, the usage of the final chunk, the provider's
continuity state and replay items.

Shared by the main agent's loop (``AgentLoop._chat_with_stream``) and a
subagent's model call (``SubagentManager._stream_model``), so a streamed reply
means the same thing to both.
"""

from __future__ import annotations

import copy

from flowly.providers.base import LLMResponse


class StreamedReply:
    """Accumulates one ``chat_stream`` reply."""

    __slots__ = ("text", "final", "chunks")

    def __init__(self) -> None:
        self.text = ""
        self.final: LLMResponse | None = None
        self.chunks = 0

    def feed(self, chunk: LLMResponse) -> str:
        """Take one chunk; return the text it adds to the reply ("" for none).

        Error text is routing input, not model output: it never becomes reply
        text (it stays on the final chunk for classification).
        """
        text = ""
        if chunk.content and chunk.finish_reason != "error":
            self.chunks += 1
            self.text += chunk.content
            text = chunk.content
        if chunk.finish_reason:
            self.final = chunk
        return text

    def interrupted(self, finish_reason: str, *, keep_usage: bool = False) -> LLMResponse:
        """The reply cut short (stopped, steered): its text so far, no tool calls.

        Half-built tool calls are never kept — they are unsafe to execute.
        """
        final = self.final
        return LLMResponse(
            content=self.text or None,
            finish_reason=finish_reason,
            usage=dict(final.usage) if keep_usage and final is not None and final.usage else {},
            partial_content_delivered=bool(self.text),
        )

    def response(self) -> LLMResponse:
        """The finished reply, as one response."""
        final = self.final
        usage = dict(final.usage) if final is not None and final.usage else {}
        error_info = final.error_info if final is not None else None
        provider_state = dict(final.provider_state) if final is not None and final.provider_state else {}
        provider_replay = (copy.deepcopy(final.provider_replay)
                           if final is not None and final.provider_replay else {})
        delivered = bool(self.text)
        if final is None:
            return LLMResponse(
                content=self.text or None,
                finish_reason="stop",
                usage=usage,
                partial_content_delivered=delivered,
                provider_state=provider_state,
                provider_replay=provider_replay,
            )
        if final.tool_calls:
            # The final chunk had the tool calls; the text came in earlier deltas.
            return LLMResponse(
                content=self.text or None,
                tool_calls=final.tool_calls,
                finish_reason=final.finish_reason,
                usage=usage,
                error_info=error_info,
                partial_content_delivered=delivered,
                provider_state=provider_state,
                provider_replay=provider_replay,
            )
        return LLMResponse(
            content=self.text or final.content,
            finish_reason=final.finish_reason,
            usage=usage,
            error_info=error_info,
            partial_content_delivered=delivered,
            provider_state=provider_state,
            provider_replay=provider_replay,
        )
