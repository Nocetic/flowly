"""OpenRouter provider using the openai SDK.

Talks to any OpenAI-compatible endpoint (OpenRouter, Flowly proxy, direct OpenAI).
Replaces the previous LiteLLM-based provider — significantly smaller dependency
footprint (no tokenizers/jinja2/jsonschema) and Nuitka-friendly static imports.

Key behaviors matched to the legacy LiteLLMProvider to keep downstream consumers
(agent loop, subagent manager, compaction summarizer) working without changes:

- ``chat()`` returns an ``LLMResponse`` on success AND on error (error responses
  carry ``finish_reason="error"`` and the error message in ``content``).
- ``chat_stream()`` yields text deltas first, then a final chunk with
  ``finish_reason`` set (plus any accumulated ``tool_calls``).
- ``ToolCallRequest.arguments`` is always a pre-parsed ``dict`` (never a JSON string).
- ``usage`` dict contains ``prompt_tokens`` / ``completion_tokens`` / ``total_tokens``
  and optionally ``cache_read_tokens`` / ``cache_write_tokens`` if the upstream
  response exposed them.
- ``KeyRotator`` is used for auth/rate-limit/overload rotation, same as before.
"""

from __future__ import annotations

import json
import os
from typing import Any, AsyncIterator

from loguru import logger

from openai import AsyncOpenAI
from openai import (
    APIConnectionError,
    APIError,
    APITimeoutError,
    AuthenticationError,
    BadRequestError,
    InternalServerError,
    PermissionDeniedError,
    RateLimitError,
)

from flowly.providers.base import LLMProvider, LLMResponse, ToolCallRequest
from flowly.providers.key_rotator import KeyRotator, classify_error
from flowly.providers.prompt_caching import apply_cache_control, is_cacheable_model


_DEFAULT_OPENROUTER_BASE = "https://openrouter.ai/api/v1"


def _strip_known_prefixes(model: str) -> str:
    """Strip litellm-era provider prefixes that some configs may still carry."""
    for prefix in ("openrouter/", "hosted_vllm/", "zhipu/", "zai/", "gemini/"):
        if model.startswith(prefix):
            return model[len(prefix):]
    return model


def _model_consumes_thought_signature(model: str | None) -> bool:
    """True when ``model`` is a Gemini-family model that requires the
    ``extra_content`` (thought_signature) on tool calls to be replayed.

    Gemini thinking models attach ``extra_content`` to each tool call and
    reject a follow-up turn with HTTP 400 if it's missing. Every other strict
    OpenAI-compatible provider rejects the request with 400 if ``extra_content``
    *is* present — so it must be kept only for Gemini targets and stripped
    otherwise, including when a non-Gemini model inherits a stale Gemini
    ``extra_content`` from earlier in a mixed-provider / provider-switch session.
    """
    m = (model or "").lower()
    return "gemini" in m or "gemma" in m


def _extract_tool_call_extra(tc: Any) -> dict[str, Any] | None:
    """Pull ``extra_content`` off a (streaming or non-streaming) tool-call
    object. The OpenAI SDK parks unknown response fields in ``model_extra``;
    Gemini returns ``extra_content`` there. Returns a plain dict (or None)."""
    extra = getattr(tc, "extra_content", None)
    if extra is None:
        model_extra = getattr(tc, "model_extra", None)
        if isinstance(model_extra, dict):
            extra = model_extra.get("extra_content")
    if extra is None:
        return None
    if hasattr(extra, "model_dump"):
        try:
            extra = extra.model_dump()
        except Exception:
            pass
    return extra if isinstance(extra, dict) else None


def _strip_tool_call_extra_content(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return ``messages`` with ``extra_content`` removed from every tool call.

    Used when the target model is NOT Gemini-family: strict providers reject a
    payload that carries ``extra_content`` ("Extra inputs are not permitted").
    Copies lazily — only the messages/tool_calls that actually carry it."""
    out: list[dict[str, Any]] = []
    for msg in messages:
        calls = msg.get("tool_calls") if isinstance(msg, dict) else None
        if calls and any(isinstance(c, dict) and "extra_content" in c for c in calls):
            new_calls = [
                {k: v for k, v in c.items() if k != "extra_content"}
                if isinstance(c, dict) else c
                for c in calls
            ]
            out.append({**msg, "tool_calls": new_calls})
        else:
            out.append(msg)
    return out


def _strip_internal_keys(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return ``messages`` with underscore-prefixed bookkeeping keys removed.

    The agent loop tags internal messages (e.g. ``_ephemeral_nudge`` on
    corrective steers) so it can keep them out of persistence. Those keys
    must not reach the API — strict providers 400 on unrecognised fields.
    Copies lazily — only the messages that actually carry an internal key —
    so the caller's working list keeps the markers for the persistence
    filter that runs after the turn."""
    out: list[dict[str, Any]] = []
    changed = False
    for msg in messages:
        if isinstance(msg, dict) and any(str(k).startswith("_") for k in msg):
            out.append({k: v for k, v in msg.items() if not str(k).startswith("_")})
            changed = True
        else:
            out.append(msg)
    return out if changed else messages


def _classify_openai_exception(exc: Exception) -> str | None:
    """Map an openai SDK exception to a KeyRotator rotation reason."""
    if isinstance(exc, AuthenticationError):
        return "auth_error"
    if isinstance(exc, PermissionDeniedError):
        return "auth_error"
    if isinstance(exc, RateLimitError):
        return "rate_limit"
    if isinstance(exc, InternalServerError):
        return "overload"
    if isinstance(exc, (APITimeoutError, APIConnectionError)):
        return "overload"
    return None


class OpenRouterProvider(LLMProvider):
    """LLM provider using the openai SDK pointed at an OpenAI-compatible endpoint.

    Despite the name, this works with any OpenAI-compatible API — OpenRouter,
    the Flowly proxy (useflowlyapp.com), or direct OpenAI. The name reflects the
    primary use case.

    When multiple API keys are supplied via ``fallback_keys`` the provider
    rotates to the next healthy key on auth/rate-limit/overload errors.
    """

    def __init__(
        self,
        api_key: str | None = None,
        api_base: str | None = None,
        default_model: str = "anthropic/claude-haiku-4.5",
        fallback_keys: list[str] | None = None,
        provider_name: str = "openrouter",
        request_timeout_seconds: float | None = None,
    ):
        super().__init__(api_key, api_base)
        self.default_model = default_model
        self.provider_name = provider_name
        # Timeout precedence: FLOWLY_LLM_TIMEOUT_SECONDS env var (power-user
        # override) > caller-supplied value (from config.agents.defaults.
        # llmTimeoutSeconds, set via the desktop UI) > 120s default.
        _env = os.getenv("FLOWLY_LLM_TIMEOUT_SECONDS")
        self.request_timeout_seconds = (
            float(_env) if _env
            else float(request_timeout_seconds) if request_timeout_seconds
            else 120.0
        )
        _stream_env = os.getenv("FLOWLY_LLM_STREAM_TIMEOUT_SECONDS")
        self.stream_timeout_seconds = (
            float(_stream_env) if _stream_env
            else self.request_timeout_seconds
        )

        # Resolve base URL: explicit api_base wins, fall back to OpenRouter.
        self._base_url = api_base or _DEFAULT_OPENROUTER_BASE

        # Default headers for OpenRouter dashboard analytics. The Flowly proxy
        # overwrites these with its own values before forwarding to OpenRouter,
        # so this only affects direct OpenRouter usage.
        self._default_headers = {
            "X-Title": "Flowly",
            "HTTP-Referer": "https://github.com/Nocetic/flowly",
        }

        # Key rotation setup
        all_keys: list[str] = []
        if api_key:
            all_keys.append(api_key)
        if fallback_keys:
            all_keys.extend(k for k in fallback_keys if k and k != api_key)

        self._rotator: KeyRotator | None = None
        if len(all_keys) > 1:
            from flowly.audit.logger import get_audit_logger
            audit = get_audit_logger()
            self._rotator = KeyRotator(
                keys=all_keys,
                provider=provider_name,
                on_rotate=lambda prov, reason, fi, ti: audit.log_key_rotation(
                    provider=prov, reason=reason, key_index_from=fi, key_index_to=ti
                ),
            )
            logger.info(
                f"[KeyRotator] {provider_name}: {len(all_keys)} keys configured"
            )

        # Build the shared async client. We refuse to start with an empty
        # api_key: the AsyncOpenAI fallback would silently send a bogus
        # ``Authorization: Bearer placeholder`` header and upstream would
        # return "missing/invalid auth" — an impossible-to-debug error.
        # Caller (gateway_cmd) is expected to resolve a usable key first;
        # if it can't, raise loudly here so the issue surfaces at boot.
        resolved_key = (api_key or "").strip()
        if not resolved_key:
            raise ValueError(
                f"OpenRouterProvider (provider={provider_name}) needs a "
                f"non-empty api_key — got empty / whitespace. Configure "
                f"one via the /integrations modal in the TUI."
            )
        self._client = AsyncOpenAI(
            api_key=resolved_key,
            base_url=self._base_url,
            default_headers=self._default_headers,
            timeout=self.request_timeout_seconds,
            max_retries=0,  # We handle retries via KeyRotator
        )

    # ------------------------------------------------------------------ utils

    def _prepare_model(self, model: str | None) -> str:
        resolved = model or self.default_model
        return _strip_known_prefixes(resolved)

    def _prepare_messages(self, messages: list[dict[str, Any]], model: str) -> list[dict[str, Any]]:
        """Sanitize + mark messages for the target model.

        - Gemini thought-signature replay: strip ``extra_content`` from tool
          calls unless the target model is Gemini-family (strict providers 400
          on its presence; Gemini 400s on its absence).
        - Apply Anthropic prompt-caching markers for Claude models.
        """
        # Drop internal bookkeeping keys (e.g. ``_ephemeral_nudge``) before the
        # API call; copy-based so the caller's list keeps them for persistence
        # filtering afterwards.
        messages = _strip_internal_keys(messages)
        if not _model_consumes_thought_signature(model):
            messages = _strip_tool_call_extra_content(messages)
        if is_cacheable_model(model):
            return apply_cache_control(messages)
        return messages

    def _client_for_key(self, api_key: str | None) -> AsyncOpenAI:
        """Return a client bound to the given API key (or the default client)."""
        if api_key and api_key != self._client.api_key:
            return self._client.with_options(api_key=api_key)
        return self._client

    def _redact(self, text: str) -> str:
        """Redact known API keys from error strings."""
        result = text
        if self.api_key and len(self.api_key) > 8:
            result = result.replace(self.api_key, "***")
        if self._rotator:
            for state in self._rotator._states:  # type: ignore[attr-defined]
                k = state.key
                if k and len(k) > 8:
                    result = result.replace(k, "***")
        return result

    def _error_response(self, exc: Exception) -> LLMResponse:
        error_msg = self._redact(str(exc))
        logger.error(f"LLM call error: {error_msg}")
        return LLMResponse(
            content=f"Error calling LLM: {error_msg}",
            finish_reason="error",
        )

    # ------------------------------------------------------------------- chat

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        tool_choice: str = "auto",
        timeout: float | None = None,
        purpose: str | None = None,
    ) -> LLMResponse:
        """Send a non-streaming chat completion request."""
        resolved_model = self._prepare_model(model)
        prepared_messages = self._prepare_messages(messages, resolved_model)

        kwargs: dict[str, Any] = {
            "model": resolved_model,
            "messages": prepared_messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = tool_choice
        # ``purpose`` is a Flowly-only field carried through extra_body so
        # the backend (useflowlyapp.com/api/v1/chat/completions) can route
        # coaching calls to a separate rate-limit bucket. Public OpenAI /
        # OpenRouter ignore unknown body fields; the backend strips this
        # before forwarding upstream so no leak occurs.
        if purpose:
            kwargs["extra_body"] = {"purpose": purpose}

        request_timeout = timeout or self.request_timeout_seconds

        max_attempts = self._rotator.key_count() if self._rotator else 1
        last_exc: Exception | None = None

        for attempt in range(max_attempts):
            current_key = self._rotator.current_key() if self._rotator else self.api_key
            client = self._client_for_key(current_key)
            try:
                response = await client.with_options(timeout=request_timeout).chat.completions.create(**kwargs)
                return self._parse_response(response)
            except Exception as exc:
                last_exc = exc
                rotation_reason = _classify_openai_exception(exc)
                if rotation_reason is None:
                    # Fallback to string-based classification for OpenRouter
                    # upstream errors that the SDK wraps as generic APIError.
                    rotation_reason = classify_error(self._redact(str(exc)))

                if rotation_reason and self._rotator and attempt < max_attempts - 1:
                    logger.warning(
                        f"LLM call failed ({rotation_reason}), rotating key "
                        f"(attempt {attempt + 1}/{max_attempts}): "
                        f"{self._redact(str(exc))[:120]}"
                    )
                    self._rotator.rotate(reason=rotation_reason)
                    continue
                break

        assert last_exc is not None
        return self._error_response(last_exc)

    # ------------------------------------------------------------ parse (non-stream)

    def _parse_response(self, response: Any) -> LLMResponse:
        """Parse an openai SDK ChatCompletion into our standard LLMResponse."""
        choice = response.choices[0]
        message = choice.message

        tool_calls: list[ToolCallRequest] = []
        raw_tool_calls = getattr(message, "tool_calls", None) or []
        for tc in raw_tool_calls:
            args = tc.function.arguments
            if isinstance(args, str):
                try:
                    args = json.loads(args) if args else {}
                except json.JSONDecodeError:
                    args = {"raw": args}
            elif args is None:
                args = {}
            tool_calls.append(ToolCallRequest(
                id=tc.id,
                name=tc.function.name,
                arguments=args,
                extra_content=_extract_tool_call_extra(tc),
            ))

        usage: dict[str, int] = {}
        if getattr(response, "usage", None):
            u = response.usage
            usage = {
                "prompt_tokens": getattr(u, "prompt_tokens", 0) or 0,
                "completion_tokens": getattr(u, "completion_tokens", 0) or 0,
                "total_tokens": getattr(u, "total_tokens", 0) or 0,
            }
            # Anthropic-style fields (litellm compat; some proxies still expose these)
            cache_read = getattr(u, "cache_read_input_tokens", 0) or 0
            cache_write = getattr(u, "cache_creation_input_tokens", 0) or 0
            # OpenAI-style nested field (prompt_tokens_details.cached_tokens)
            details = getattr(u, "prompt_tokens_details", None)
            if details is not None:
                cached = getattr(details, "cached_tokens", 0) or 0
                if cached and not cache_read:
                    cache_read = cached
            if cache_read or cache_write:
                usage["cache_read_tokens"] = cache_read
                usage["cache_write_tokens"] = cache_write
                prompt = usage.get("prompt_tokens", 0) or 1
                logger.info(
                    f"Prompt cache: {cache_read:,}/{prompt:,} tokens read "
                    f"({cache_read * 100 // prompt}% hit), {cache_write:,} written"
                )

        return LLMResponse(
            content=message.content,
            tool_calls=tool_calls,
            finish_reason=choice.finish_reason or "stop",
            usage=usage,
        )

    # ----------------------------------------------------------------- stream

    async def chat_stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        tool_choice: str = "auto",
    ) -> AsyncIterator[LLMResponse]:
        """Stream a chat completion.

        Yields text-delta LLMResponses (content set, no tool_calls) as tokens
        arrive, then a final LLMResponse with ``finish_reason`` set and any
        accumulated ``tool_calls``. This matches what the agent loop expects
        in ``flowly/agent/loop.py:1115-1151``.
        """
        resolved_model = self._prepare_model(model)
        prepared_messages = self._prepare_messages(messages, resolved_model)

        kwargs: dict[str, Any] = {
            "model": resolved_model,
            "messages": prepared_messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": True,
            # Ensure usage metrics are emitted in the final stream chunk —
            # required for the Flowly proxy's rate-limit + credit tracking.
            "stream_options": {"include_usage": True},
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = tool_choice

        current_key = self._rotator.current_key() if self._rotator else self.api_key
        client = self._client_for_key(current_key).with_options(
            timeout=self.stream_timeout_seconds
        )

        try:
            stream = await client.chat.completions.create(**kwargs)
        except Exception as exc:
            yield self._error_response(exc)
            return

        tool_call_accum: dict[int, dict[str, Any]] = {}
        finish_reason = "stop"
        final_usage: dict[str, int] = {}

        try:
            async for chunk in stream:
                # Usage-only chunks (final chunk with include_usage) have no choices
                if getattr(chunk, "usage", None):
                    u = chunk.usage
                    # Canonical usage extraction for the chat_completions
                    # branch (see ``flowly/agent/usage_pricing.py``).
                    # Three things matter:
                    #
                    # 1. ``prompt_tokens`` is ALWAYS the full input
                    #    incl. cache when the response is OAI-shape
                    #    (every endpoint our client hits). Downstream
                    #    sums prompt + completion as context occupancy
                    #    — don't add cache to prompt or we'd
                    #    triple-count cached bytes.
                    # 2. Cache details live under
                    #    ``prompt_tokens_details.cached_tokens``
                    #    (OAI / xAI / Codex via OR) OR at the top
                    #    level (``cache_read_input_tokens`` /
                    #    ``cache_creation_input_tokens`` when OR
                    #    proxies Anthropic without renesting). Read
                    #    both paths so per-turn cache reporting works
                    #    regardless of which upstream model OR is
                    #    routing.
                    # 3. ``reasoning_tokens`` (OpenAI o1/o3, DeepSeek
                    #    reasoner) sit under
                    #    ``completion_tokens_details.reasoning_tokens``.
                    #    Surfaced so future per-turn breakdown UIs
                    #    can split them out — completion_tokens
                    #    already includes them so context maths is
                    #    unaffected.
                    details = getattr(u, "prompt_tokens_details", None)
                    cache_read = 0
                    cache_write = 0
                    if details is not None:
                        cache_read = int(getattr(details, "cached_tokens", 0) or 0)
                        cache_write = int(getattr(details, "cache_write_tokens", 0) or 0)
                    if not cache_read:
                        cache_read = int(
                            getattr(u, "cache_read_input_tokens", 0)
                            or getattr(u, "cache_read_tokens", 0) or 0
                        )
                    if not cache_write:
                        cache_write = int(
                            getattr(u, "cache_creation_input_tokens", 0)
                            or getattr(u, "cache_write_tokens", 0) or 0
                        )
                    out_details = getattr(u, "completion_tokens_details", None)
                    reasoning = 0
                    if out_details is not None:
                        reasoning = int(getattr(out_details, "reasoning_tokens", 0) or 0)
                    final_usage = {
                        "prompt_tokens":      int(getattr(u, "prompt_tokens", 0) or 0),
                        "completion_tokens":  int(getattr(u, "completion_tokens", 0) or 0),
                        "total_tokens":       int(getattr(u, "total_tokens", 0) or 0),
                        "cache_read_tokens":  cache_read,
                        "cache_write_tokens": cache_write,
                        "reasoning_tokens":   reasoning,
                    }

                if not chunk.choices:
                    continue

                choice = chunk.choices[0]
                delta = choice.delta
                if choice.finish_reason:
                    finish_reason = choice.finish_reason

                if getattr(delta, "content", None):
                    yield LLMResponse(content=delta.content, finish_reason="")

                if getattr(delta, "tool_calls", None):
                    for tc_delta in delta.tool_calls:
                        idx = tc_delta.index
                        entry = tool_call_accum.setdefault(
                            idx, {"id": "", "name": "", "arguments_str": ""}
                        )
                        if tc_delta.id:
                            entry["id"] = tc_delta.id
                        fn = getattr(tc_delta, "function", None)
                        if fn is not None:
                            if getattr(fn, "name", None):
                                entry["name"] += fn.name
                            if getattr(fn, "arguments", None):
                                entry["arguments_str"] += fn.arguments
                        # Gemini thought_signature arrives via extra_content on
                        # (usually the first) delta of each tool call — keep it.
                        extra = _extract_tool_call_extra(tc_delta)
                        if extra is not None:
                            entry["extra_content"] = extra
        except Exception as exc:
            logger.error(f"LLM stream read error: {self._redact(str(exc))}")

        # Emit the final response (with tool calls if any).
        if tool_call_accum:
            tool_calls: list[ToolCallRequest] = []
            for idx in sorted(tool_call_accum):
                entry = tool_call_accum[idx]
                args_str = entry["arguments_str"]
                try:
                    args = json.loads(args_str) if args_str else {}
                except json.JSONDecodeError:
                    args = {"raw": args_str}
                tool_calls.append(ToolCallRequest(
                    id=entry["id"] or f"call_{idx}",
                    name=entry["name"],
                    arguments=args,
                    extra_content=entry.get("extra_content"),
                ))
            yield LLMResponse(
                content=None,
                tool_calls=tool_calls,
                finish_reason=finish_reason,
                usage=final_usage,
            )
        else:
            yield LLMResponse(
                content=None,
                finish_reason=finish_reason,
                usage=final_usage,
            )

    # ---------------------------------------------------------------- default

    def get_default_model(self) -> str:
        return self.default_model
