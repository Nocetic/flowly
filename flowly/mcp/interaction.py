"""Fail-closed MCP consent and forms, routed back to the actual calling loop.

Modern input-required responses retain their call's task context. Legacy
server-initiated requests have no reliable parent-call identifier, so legacy
calls are serialized while elicitation is enabled. No model-provided argument
or server-provided metadata can select another conversation's approval surface.
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import math
import re
import secrets
import time
from contextlib import asynccontextmanager
from typing import Any

from flowly.agent.tool_context import ToolCallOrigin, current_tool_origin
from flowly.mcp.content import mcp_attr
from flowly.mcp.security import sanitize_error

_call_id: contextvars.ContextVar[object | None] = contextvars.ContextVar("mcp_input_call", default=None)
_SENSITIVE = re.compile(r"password|passwd|secret|token|api.?key|credit.?card|private.?key", re.I)


def _label(value: Any, limit: int = 2000) -> str:
    return sanitize_error(str(value))[:limit]


class MCPInteraction:
    def __init__(self, name: str, config: dict) -> None:
        self.name = name
        settings = config.get("elicitation") or {}
        self.enabled = settings.get("enabled", True) is True
        try:
            timeout = float(settings.get("timeout", 300))
            self.timeout = min(600, max(0.01, timeout)) if math.isfinite(timeout) else 300
        except (ValueError, TypeError):
            self.timeout = 300
        self._active: dict[object, ToolCallOrigin | None] = {}
        self._legacy_lock = asyncio.Lock()
        self.metrics = {"requests": 0, "accepted": 0, "declined": 0, "cancelled": 0}

    @asynccontextmanager
    async def invocation(self, session: Any, origin: ToolCallOrigin | None):
        legacy = getattr(session, "discover_result", None) is None
        if legacy and self.enabled:
            await self._legacy_lock.acquire()
        key = object()
        token = _call_id.set(key)
        self._active[key] = origin
        try:
            yield
        finally:
            self._active.pop(key, None)
            _call_id.reset(token)
            if legacy and self.enabled:
                self._legacy_lock.release()

    def _owner(self) -> ToolCallOrigin | None:
        key = _call_id.get()
        if key in self._active:
            return self._active[key]
        # Legacy callbacks run in the transport receiver's context. Only one
        # in-flight call can own them; absent/ambiguous ownership declines.
        if len(self._active) == 1:
            return next(iter(self._active.values()))
        return None

    async def _on_owner(self, origin: ToolCallOrigin | None, factory: Any) -> Any:
        if origin is None or not origin.session_key or not origin.loop.is_running():
            return None
        coro = factory(origin.session_key)
        try:
            future = origin.context.copy().run(asyncio.run_coroutine_threadsafe, coro, origin.loop)
        except RuntimeError:
            coro.close()
            return None
        try:
            return await asyncio.wait_for(asyncio.wrap_future(future), self.timeout)
        finally:
            # Propagate MCP cancellation/timeout to the owning surface too.
            if not future.done():
                future.cancel()

    async def _consent(self, session_key: str, message: str, reasons: list[str]) -> bool:
        from flowly.exec.approval_manager import get_approval_manager
        from flowly.exec.types import ExecRequest, PendingApproval

        now = time.time()
        pending = PendingApproval(
            id="mcp_" + secrets.token_hex(12),
            request=ExecRequest(command=_label(message), session_key=session_key),
            session_key=session_key, created_at=now, expires_at=now + self.timeout,
            risk_reasons=[_label(reason) for reason in reasons],
            kind="action", supports_always=False,
        )
        decision = await get_approval_manager().request_and_wait(pending)
        return decision == "allow-once"

    async def authorize(self, tool_name: str, annotations: Any, arguments: dict) -> bool:
        if isinstance(annotations, dict) and annotations.get("readOnlyHint") is True:
            return True
        # Show what will be sent, without leaking secret-valued argument fields
        # into approval notifications, logs or push previews.
        def redact(value):
            if isinstance(value, dict):
                return {k: "[REDACTED]" if _SENSITIVE.search(str(k)) else redact(v)
                        for k, v in value.items()}
            if isinstance(value, list):
                return [redact(v) for v in value[:20]]
            return value

        async def ask(key):
            return await self._consent(key, f"Run MCP tool {self.name}/{tool_name}?", [
                "This server is configured as untrusted. The tool may modify external state.",
                _label(json.dumps(redact(arguments), ensure_ascii=False, default=str)),
            ])

        try:
            return bool(await self._on_owner(current_tool_origin(), ask))
        except asyncio.CancelledError:
            raise
        except Exception:
            return False

    @staticmethod
    def _form_schema(schema: Any) -> dict | None:
        """Accept only bounded flat non-sensitive forms; never resolve refs."""
        from jsonschema import Draft202012Validator

        if not isinstance(schema, dict) or len(json.dumps(schema)) > 16_000:
            return None
        props = schema.get("properties", {})
        if schema.get("type") != "object" or not isinstance(props, dict) or len(props) > 16:
            return None
        if any(key not in {"type", "properties", "required", "title", "description", "additionalProperties"}
               for key in schema):
            return None
        allowed = {"type", "title", "description", "enum", "enumNames", "default", "format",
                   "minLength", "maxLength", "minimum", "maximum"}
        for name, spec in props.items():
            if (_SENSITIVE.search(name) or not isinstance(spec, dict)
                    or spec.get("type") not in {"string", "integer", "number", "boolean"}
                    or any(key not in allowed for key in spec)
                    or spec.get("format") == "password"):
                return None
        try:
            Draft202012Validator.check_schema(schema)
        except Exception:
            return None
        return schema

    async def _form(self, session_key: str, message: str, schema: dict) -> dict | None:
        from jsonschema import Draft202012Validator, FormatChecker

        from flowly.clarify.manager import get_clarify_manager
        from flowly.clarify.types import ClarifyRequest

        props = schema.get("properties", {})
        if not await self._consent(session_key, f"MCP server {self.name}: {message}", [
            "Share the requested information with this MCP server?",
            "Requested fields: " + ", ".join(props),
        ]):
            return None
        result = {}
        for name, spec in props.items():
            required = name in schema.get("required", [])
            for attempt in range(2):
                now = time.time()
                choices = spec.get("enum")
                if spec["type"] == "boolean" and choices is None:
                    choices = [True, False]
                labels = ([json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value
                           for value in choices] if isinstance(choices, list) else [])
                pending = ClarifyRequest(
                    id="mcp_form_" + secrets.token_hex(12), session_key=session_key,
                    question=_label(f"{message}\nMCP {self.name} — {name}: {spec.get('description', '')}\n"
                                    + ("Required." if required else "Optional: reply /skip to omit.")
                                    + (" Previous answer did not satisfy the field constraints." if attempt else "")
                                    + f"\nConstraints: {json.dumps(spec, ensure_ascii=False)}"),
                    choices=labels if 0 < len(labels) <= 4 else None,
                    created_at=now, expires_at=now + self.timeout,
                )
                answer = await get_clarify_manager().request_and_wait(pending)
                if answer is None:
                    return None
                if answer == "/skip" and not required:
                    break
                try:
                    if len(answer) > 16_000:
                        raise ValueError("Form answer too long")
                    value = answer if spec["type"] == "string" else json.loads(answer)
                    if isinstance(value, float) and not math.isfinite(value):
                        raise ValueError("Form numbers must be finite")
                    Draft202012Validator(spec, format_checker=FormatChecker()).validate(value)
                    result[name] = value
                    break
                except (ValueError, TypeError):
                    if attempt:
                        return None
                except Exception:
                    if attempt:
                        return None
        if not Draft202012Validator(schema, format_checker=FormatChecker()).is_valid(result):
            return None
        return result

    async def elicit(self, _context: Any, params: Any) -> Any:
        from mcp import types

        self.metrics["requests"] += 1
        action, content = "decline", None
        try:
            if self.enabled and getattr(params, "mode", "form") == "form":
                schema = self._form_schema(mcp_attr(params, "requested_schema", "requestedSchema"))
                if schema is not None:
                    content = await self._on_owner(self._owner(), lambda key: self._form(
                        key, _label(getattr(params, "message", "")), schema,
                    ))
                    if content is not None:
                        action = "accept"
        except asyncio.CancelledError:
            self.metrics["cancelled"] += 1
            raise
        except asyncio.TimeoutError:
            action = "cancel"
        except Exception:
            action = "decline"
        self.metrics[{"accept": "accepted", "decline": "declined", "cancel": "cancelled"}[action]] += 1
        return types.ElicitResult(action=action, **({"content": content} if action == "accept" else {}))
