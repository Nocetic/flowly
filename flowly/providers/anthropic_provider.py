"""Native Anthropic Messages API provider.

Flowly's internal message/tool contract is OpenAI chat-completions shaped.
Anthropic BYOK is not: direct Anthropic calls must use ``/v1/messages`` with
``x-api-key`` auth, ``system`` as a top-level field, and tool calls represented
as ``tool_use`` / ``tool_result`` content blocks. This provider performs that
translation while returning the same ``LLMResponse`` objects as the existing
OpenAI-compatible provider.
"""

from __future__ import annotations

import json
import os
from typing import Any, AsyncIterator

import httpx
from loguru import logger

from flowly.providers.base import LLMProvider, LLMResponse, ToolCallRequest
from flowly.providers.key_rotator import KeyRotator, classify_error
from flowly.providers.prompt_caching import apply_cache_control, is_cacheable_model

_DEFAULT_ANTHROPIC_BASE = "https://api.anthropic.com/v1"
_ANTHROPIC_VERSION = "2023-06-01"
_USER_AGENT = "flowly/anthropic-provider"

_STOP_REASON_MAP = {
    "end_turn": "stop",
    "tool_use": "tool_calls",
    "max_tokens": "length",
    "stop_sequence": "stop",
    "refusal": "content_filter",
    "model_context_window_exceeded": "length",
}


class AnthropicAPIError(RuntimeError):
    """HTTP/API error raised before conversion to an ``LLMResponse``."""

    def __init__(self, status_code: int, message: str):
        super().__init__(f"Anthropic HTTP {status_code}: {message}")
        self.status_code = status_code


def _normalize_base_url(api_base: str | None) -> str:
    base = (api_base or _DEFAULT_ANTHROPIC_BASE).strip().rstrip("/")
    if not base:
        base = _DEFAULT_ANTHROPIC_BASE
    if base.endswith("/messages"):
        base = base[: -len("/messages")].rstrip("/")
    if not base.endswith("/v1"):
        base = f"{base}/v1"
    return base


def _strip_known_prefixes(model: str) -> str:
    resolved = model
    for prefix in ("openrouter/anthropic/", "anthropic/"):
        if resolved.startswith(prefix):
            resolved = resolved[len(prefix):]
            break
    return resolved.replace("claude-haiku-4.5", "claude-haiku-4-5").replace(
        "claude-sonnet-4.5", "claude-sonnet-4-5"
    ).replace("claude-opus-4.5", "claude-opus-4-5")


def _strip_internal_keys(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    changed = False
    for msg in messages:
        if isinstance(msg, dict) and any(str(k).startswith("_") for k in msg):
            out.append({k: v for k, v in msg.items() if not str(k).startswith("_")})
            changed = True
        else:
            out.append(msg)
    return out if changed else messages


def _parse_args(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw) if raw else {}
            return parsed if isinstance(parsed, dict) else {"value": parsed}
        except json.JSONDecodeError:
            return {"raw": raw}
    if raw is None:
        return {}
    return {"value": raw}


def _content_part_to_anthropic(part: Any) -> dict[str, Any] | None:
    if isinstance(part, str):
        return {"type": "text", "text": part}
    if not isinstance(part, dict):
        return None

    part_type = str(part.get("type") or "").lower()
    if part_type in {"text", "input_text", "output_text"}:
        text = part.get("text")
        if isinstance(text, str):
            block: dict[str, Any] = {"type": "text", "text": text}
            if isinstance(part.get("cache_control"), dict):
                block["cache_control"] = part["cache_control"]
            return block
        return None

    if part_type == "image":
        source = part.get("source")
        if isinstance(source, dict):
            return {"type": "image", "source": source}
        return None

    if part_type in {"image_url", "input_image"}:
        image_ref = part.get("image_url")
        if isinstance(image_ref, dict):
            url = image_ref.get("url")
        else:
            url = image_ref
        if not isinstance(url, str) or not url:
            return None
        if url.startswith("data:") and ";base64," in url:
            media_type, data = url[5:].split(";base64,", 1)
            return {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": media_type or "image/png",
                    "data": data,
                },
            }
        return {"type": "image", "source": {"type": "url", "url": url}}

    # Already-native blocks that are valid on the request path.
    if part_type in {"tool_result", "tool_use"}:
        return dict(part)
    return None


def _content_to_blocks(content: Any, *, empty_text: str) -> list[dict[str, Any]]:
    if isinstance(content, list):
        blocks = [
            block for block in (_content_part_to_anthropic(part) for part in content)
            if block is not None
        ]
        return blocks or [{"type": "text", "text": empty_text}]
    if content is None:
        return [{"type": "text", "text": empty_text}]
    text = str(content)
    if not text.strip():
        text = empty_text
    return [{"type": "text", "text": text}]


def _apply_message_cache_control(blocks: list[dict[str, Any]], msg: dict[str, Any]) -> None:
    marker = msg.get("cache_control")
    if isinstance(marker, dict) and blocks:
        blocks[-1]["cache_control"] = marker


def _assistant_message_to_anthropic(msg: dict[str, Any]) -> dict[str, Any]:
    blocks: list[dict[str, Any]] = []
    content = msg.get("content")
    if content not in (None, ""):
        blocks.extend(_content_to_blocks(content, empty_text=""))

    for tc in msg.get("tool_calls") or []:
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function") if isinstance(tc.get("function"), dict) else {}
        name = fn.get("name")
        if not isinstance(name, str) or not name:
            continue
        blocks.append({
            "type": "tool_use",
            "id": str(tc.get("id") or f"toolu_{len(blocks)}"),
            "name": name,
            "input": _parse_args(fn.get("arguments")),
        })

    if not blocks:
        blocks = [{"type": "text", "text": "(empty)"}]
    _apply_message_cache_control(blocks, msg)
    return {"role": "assistant", "content": blocks}


def _append_tool_result(result: list[dict[str, Any]], msg: dict[str, Any]) -> None:
    content = msg.get("content")
    if isinstance(content, list):
        result_content: Any = _content_to_blocks(content, empty_text="(no output)")
    elif content is None or (isinstance(content, str) and not content.strip()):
        result_content = "(no output)"
    else:
        result_content = content if isinstance(content, str) else json.dumps(content)

    tool_result = {
        "type": "tool_result",
        "tool_use_id": str(msg.get("tool_call_id") or ""),
        "content": result_content,
    }
    if isinstance(msg.get("cache_control"), dict):
        tool_result["cache_control"] = msg["cache_control"]

    if (
        result
        and result[-1].get("role") == "user"
        and isinstance(result[-1].get("content"), list)
        and result[-1]["content"]
        and isinstance(result[-1]["content"][0], dict)
        and result[-1]["content"][0].get("type") == "tool_result"
    ):
        result[-1]["content"].append(tool_result)
    else:
        result.append({"role": "user", "content": [tool_result]})


def _messages_to_anthropic(
    messages: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]] | None, list[dict[str, Any]]]:
    system_blocks: list[dict[str, Any]] = []
    out: list[dict[str, Any]] = []

    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        if role == "system":
            blocks = _content_to_blocks(msg.get("content"), empty_text="")
            _apply_message_cache_control(blocks, msg)
            system_blocks.extend(blocks)
        elif role == "user":
            blocks = _content_to_blocks(msg.get("content"), empty_text="(empty message)")
            _apply_message_cache_control(blocks, msg)
            out.append({"role": "user", "content": blocks})
        elif role == "assistant":
            out.append(_assistant_message_to_anthropic(msg))
        elif role == "tool":
            _append_tool_result(out, msg)

    return (system_blocks or None), out


def _tools_to_anthropic(tools: list[dict[str, Any]] | None) -> list[dict[str, Any]] | None:
    if not tools:
        return None
    converted: list[dict[str, Any]] = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        fn = tool.get("function") if isinstance(tool.get("function"), dict) else None
        if not fn:
            continue
        name = fn.get("name")
        if not isinstance(name, str) or not name:
            continue
        params = fn.get("parameters") if isinstance(fn.get("parameters"), dict) else {
            "type": "object",
            "properties": {},
        }
        converted.append({
            "name": name,
            "description": str(fn.get("description") or ""),
            "input_schema": params,
        })
    return converted or None


def _tool_choice_to_anthropic(tool_choice: str | None) -> dict[str, Any] | None:
    if tool_choice in (None, "auto"):
        return {"type": "auto"}
    if tool_choice == "required":
        return {"type": "any"}
    if tool_choice == "none":
        return {"type": "none"}
    if isinstance(tool_choice, str) and tool_choice:
        return {"type": "tool", "name": tool_choice}
    return None


def _usage_from_anthropic(data: dict[str, Any] | None) -> dict[str, int]:
    if not isinstance(data, dict):
        return {}
    prompt = int(data.get("input_tokens") or data.get("prompt_tokens") or 0)
    completion = int(data.get("output_tokens") or data.get("completion_tokens") or 0)
    cache_read = int(data.get("cache_read_input_tokens") or data.get("cache_read_tokens") or 0)
    cache_write = int(
        data.get("cache_creation_input_tokens") or data.get("cache_write_tokens") or 0
    )
    usage = {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
    }
    if cache_read or cache_write:
        usage["cache_read_tokens"] = cache_read
        usage["cache_write_tokens"] = cache_write
    return usage


def _classify_anthropic_error(exc: Exception) -> str | None:
    if isinstance(exc, AnthropicAPIError):
        if exc.status_code in (401, 403):
            return "auth_error"
        if exc.status_code == 429:
            return "rate_limit"
        if exc.status_code in (500, 502, 503, 504, 529):
            return "overload"
    if isinstance(exc, (httpx.TimeoutException, httpx.ConnectError, httpx.NetworkError)):
        return "overload"
    return classify_error(str(exc))


class AnthropicProvider(LLMProvider):
    """LLM provider for Anthropic's native Messages API."""

    def __init__(
        self,
        api_key: str | None = None,
        api_base: str | None = None,
        default_model: str = "claude-haiku-4-5",
        fallback_keys: list[str] | None = None,
        provider_name: str = "anthropic",
        request_timeout_seconds: float | None = None,
    ):
        super().__init__(api_key, api_base)
        self.default_model = default_model
        self.provider_name = provider_name
        self._base_url = _normalize_base_url(api_base)

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

        all_keys: list[str] = []
        if api_key and api_key.strip():
            all_keys.append(api_key.strip())
        if fallback_keys:
            all_keys.extend(k.strip() for k in fallback_keys if k and k.strip() != api_key)
        if not all_keys:
            raise ValueError("AnthropicProvider needs a non-empty api_key.")

        self.api_key = all_keys[0]
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

    def _headers(self, api_key: str | None = None) -> dict[str, str]:
        return {
            "x-api-key": api_key or self.api_key or "",
            "anthropic-version": _ANTHROPIC_VERSION,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": _USER_AGENT,
        }

    def _redact(self, text: str) -> str:
        result = text
        if self.api_key and len(self.api_key) > 8:
            result = result.replace(self.api_key, "***")
        if self._rotator:
            for state in self._rotator._states:  # type: ignore[attr-defined]
                key = state.key
                if key and len(key) > 8:
                    result = result.replace(key, "***")
        return result

    def _error_response(self, exc: Exception) -> LLMResponse:
        message = self._redact(str(exc))
        logger.error("Anthropic call error: {}", message)
        return LLMResponse(content=f"Error calling LLM: {message}", finish_reason="error")

    def _prepare_model(self, model: str | None) -> str:
        return _strip_known_prefixes((model or self.default_model).strip())

    def _build_payload(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        model: str | None,
        max_tokens: int,
        temperature: float,
        tool_choice: str | None,
        stream: bool = False,
    ) -> dict[str, Any]:
        resolved_model = self._prepare_model(model)
        prepared = _strip_internal_keys(messages)
        if is_cacheable_model(resolved_model):
            prepared = apply_cache_control(prepared)
        system, anthropic_messages = _messages_to_anthropic(prepared)

        payload: dict[str, Any] = {
            "model": resolved_model,
            "messages": anthropic_messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if system:
            payload["system"] = system
        anthropic_tools = _tools_to_anthropic(tools)
        if anthropic_tools:
            payload["tools"] = anthropic_tools
            choice = _tool_choice_to_anthropic(tool_choice)
            if choice:
                payload["tool_choice"] = choice
        if stream:
            payload["stream"] = True
        return payload

    async def _raise_for_error(self, response: httpx.Response) -> None:
        if response.status_code < 400:
            return
        try:
            data = response.json()
            error = data.get("error") if isinstance(data, dict) else None
            if isinstance(error, dict):
                detail = str(error.get("message") or error.get("type") or response.text)
            else:
                detail = response.text
        except Exception:
            detail = response.text
        raise AnthropicAPIError(response.status_code, self._redact(detail[:500]))

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
        del purpose
        payload = self._build_payload(
            messages=messages,
            tools=tools,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            tool_choice=tool_choice,
        )
        request_timeout = timeout or self.request_timeout_seconds
        url = f"{self._base_url}/messages"

        max_attempts = self._rotator.key_count() if self._rotator else 1
        last_exc: Exception | None = None
        for attempt in range(max_attempts):
            current_key = self._rotator.current_key() if self._rotator else self.api_key
            try:
                async with httpx.AsyncClient(timeout=request_timeout) as client:
                    response = await client.post(url, json=payload, headers=self._headers(current_key))
                await self._raise_for_error(response)
                return self._parse_response(response.json())
            except Exception as exc:
                last_exc = exc
                reason = _classify_anthropic_error(exc)
                if reason and self._rotator and attempt < max_attempts - 1:
                    logger.warning(
                        "Anthropic call failed ({}), rotating key (attempt {}/{}): {}",
                        reason,
                        attempt + 1,
                        max_attempts,
                        self._redact(str(exc))[:120],
                    )
                    self._rotator.rotate(reason=reason)
                    continue
                break

        assert last_exc is not None
        return self._error_response(last_exc)

    def _parse_response(self, data: dict[str, Any]) -> LLMResponse:
        if not isinstance(data, dict):
            return LLMResponse(content="Error calling LLM: malformed Anthropic response", finish_reason="error")

        text_parts: list[str] = []
        tool_calls: list[ToolCallRequest] = []
        for block in data.get("content") or []:
            if not isinstance(block, dict):
                continue
            block_type = block.get("type")
            if block_type == "text":
                text = block.get("text")
                if isinstance(text, str):
                    text_parts.append(text)
            elif block_type == "tool_use":
                tool_calls.append(ToolCallRequest(
                    id=str(block.get("id") or f"toolu_{len(tool_calls)}"),
                    name=str(block.get("name") or ""),
                    arguments=_parse_args(block.get("input")),
                ))

        stop_reason = str(data.get("stop_reason") or "end_turn")
        return LLMResponse(
            content="".join(text_parts) or None,
            tool_calls=tool_calls,
            finish_reason=_STOP_REASON_MAP.get(stop_reason, stop_reason or "stop"),
            usage=_usage_from_anthropic(data.get("usage")),
        )

    async def chat_stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        tool_choice: str = "auto",
    ) -> AsyncIterator[LLMResponse]:
        payload = self._build_payload(
            messages=messages,
            tools=tools,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            tool_choice=tool_choice,
            stream=True,
        )
        current_key = self._rotator.current_key() if self._rotator else self.api_key
        url = f"{self._base_url}/messages"

        tool_call_accum: dict[int, dict[str, Any]] = {}
        finish_reason = "stop"
        usage: dict[str, int] = {}

        try:
            async with httpx.AsyncClient(timeout=self.stream_timeout_seconds) as client:
                async with client.stream(
                    "POST",
                    url,
                    json=payload,
                    headers=self._headers(current_key),
                ) as response:
                    if response.status_code >= 400:
                        await response.aread()
                        await self._raise_for_error(response)

                    async for raw_line in response.aiter_lines():
                        line = raw_line.strip()
                        if not line or not line.startswith("data:"):
                            continue
                        raw_data = line[len("data:"):].strip()
                        if not raw_data or raw_data == "[DONE]":
                            continue
                        try:
                            event = json.loads(raw_data)
                        except json.JSONDecodeError:
                            continue
                        if not isinstance(event, dict):
                            continue

                        event_type = event.get("type")
                        if event_type == "message_start":
                            usage.update(_usage_from_anthropic(
                                (event.get("message") or {}).get("usage")
                                if isinstance(event.get("message"), dict) else None
                            ))
                        elif event_type == "content_block_start":
                            index = int(event.get("index") or 0)
                            block = event.get("content_block")
                            if isinstance(block, dict) and block.get("type") == "tool_use":
                                initial_input = block.get("input")
                                tool_call_accum[index] = {
                                    "id": str(block.get("id") or f"toolu_{index}"),
                                    "name": str(block.get("name") or ""),
                                    "arguments_str": (
                                        json.dumps(initial_input)
                                        if isinstance(initial_input, dict) and initial_input else ""
                                    ),
                                }
                        elif event_type == "content_block_delta":
                            index = int(event.get("index") or 0)
                            delta = event.get("delta")
                            if not isinstance(delta, dict):
                                continue
                            delta_type = delta.get("type")
                            if delta_type == "text_delta":
                                text = delta.get("text")
                                if isinstance(text, str) and text:
                                    yield LLMResponse(content=text, finish_reason="")
                            elif delta_type == "input_json_delta":
                                partial = delta.get("partial_json")
                                if isinstance(partial, str):
                                    entry = tool_call_accum.setdefault(
                                        index,
                                        {
                                            "id": f"toolu_{index}",
                                            "name": "",
                                            "arguments_str": "",
                                        },
                                    )
                                    entry["arguments_str"] += partial
                        elif event_type == "message_delta":
                            delta = event.get("delta")
                            if isinstance(delta, dict) and delta.get("stop_reason"):
                                finish_reason = _STOP_REASON_MAP.get(
                                    str(delta["stop_reason"]),
                                    str(delta["stop_reason"]),
                                )
                            delta_usage = _usage_from_anthropic(event.get("usage"))
                            if delta_usage:
                                usage.update({k: v for k, v in delta_usage.items() if v})
                        elif event_type == "error":
                            error = event.get("error")
                            detail = error.get("message") if isinstance(error, dict) else str(error)
                            yield self._error_response(RuntimeError(detail))
                            return
        except Exception as exc:
            yield self._error_response(exc)
            return

        final_usage = dict(usage)
        if final_usage:
            final_usage["total_tokens"] = (
                final_usage.get("prompt_tokens", 0)
                + final_usage.get("completion_tokens", 0)
            )

        tool_calls: list[ToolCallRequest] = []
        for index in sorted(tool_call_accum):
            entry = tool_call_accum[index]
            tool_calls.append(ToolCallRequest(
                id=entry["id"],
                name=entry["name"],
                arguments=_parse_args(entry.get("arguments_str")),
            ))

        yield LLMResponse(
            content=None,
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            usage=final_usage,
        )

    def get_default_model(self) -> str:
        return self.default_model
