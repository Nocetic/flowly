"""Short answers the agent asks for in the background.

The dreamer, memory import and consolidation and skill curation each send one
prompt and want a short JSON answer under a fixed ``max_tokens``. A thinking
model given that cap can spend all of it thinking and answer nothing
(``finish_reason=length``, every completion token reasoning): measured on
``deepseek/deepseek-v4.1-flash`` with the dreamer's own prompt, three empty
attempts in a row, and with thinking off a whole answer in a quarter of the
cap. So these calls ask the model not to think (a model that cannot turn it
off is asked again as it is, by the provider).

They stream, as before: the Flowly backend times out long non-streamed
completions. A provider error arrives on the stream as text
(``Error calling LLM: …``); it is never taken for an answer.
"""

from __future__ import annotations

from typing import Any

from loguru import logger


class BackgroundAnswerError(RuntimeError):
    """Every attempt failed at the provider; no answer, empty or not, came back."""


async def background_answer(
    provider: Any,
    prompt: str,
    *,
    model: str,
    max_tokens: int,
    temperature: float,
    label: str,
    attempts: int = 3,
) -> str:
    """The model's answer to *prompt*, thinking off.

    Returns ``""`` when the model answered nothing on every attempt; raises
    :class:`BackgroundAnswerError` when every attempt failed at the provider.
    """
    failure = ""
    answered = False
    for attempt in range(1, attempts + 1):
        parts: list[str] = []
        finish = ""
        usage: dict[str, int] = {}
        try:
            async for delta in provider.chat_stream(
                [{"role": "user", "content": prompt}],
                model=model,
                max_tokens=max_tokens,
                temperature=temperature,
                reasoning=False,
            ):
                # Read loosely: a provider yields LLMResponse, a stand-in may not.
                content = getattr(delta, "content", None)
                reason = getattr(delta, "finish_reason", "") or ""
                if reason == "error":
                    failure = (content or "provider error")[:300]
                    finish = "error"
                    break
                if content:
                    parts.append(content)
                if reason:
                    finish = reason
                usage = getattr(delta, "usage", None) or usage
        except Exception as exc:  # noqa: BLE001 — a failed call is retried like an empty one
            failure = f"{type(exc).__name__}: {exc}"[:300]
            logger.warning(f"[{label}] attempt {attempt} failed: {failure}")
            continue
        if finish == "error":
            logger.warning(f"[{label}] attempt {attempt} failed: {failure}")
            continue
        answered = True
        text = "".join(parts)
        if text.strip():
            if finish == "length":
                logger.warning(f"[{label}] answer cut at max_tokens={max_tokens}")
            return text
        logger.warning(
            f"[{label}] attempt {attempt} empty (finish={finish or '?'}, "
            f"reasoning_tokens={usage.get('reasoning_tokens', '?')}); retrying"
        )
    if not answered:
        raise BackgroundAnswerError(failure or "no answer")
    return ""
