"""ChatGPT Codex Responses provider — access via a ChatGPT subscription.

Speaks the ``/responses`` wire protocol against
``chatgpt.com/backend-api/codex/responses``, authenticated by an OpenAI
Codex OAuth access token (``flowly codex login``). Usage is billed against
the user's ChatGPT plan rather than the metered OpenAI API.

Protocol notes (proven against the live backend by ChatMock / LiteLLM /
opencode's Codex plugins):

* ``store: false`` and ``stream: true`` are mandatory. The blocking
  :meth:`chat` therefore opens the SSE stream and aggregates it into one
  ``LLMResponse``; :meth:`chat_stream` yields token deltas live.
* The system prompt goes into ``input`` as a ``developer`` message; the
  ``instructions`` field carries the Codex base preamble the backend
  expects (override with ``FLOWLY_CODEX_INSTRUCTIONS``).
* Item ids are stripped (stateless mode); reasoning is requested via
  ``include: ["reasoning.encrypted_content"]`` + a ``reasoning`` block.
* ``max_output_tokens`` / ``temperature`` / ``metadata`` are rejected by
  this backend and are never sent.
* Auth: 401 → refresh the OAuth token once and retry. The
  ``ChatGPT-Account-Id`` header is required alongside the bearer.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
from typing import Any, AsyncIterator

import httpx
from loguru import logger

from flowly.auth.openai_codex import (
    CODEX_RESPONSES_URL,
    CodexAuthError,
    CodexEntitlementError,
    redact_secret,
    resolve_runtime_credentials,
)
from flowly.providers.base import LLMProvider, LLMResponse, ToolCallRequest

# Keep the startup/provider-switch default aligned with the first visible
# model in the live ChatGPT Codex catalogue.
DEFAULT_CODEX_MODEL = os.getenv("FLOWLY_CODEX_MODEL", "gpt-5.6-sol")

# Reasoning efforts the ChatGPT Codex backend accepts. "minimal" is NOT
# accepted here (unlike the metered API) — it is folded down to "low".
_VALID_EFFORTS = ("none", "low", "medium", "high", "xhigh", "max")
_DEFAULT_EFFORT = "medium"


def _strip_known_prefixes(model: str) -> str:
    for prefix in ("openai/", "chatgpt/", "codex/"):
        if model.startswith(prefix):
            return model[len(prefix):]
    return model


def _split_effort_suffix(model: str) -> tuple[str, str | None]:
    """Split a trailing ``:effort`` / ``-effort`` off a model id."""
    value = model.strip()
    if not value:
        return value, None
    if ":" in value:
        base, maybe = value.rsplit(":", 1)
        if maybe.lower() in _VALID_EFFORTS:
            return base, maybe.lower()
    for sep in ("-", "_"):
        for effort in _VALID_EFFORTS:
            suffix = f"{sep}{effort}"
            if value.lower().endswith(suffix):
                return value[: -len(suffix)], effort
    return value, None


def _normalize_codex_model(model: str | None) -> str:
    candidate = _strip_known_prefixes((model or DEFAULT_CODEX_MODEL).strip())
    base, _ = _split_effort_suffix(candidate)
    base = base.strip()
    if not base or "/" in base:
        return DEFAULT_CODEX_MODEL
    return base


def _resolve_effort(model: str | None, override: str | None = None) -> str:
    _, suffix_effort = _split_effort_suffix(_strip_known_prefixes((model or "").strip()))
    effort = (override or suffix_effort or _DEFAULT_EFFORT).strip().lower()
    if effort == "minimal":
        effort = "low"
    if effort not in _VALID_EFFORTS:
        effort = _DEFAULT_EFFORT
    normalized = _normalize_codex_model(model)
    low_model = normalized.lower()
    requires_reasoning = "codex" in low_model or low_model.startswith("gpt-5.6")
    # Codex-family and GPT-5.6 agentic models reject "none"; bump it to low.
    if effort == "none" and requires_reasoning:
        effort = "low"
    # xhigh/max only exist on newer models; keep them and let the backend
    # reject an explicitly requested unsupported combination.
    return effort


def _content_parts(content: Any, *, role: str) -> list[dict[str, Any]]:
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


def _split_tool_id(raw_id: Any) -> str:
    if not isinstance(raw_id, str):
        return ""
    return raw_id.strip()


def _messages_to_codex_input(
    messages: list[dict[str, Any]],
) -> tuple[str, list[dict[str, Any]]]:
    """Convert flat chat messages to Codex Responses ``input`` items.

    Returns ``(instructions_text, input_items)``. System messages are joined
    into the ``instructions`` field (the Responses API's system prompt slot) —
    this carries Flowly's persona intact, the same way every other Flowly
    provider treats system messages. Item ids are not emitted — the backend
    runs stateless (``store: false``).
    """
    instruction_chunks: list[str] = []
    items: list[dict[str, Any]] = []

    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")

        if role in {"system", "developer"}:
            content = msg.get("content")
            if isinstance(content, str) and content.strip():
                instruction_chunks.append(content.strip())
            elif isinstance(content, list):
                for part in _content_parts(content, role="user"):
                    if part.get("type") == "input_text":
                        instruction_chunks.append(str(part.get("text") or ""))
            continue

        if role in {"user", "assistant"}:
            content = msg.get("content", "")
            if isinstance(content, list):
                parts = _content_parts(content, role=role)
                items.append({"role": role, "content": parts if parts else ""})
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
                    call_id = _split_tool_id(tc.get("id")) or f"call_{idx}"
                    items.append({
                        "type": "function_call",
                        "call_id": call_id,
                        "name": name,
                        "arguments": args or "{}",
                    })
            continue

        if role == "tool":
            call_id = _split_tool_id(msg.get("tool_call_id"))
            if not call_id:
                continue
            content = msg.get("content", "")
            if isinstance(content, list):
                texts = [
                    str(p.get("text") or "")
                    for p in _content_parts(content, role="user")
                    if p.get("type") == "input_text"
                ]
                output = "\n".join(t for t in texts if t)
            else:
                output = "" if content is None else str(content)
            items.append({
                "type": "function_call_output",
                "call_id": call_id,
                "output": output,
            })

    instructions_text = "\n\n".join(c for c in instruction_chunks if c)
    return instructions_text, items


def _strip_schema_keywords(value: Any) -> Any:
    if isinstance(value, list):
        return [_strip_schema_keywords(item) for item in value]
    if not isinstance(value, dict):
        return value
    stripped: dict[str, Any] = {}
    for key, val in value.items():
        if key in {"pattern", "format"}:
            continue
        stripped[key] = _strip_schema_keywords(val)
    return stripped


def _codex_tools(tools: list[dict[str, Any]] | None) -> list[dict[str, Any]] | None:
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


def _session_id_for(instructions_text: str, input_items: list[dict[str, Any]]) -> str:
    """Stable prompt-cache key for a conversation.

    Derived from the instructions + first user message so repeated turns of
    the same conversation share a cache key without needing server state.
    """
    seed = instructions_text
    for item in input_items:
        if item.get("role") == "user":
            content = item.get("content")
            seed += json.dumps(content, ensure_ascii=False, sort_keys=True) if content else ""
            break
    return hashlib.sha256(seed.encode("utf-8", "ignore")).hexdigest()[:32]


class CodexResponsesProvider(LLMProvider):
    """LLM provider for the ChatGPT Codex ``/responses`` endpoint."""

    def __init__(
        self,
        api_key: str,
        account_id: str,
        api_base: str | None = None,
        default_model: str = DEFAULT_CODEX_MODEL,
        provider_name: str = "openai_codex",
        request_timeout_seconds: float | None = None,
    ):
        super().__init__(api_key, api_base or CODEX_RESPONSES_URL)
        self.account_id = account_id
        self.default_model = _normalize_codex_model(default_model)
        self.provider_name = provider_name
        # Precedence: env var > config (llmTimeoutSeconds) > 180s default.
        # Reasoning models can run long, hence the higher default.
        _env = os.getenv("FLOWLY_LLM_TIMEOUT_SECONDS")
        self.request_timeout_seconds = (
            float(_env) if _env
            else float(request_timeout_seconds) if request_timeout_seconds
            else 180.0
        )

    # ── helpers ──────────────────────────────────────────────────────

    def _redact(self, text: str) -> str:
        return redact_secret(text, self.api_key or "")

    def _error_response(self, exc: Exception) -> LLMResponse:
        message = self._redact(str(exc))
        logger.error("Codex Responses call error: {}", message)
        return LLMResponse(content=f"Error calling LLM: {message}", finish_reason="error")

    def _instructions(self, from_messages: str) -> str:
        # Flowly's system prompt (extracted from messages) is the system
        # prompt. Fall back to a minimal preamble only when a caller sends no
        # system message at all, or to a full override via env var.
        override = os.getenv("FLOWLY_CODEX_INSTRUCTIONS")
        if override and override.strip():
            return override
        if from_messages and from_messages.strip():
            return from_messages
        return CODEX_BASE_INSTRUCTIONS

    async def _refresh_api_key(self) -> None:
        creds = await _to_thread(resolve_runtime_credentials, force_refresh=True)
        if creds is None or not creds.api_key or not creds.account_id:
            raise CodexAuthError("Codex OAuth credentials are unavailable; run `flowly codex login`.")
        self.api_key = creds.api_key
        self.account_id = creds.account_id

    def _build_payload(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        model: str | None,
        tool_choice: str,
        *,
        stream: bool,
    ) -> tuple[dict[str, Any], str]:
        resolved_model = _normalize_codex_model(model or self.default_model)
        instructions_text, input_items = _messages_to_codex_input(messages)
        session_id = _session_id_for(instructions_text, input_items)
        payload: dict[str, Any] = {
            "model": resolved_model,
            "instructions": self._instructions(instructions_text),
            "input": input_items,
            "store": False,
            "stream": stream,
            "prompt_cache_key": session_id,
            "reasoning": {"effort": _resolve_effort(model or self.default_model), "summary": "auto"},
            "include": ["reasoning.encrypted_content"],
        }
        response_tools = _codex_tools(tools)
        if response_tools:
            payload["tools"] = response_tools
            payload["tool_choice"] = (
                tool_choice if tool_choice in {"auto", "none", "required"} else "auto"
            )
            payload["parallel_tool_calls"] = False
        return payload, session_id

    def _headers(self, session_id: str, *, accept: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": accept,
            "ChatGPT-Account-Id": self.account_id,
            "OpenAI-Beta": "responses=experimental",
            "originator": "flowly",
            "session_id": session_id,
            "User-Agent": "flowly-codex",
        }

    # ── blocking chat ────────────────────────────────────────────────

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
        # This backend rejects max_output_tokens / temperature — accepted for
        # interface parity, deliberately not forwarded.
        del max_tokens, temperature, purpose
        payload, session_id = self._build_payload(
            messages, tools, model, tool_choice, stream=True
        )
        request_timeout = timeout or self.request_timeout_seconds
        try:
            for attempt in range(2):
                async with httpx.AsyncClient(timeout=request_timeout) as client:
                    async with client.stream(
                        "POST",
                        CODEX_RESPONSES_URL,
                        json=payload,
                        headers=self._headers(session_id, accept="text/event-stream"),
                    ) as response:
                        if response.status_code == 401 and attempt == 0:
                            await response.aread()
                            await self._refresh_api_key()
                            continue
                        if response.status_code == 403:
                            await response.aread()
                            raise CodexEntitlementError(
                                "ChatGPT returned HTTP 403. The account is authenticated "
                                "but this plan can't use Codex / this model."
                            )
                        if response.status_code >= 400:
                            body = (await response.aread()).decode("utf-8", "replace")
                            raise CodexAuthError(
                                f"Codex Responses HTTP {response.status_code}: "
                                f"{self._redact(body[:500])}"
                            )
                        return await self._aggregate_stream(response)
        except Exception as exc:
            return self._error_response(exc)
        return LLMResponse(
            content="Error calling LLM: Codex OAuth refresh failed", finish_reason="error"
        )

    async def _aggregate_stream(self, response: httpx.Response) -> LLMResponse:
        """Collect an SSE stream into one final LLMResponse (blocking path).

        The ChatGPT Codex backend streams complete output items in
        ``response.output_item.done`` and leaves ``response.completed``'s
        ``output`` empty, so items are collected from the stream rather than
        the final payload.
        """
        collected_items: list[dict[str, Any]] = []
        text_parts: list[str] = []
        usage: dict[str, Any] = {}
        status = "completed"
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
                if isinstance(delta, str):
                    text_parts.append(delta)
            elif etype == "response.output_item.done":
                item = event.get("item")
                if isinstance(item, dict):
                    collected_items.append(item)
            elif etype in {"response.completed", "response.incomplete"}:
                full = event.get("response")
                if isinstance(full, dict):
                    usage = full.get("usage") if isinstance(full.get("usage"), dict) else {}
                    status = str(full.get("status") or status)
                    # Some deployments DO populate output — prefer it if the
                    # stream didn't surface items.
                    if not collected_items and isinstance(full.get("output"), list):
                        collected_items = [i for i in full["output"] if isinstance(i, dict)]
                if etype == "response.incomplete":
                    status = "incomplete"
            elif etype in {"response.failed", "error"}:
                err = (
                    (event.get("response") or {}).get("error")
                    or event.get("error")
                    or event
                )
                raise CodexAuthError(json.dumps(err, ensure_ascii=False)[:500])
        final = self._parse_response(
            {"output": collected_items, "usage": usage, "status": status}
        )
        if not final.content and text_parts:
            final.content = "".join(text_parts).strip() or None
        return final

    def _parse_response(self, data: dict[str, Any]) -> LLMResponse:
        if not isinstance(data, dict):
            return LLMResponse(
                content="Error calling LLM: malformed Codex response", finish_reason="error"
            )
        status = str(data.get("status") or "").lower()
        if status in {"failed", "cancelled"}:
            error = data.get("error")
            raise CodexAuthError(
                json.dumps(error, ensure_ascii=False) if error else f"Codex response {status}"
            )

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
                    raw_args = (
                        item.get("arguments") if item_type == "function_call"
                        else item.get("input")
                    )
                    if isinstance(raw_args, dict):
                        args = raw_args
                    else:
                        try:
                            args = json.loads(raw_args or "{}")
                        except json.JSONDecodeError:
                            args = {"raw": str(raw_args or "")}
                    call_id = str(item.get("call_id") or item.get("id") or f"call_{len(tool_calls)}")
                    tool_calls.append(ToolCallRequest(id=call_id, name=name, arguments=args))

        final_text = "\n".join(p for p in content_parts if p).strip()
        if not final_text and isinstance(data.get("output_text"), str):
            final_text = data["output_text"].strip()

        return LLMResponse(
            content=final_text or None,
            tool_calls=tool_calls,
            finish_reason="tool_calls" if tool_calls else ("length" if status == "incomplete" else "stop"),
            usage=_usage_from_response(data),
        )

    # ── streaming chat ───────────────────────────────────────────────

    async def chat_stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        tool_choice: str = "auto",
    ) -> AsyncIterator[LLMResponse]:
        """Token-by-token streaming over the ChatGPT Codex SSE API.

        Yields text-delta responses (``content`` set, ``finish_reason=""``),
        then ONE final response with ``finish_reason`` + ``tool_calls`` +
        ``usage`` and ``content=None``. Any failure degrades to a single
        blocking :meth:`chat` so a turn is never left empty.
        """
        del max_tokens, temperature
        payload, session_id = self._build_payload(
            messages, tools, model, tool_choice, stream=True
        )
        request_timeout = self.request_timeout_seconds
        try:
            for attempt in range(2):
                streamed_text = False
                async with httpx.AsyncClient(timeout=request_timeout) as client:
                    async with client.stream(
                        "POST",
                        CODEX_RESPONSES_URL,
                        json=payload,
                        headers=self._headers(session_id, accept="text/event-stream"),
                    ) as response:
                        if response.status_code == 401 and attempt == 0:
                            await response.aread()
                            await self._refresh_api_key()
                            continue
                        if response.status_code == 403:
                            await response.aread()
                            raise CodexEntitlementError(
                                "ChatGPT returned HTTP 403. The account is authenticated "
                                "but this plan can't use Codex / this model."
                            )
                        if response.status_code >= 400:
                            body = (await response.aread()).decode("utf-8", "replace")
                            raise CodexAuthError(
                                f"Codex Responses HTTP {response.status_code}: "
                                f"{self._redact(body[:500])}"
                            )

                        collected_items: list[dict[str, Any]] = []
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
                            elif etype == "response.output_item.done":
                                item = event.get("item")
                                if isinstance(item, dict):
                                    collected_items.append(item)
                            elif etype in {"response.completed", "response.incomplete"}:
                                full = event.get("response") if isinstance(event.get("response"), dict) else {}
                                usage = full.get("usage") if isinstance(full.get("usage"), dict) else {}
                                status = str(full.get("status") or "completed")
                                if etype == "response.incomplete":
                                    status = "incomplete"
                                if not collected_items and isinstance(full.get("output"), list):
                                    collected_items = [i for i in full["output"] if isinstance(i, dict)]
                                parsed = self._parse_response(
                                    {"output": collected_items, "usage": usage, "status": status}
                                )
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
                                raise CodexAuthError(json.dumps(err, ensure_ascii=False)[:500])

                        if streamed_text or collected_items:
                            parsed = self._parse_response(
                                {"output": collected_items, "usage": {}, "status": "completed"}
                            )
                            yield LLMResponse(
                                content=None,
                                tool_calls=parsed.tool_calls,
                                finish_reason=parsed.finish_reason,
                                usage=parsed.usage,
                            )
                            return
                        raise CodexAuthError("Codex stream closed without recognizable output")
        except Exception as exc:
            logger.warning(
                "Codex stream failed ({}); falling back to blocking call", self._redact(str(exc))
            )
            yield await self.chat(
                messages=messages,
                tools=tools,
                model=model,
                tool_choice=tool_choice,
            )

    def get_default_model(self) -> str:
        return self.default_model


async def _to_thread(fn, /, *args, **kwargs):
    import asyncio

    return await asyncio.to_thread(fn, *args, **kwargs)


# Minimal fallback used only when a caller sends no system message at all
# (Flowly always sends one, so this is a safety net). Flowly's real persona
# arrives as the ``instructions`` field, extracted from the system message.
CODEX_BASE_INSTRUCTIONS = (
    "You are a helpful, general-purpose agent running on a user's computer. "
    "Be concise, accurate, and helpful. When searching for text or files, prefer "
    "`rg`. Default to ASCII when editing files unless the file already uses "
    "Unicode. Do not run destructive commands unless explicitly requested."
)
