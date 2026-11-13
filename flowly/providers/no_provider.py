"""The provider of an agent that has none yet.

A gateway started with ``--wait-for-provider`` (every service Core installs)
runs before its owner has signed in or picked a model: Flowly Desktop installs
the agent at its first launch, so the slow first start is over by the time the
person signs in. Until a provider lands, every call answers this error, which
the agent shows as "sign in or connect a provider"
(``flowly.agent.error_classifier``); signing in hot-reloads the real provider.
"""

from __future__ import annotations

from typing import Any

from flowly.providers.base import LLMErrorInfo, LLMProvider, LLMResponse

#: ``LLMErrorInfo.code`` of every answer: the error classifier keys on it.
NO_PROVIDER_CODE = "no_provider"

NO_PROVIDER_MESSAGE = (
    "No model provider yet: sign in to Flowly (`flowly login`) or connect one (`flowly setup`)."
)


class NoProvider(LLMProvider):
    provider_name = "none"

    def __init__(self, default_model: str = "") -> None:
        super().__init__(api_key=None, api_base=None)
        self._default_model = default_model

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        timeout: float | None = None,
        purpose: str | None = None,
        reasoning: bool | None = None,
    ) -> LLMResponse:
        del messages, tools, model, max_tokens, temperature, timeout, purpose, reasoning
        return LLMResponse(
            content=NO_PROVIDER_MESSAGE,
            finish_reason="error",
            error_info=LLMErrorInfo(code=NO_PROVIDER_CODE),
        )

    def get_default_model(self) -> str:
        return self._default_model
