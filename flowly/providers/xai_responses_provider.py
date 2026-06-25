"""xAI Responses API provider used by Grok subscription OAuth."""

from __future__ import annotations

import copy
import json
import os
from typing import Any, AsyncIterator

import httpx
from loguru import logger

from flowly.auth.xai_oauth import (
    DEFAULT_XAI_OAUTH_BASE_URL,
    XAIAuthError,
    XAIEntitlementError,
    redact_secret,
    refresh_tokens,
    resolve_runtime_credentials,
    validate_xai_oauth_base_url,
)
from flowly.providers.base import LLMProvider, LLMResponse, ToolCallRequest

DEFAULT_XAI_RESPONSES_MODEL = os.getenv("FLOWLY_XAI_OAUTH_MODEL", "grok-4.20-reasoning")

def _strip_known_prefixes(model: str) -> str:
    for prefix in ("xai/", "grok/", "openrouter/"):
        if model.startswith(prefix):
            return model[len(prefix):]
    return model


def _normalize_xai_model(model: str | None) -> str:
    candidate = _strip_known_prefixes(model or DEFAULT_XAI_RESPONSES_MODEL).strip()
    if not candidate or "/" in candidate or not candidate.lower().startswith("grok"):
        return DEFAULT_XAI_RESPONSES_MODEL
    return candidate


def _content_parts_for_responses(content: Any, *, role: str) -> list[dict[str, Any]]:
    text_type = "output_text" if role == "assistant" else "input_text"
    if not isinstance(content, list):
        return []
    out: list[dict[str, Any]] = []
    for part in content:
        if isinstance(part, str):
            if part:
                out.append({"type": text_type, "text": part})
            continue
        if not isinstance(part, dict):
            continue
        part_type = str(part.get("type") or "").lower()
        if part_type in {"text", "input_text", "output_text"}:
            text = part.get("text")
            if isinstance(text, str) and text:
                out.append({"type": text_type, "text": text})
        elif part_type in {"image_url", "input_image"}:
            image_ref = part.get("image_url")
            detail = part.get("detail")
            if isinstance(image_ref, dict):
                url = image_ref.get("url")
                detail = image_ref.get("detail", detail)
            else:
                url = image_ref
            if isinstance(url, str) and url:
                image_part = {"type": "input_image", "image_url": url}
                if isinstance(detail, str) and detail:
                    image_part["detail"] = detail
                out.append(image_part)
    return out


def _split_responses_tool_id(raw_id: Any) -> tuple[str, str]:
    if not isinstance(raw_id, str):
        return "", ""
    value = raw_id.strip()
    if not value:
        return "", ""
    if "|" in value:
        call_id, response_item_id = value.split("|", 1)
        return call_id.strip(), response_item_id.strip()
    if value.startswith("fc_"):
        return "", value
    return value, ""


def _messages_to_responses_input(messages: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    instructions: list[str] = []
    items: list[dict[str, Any]] = []

    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        if role == "system":
            content = msg.get("content")
            if isinstance(content, str) and content.strip():
                instructions.append(content.strip())
            continue

        if role in {"user", "assistant"}:
            content = msg.get("content", "")
            if isinstance(content, list):
                parts = _content_parts_for_responses(content, role=role)
                if parts:
                    items.append({"role": role, "content": parts})
                else:
                    items.append({"role": role, "content": ""})
            else:
                text = "" if content is None else str(content)
                if text or role == "user":
                    items.append({"role": role, "content": text})

            if role == "assistant":
                for idx, tc in enumerate(msg.get("tool_calls") or []):
                    if not isinstance(tc, dict):
                        continue
                    fn = tc.get("function") if isinstance(tc.get("function"), dict) else {}
                    name = fn.get("name")
                    if not isinstance(name, str) or not name:
                        continue
                    args = fn.get("arguments", "{}")
                    if isinstance(args, dict):
                        args = json.dumps(args, ensure_ascii=False)
                    elif not isinstance(args, str):
                        args = str(args)
                    call_id, response_item_id = _split_responses_tool_id(tc.get("id"))
                    if not call_id and response_item_id.startswith("fc_"):
                        call_id = f"call_{response_item_id[len('fc_'):]}"
                    if not call_id:
                        call_id = f"call_{idx}"
                    items.append({
                        "type": "function_call",
                        "call_id": call_id,
                        "name": name,
                        "arguments": args or "{}",
                    })
            continue

        if role == "tool":
            call_id, _ = _split_responses_tool_id(msg.get("tool_call_id"))
            if not call_id:
                continue
            content = msg.get("content", "")
            if isinstance(content, list):
                output = _content_parts_for_responses(content, role="user") or ""
            else:
                output = "" if content is None else str(content)
            items.append({
                "type": "function_call_output",
                "call_id": call_id,
                "output": output,
            })

    return "\n\n".join(instructions), items


def _strip_schema_keywords(value: Any) -> Any:
    if isinstance(value, list):
        return [_strip_schema_keywords(item) for item in value]
    if not isinstance(value, dict):
        return value
    stripped: dict[str, Any] = {}
    for key, val in value.items():
        if key in {"pattern", "format"}:
            continue
        if key == "enum" and isinstance(val, list) and any("/" in str(x) for x in val):
            continue
        stripped[key] = _strip_schema_keywords(val)
    return stripped


def _responses_tools(tools: list[dict[str, Any]] | None) -> list[dict[str, Any]] | None:
    if not tools:
        return None
    converted: list[dict[str, Any]] = []
    for tool in tools:
        fn = tool.get("function") if isinstance(tool, dict) else None
        if not isinstance(fn, dict):
            continue
        name = fn.get("name")
        if not isinstance(name, str) or not name:
            continue
        params = fn.get("parameters") if isinstance(fn.get("parameters"), dict) else {
            "type": "object",
            "properties": {},
        }
        converted.append({
            "type": "function",
            "name": name,
            "description": str(fn.get("description") or ""),
            "strict": False,
            "parameters": _strip_schema_keywords(copy.deepcopy(params)),
        })
    return converted or None


def _extract_message_text(item: dict[str, Any]) -> str:
    content = item.get("content")
    if isinstance(content, str):
        return content
    parts: list[str] = []
    if isinstance(content, list):
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") in {"output_text", "text"}:
                text = part.get("text")
                if isinstance(text, str):
                    parts.append(text)
    return "".join(parts)


def _usage_from_response(data: dict[str, Any]) -> dict[str, int]:
    usage = data.get("usage")
    if not isinstance(usage, dict):
        return {}
    prompt = int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
    completion = int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)
    total = int(usage.get("total_tokens") or (prompt + completion))
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total,
    }


class XAIResponsesProvider(LLMProvider):
    """LLM provider for xAI's ``/v1/responses`` endpoint."""

    def __init__(
        self,
        api_key: str,
        api_base: str | None = None,
        default_model: str = DEFAULT_XAI_RESPONSES_MODEL,
        provider_name: str = "xai_oauth",
        client_id: str = "",
        request_timeout_seconds: float | None = None,
    ):
        base_url = validate_xai_oauth_base_url(api_base or DEFAULT_XAI_OAUTH_BASE_URL)
        super().__init__(api_key, base_url)
        self.default_model = _normalize_xai_model(default_model)
        self.provider_name = provider_name
        self._client_id = client_id
        # Precedence: env var > config (llmTimeoutSeconds) > 180s default.
        # Reasoning models can run long, hence the higher default than OpenRouter.
        _env = os.getenv("FLOWLY_LLM_TIMEOUT_SECONDS")
        self.request_timeout_seconds = (
            float(_env) if _env
            else float(request_timeout_seconds) if request_timeout_seconds
            else 180.0
        )

    def _redact(self, text: str) -> str:
        return redact_secret(text, self.api_key or "")

    def _error_response(self, exc: Exception) -> LLMResponse:
        message = self._redact(str(exc))
        logger.error("xAI Responses call error: {}", message)
        return LLMResponse(content=f"Error calling LLM: {message}", finish_reason="error")

    async def _refresh_api_key(self) -> None:
        payload = await _to_thread(refresh_tokens, client_id=self._client_id or None)
        creds = await _to_thread(resolve_runtime_credentials, force_refresh=False)
        if creds is None or not creds.api_key:
            raise XAIAuthError("xAI OAuth credentials are unavailable; run `flowly xai login`.")
        self.api_key = payload.access_token or creds.api_key
        self.api_base = validate_xai_oauth_base_url(payload.base_url or creds.base_url)

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
        resolved_model = _normalize_xai_model(model or self.default_model)
        instructions, input_items = _messages_to_responses_input(messages)
        payload: dict[str, Any] = {
            "model": resolved_model,
            "input": input_items,
            "store": False,
            "max_output_tokens": max_tokens,
            "temperature": temperature,
        }
        if instructions:
            payload["instructions"] = instructions
        response_tools = _responses_tools(tools)
        if response_tools:
            payload["tools"] = response_tools
            payload["tool_choice"] = tool_choice if tool_choice in {"auto", "required", "none"} else "auto"

        request_timeout = timeout or self.request_timeout_seconds
        for attempt in range(2):
            try:
                async with httpx.AsyncClient(timeout=request_timeout) as client:
                    response = await client.post(
                        f"{validate_xai_oauth_base_url(self.api_base)}/responses",
                        json=payload,
                        headers={
                            "Authorization": f"Bearer {self.api_key}",
                            "Content-Type": "application/json",
                            "Accept": "application/json",
                            "User-Agent": "flowly/xai-responses",
                        },
                    )
                # xAI's OAuth endpoint returns 403 (not 401) when the access
                # token has expired — so refresh + retry on BOTH before giving
                # up. Without this the running gateway can't self-heal a stale
                # token and only a restart (which refreshes at boot) recovers.
                # A genuine entitlement error still 403s after the refresh and
                # surfaces below on the second attempt.
                if response.status_code in (401, 403) and attempt == 0:
                    await self._refresh_api_key()
                    continue
                if response.status_code == 403:
                    raise XAIEntitlementError(
                        "xAI returned HTTP 403. The account is authenticated but "
                        "not entitled to this API/model."
                    )
                if response.status_code >= 400:
                    raise XAIAuthError(
                        f"xAI Responses HTTP {response.status_code}: "
                        f"{self._redact(response.text[:500])}"
                    )
                return self._parse_response(response.json())
            except Exception as exc:
                return self._error_response(exc)
        return LLMResponse(content="Error calling LLM: xAI OAuth refresh failed", finish_reason="error")

    def _parse_response(self, data: dict[str, Any]) -> LLMResponse:
        if not isinstance(data, dict):
            return LLMResponse(content="Error calling LLM: malformed xAI response", finish_reason="error")

        status = str(data.get("status") or "").lower()
        if status in {"failed", "cancelled"}:
            error = data.get("error")
            raise XAIAuthError(json.dumps(error, ensure_ascii=False) if error else f"xAI response {status}")

        content_parts: list[str] = []
        tool_calls: list[ToolCallRequest] = []
        output = data.get("output")
        if isinstance(output, list):
            for item in output:
                if not isinstance(item, dict):
                    continue
                item_type = item.get("type")
                if item_type == "message":
                    text = _extract_message_text(item)
                    if text:
                        content_parts.append(text)
                elif item_type in {"function_call", "custom_tool_call"}:
                    name = str(item.get("name") or "")
                    raw_args = item.get("arguments") if item_type == "function_call" else item.get("input")
                    if isinstance(raw_args, dict):
                        args = raw_args
                    else:
                        try:
                            args = json.loads(raw_args or "{}")
                        except json.JSONDecodeError:
                            args = {"raw": str(raw_args or "")}
                    call_id = str(item.get("call_id") or item.get("id") or f"call_{len(tool_calls)}")
                    tool_calls.append(ToolCallRequest(id=call_id, name=name, arguments=args))

        final_text = "\n".join(part for part in content_parts if part).strip()
        if not final_text and isinstance(data.get("output_text"), str):
            final_text = data["output_text"].strip()

        return LLMResponse(
            content=final_text or None,
            tool_calls=tool_calls,
            finish_reason="tool_calls" if tool_calls else ("length" if status == "incomplete" else "stop"),
            usage=_usage_from_response(data),
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
        """Real token-by-token streaming over the xAI Responses SSE API.

        Yields text-delta ``LLMResponse`` objects (``content`` set,
        ``finish_reason=""``) as tokens arrive, then ONE final response with
        ``finish_reason`` + any ``tool_calls`` + ``usage`` and ``content=None``
        (the text was already streamed). Matches the OpenRouter provider's
        contract that ``loop.py:_chat_with_stream`` consumes.

        Robust by construction: tool_calls + usage come from the authoritative
        ``response.completed`` payload (parsed exactly like the blocking path),
        and any streaming failure degrades to a single blocking ``chat()`` call
        so a turn is NEVER left without a reply.
        """
        resolved_model = _normalize_xai_model(model or self.default_model)
        instructions, input_items = _messages_to_responses_input(messages)
        payload: dict[str, Any] = {
            "model": resolved_model,
            "input": input_items,
            "store": False,
            "max_output_tokens": max_tokens,
            "temperature": temperature,
            "stream": True,
        }
        if instructions:
            payload["instructions"] = instructions
        response_tools = _responses_tools(tools)
        if response_tools:
            payload["tools"] = response_tools
            payload["tool_choice"] = tool_choice if tool_choice in {"auto", "required", "none"} else "auto"

        request_timeout = self.request_timeout_seconds
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "User-Agent": "flowly/xai-responses",
        }

        try:
            for attempt in range(2):
                streamed_text = False
                async with httpx.AsyncClient(timeout=request_timeout) as client:
                    async with client.stream(
                        "POST",
                        f"{validate_xai_oauth_base_url(self.api_base)}/responses",
                        json=payload,
                        headers={**headers, "Authorization": f"Bearer {self.api_key}"},
                    ) as response:
                        # 403 == expired OAuth token here (xAI quirk), so
                        # refresh + retry on both 401 and 403 before treating it
                        # as a hard entitlement error on the second attempt.
                        if response.status_code in (401, 403) and attempt == 0:
                            await response.aread()
                            await self._refresh_api_key()
                            continue
                        if response.status_code == 403:
                            await response.aread()
                            raise XAIEntitlementError(
                                "xAI returned HTTP 403. The account is authenticated but "
                                "not entitled to this API/model."
                            )
                        if response.status_code >= 400:
                            body = (await response.aread()).decode("utf-8", "replace")
                            raise XAIAuthError(
                                f"xAI Responses HTTP {response.status_code}: "
                                f"{self._redact(body[:500])}"
                            )

                        async for line in response.aiter_lines():
                            line = line.strip()
                            if not line or not line.startswith("data:"):
                                continue
                            data_str = line[len("data:"):].strip()
                            if not data_str or data_str == "[DONE]":
                                continue
                            try:
                                event = json.loads(data_str)
                            except json.JSONDecodeError:
                                continue
                            etype = str(event.get("type") or "")
                            if etype == "response.output_text.delta":
                                delta = event.get("delta")
                                if isinstance(delta, str) and delta:
                                    streamed_text = True
                                    yield LLMResponse(content=delta, finish_reason="")
                            elif etype in {"response.completed", "response.incomplete"}:
                                full = event.get("response")
                                parsed = (
                                    self._parse_response(full)
                                    if isinstance(full, dict)
                                    else LLMResponse(content=None, finish_reason="stop")
                                )
                                # If the model never emitted text deltas (some
                                # turns send only the final payload), stream the
                                # full text now so the user still sees the reply.
                                if not streamed_text and parsed.content:
                                    yield LLMResponse(content=parsed.content, finish_reason="")
                                yield LLMResponse(
                                    content=None,
                                    tool_calls=parsed.tool_calls,
                                    finish_reason=parsed.finish_reason,
                                    usage=parsed.usage,
                                )
                                return
                            elif etype in {"response.failed", "error"}:
                                err = (
                                    (event.get("response") or {}).get("error")
                                    or event.get("error")
                                    or event
                                )
                                raise XAIAuthError(json.dumps(err, ensure_ascii=False)[:500])

                        # Stream closed without a terminal ``response.completed``.
                        if streamed_text:
                            # Text already delivered — just close the turn.
                            yield LLMResponse(content=None, finish_reason="stop")
                            return
                        # Nothing usable parsed (unexpected event shape): don't
                        # leave the turn empty — fall back to the blocking call.
                        raise XAIAuthError("xAI stream closed without recognizable output")
        except Exception as exc:
            # Never leave a turn empty: fall back to the blocking Responses call
            # (the model still answers, just without live tokens this turn).
            logger.warning("xAI stream failed ({}); falling back to blocking call", self._redact(str(exc)))
            yield await self.chat(
                messages=messages,
                tools=tools,
                model=model,
                max_tokens=max_tokens,
                temperature=temperature,
                tool_choice=tool_choice,
            )

    def get_default_model(self) -> str:
        return self.default_model


async def _to_thread(fn, /, *args, **kwargs):
    import asyncio

    return await asyncio.to_thread(fn, *args, **kwargs)
