"""Session-scoped access to curated tools in the owning live runtime.

Opaque grants carry authority separately from model-authored arguments. This
is a protocol boundary, not an OS sandbox for a client that can read all of
the user's files. No alternate Board store, provider, or agent loop is built.
"""

from __future__ import annotations

import asyncio
import base64
import contextvars
import copy
import hashlib
import json
import mimetypes
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from jsonschema.validators import validator_for
from referencing import Registry

READ_TOOLS = frozenset({
    "web_search", "web_fetch", "web_extract", "video_analyze", "image_analyze",
    "skill_view", "skills_list", "board_list", "board_get",
})
WRITE_TOOLS = frozenset({"image_generate", "voice_generate", "board_add", "board_update", "board_run"})
MEDIA_TOOLS = frozenset({"image_generate", "voice_generate"})
MAX_ARGUMENT_BYTES = 12 * 1024 * 1024
MAX_RESULT_BYTES = 16 * 1024 * 1024
MAX_INLINE_MEDIA_BYTES = 8 * 1024 * 1024
MAX_PENDING_CALLS = 64
MAX_GRANT_PENDING_CALLS = 8
MAX_PENDING_ARGUMENT_BYTES = 32 * 1024 * 1024


class ToolBridgeError(RuntimeError):
    """Safe client-facing failure without secrets or model argument values."""


@dataclass
class _Grant:
    session_key: str
    names: frozenset[str]
    context: contextvars.Context
    expires_at: float
    allow_writes: bool = False
    authority_check: Callable[[], bool] | None = None
    audit_id: str = field(default_factory=lambda: secrets.token_hex(16))
    pending: int = 0
    active: bool = True
    timer: Any = None
    semaphore: asyncio.Semaphore = field(default_factory=lambda: asyncio.Semaphore(4))
    calls: dict[str, tuple[str, asyncio.Task | None]] = field(default_factory=dict)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def _error(message: str) -> dict:
    return {"content": [{"type": "text", "text": message}], "isError": True}


def _validate_arguments(tool: Any, arguments: dict) -> None:
    schema = tool.parameters
    try:
        cls = validator_for(schema)
        cls.check_schema(schema)
        # No network retrieval of schema references during validation.
        validator = cls(schema, registry=Registry())
        if next(validator.iter_errors(arguments), None) is not None:
            raise ToolBridgeError(f"Arguments do not match the schema for '{tool.name}'")
    except ToolBridgeError:
        raise
    except Exception:
        raise ToolBridgeError(f"Cannot validate the schema for '{tool.name}'") from None


class RuntimeToolBridge:
    def __init__(self, owner: Any):
        self.owner = owner
        self._grants: dict[str, _Grant] = {}
        self._tasks: set[asyncio.Task] = set()
        self._parallel = asyncio.Semaphore(16)
        self._pending = 0
        self._pending_bytes = 0
        self._closed = False

    def _route(self, session_key: str) -> dict:
        platform = session_key.split(":", 1)[0]
        enabled, disabled = self.owner._resolve_toolset_route(platform)
        return {"platform": platform, "enabled_toolsets": enabled, "disabled_toolsets": disabled}

    def _available(self, grant: _Grant, name: str) -> bool:
        return (
            not self._closed and grant.active and time.monotonic() < grant.expires_at
            and (grant.authority_check is None or grant.authority_check())
            and name in grant.names
            and self._permitted(self.owner.tools.get(name), allow_writes=grant.allow_writes)
            and self.owner.tools.is_available(name, **self._route(grant.session_key))
        )

    @staticmethod
    def _permitted(tool: Any, *, allow_writes: bool) -> bool:
        from flowly.mcp.tool import MCPTool, _MCPUtilityTool

        if tool is None:
            return False
        if isinstance(tool, MCPTool):
            # Preserve configured MCP integrations through the owning client,
            # including its consent/OAuth/transport policy, not a second connection.
            return allow_writes or (tool.annotations or {}).get("readOnlyHint") is True
        if isinstance(tool, _MCPUtilityTool):
            return True
        return tool.name in READ_TOOLS or (allow_writes and tool.name in WRITE_TOOLS)

    def create_grant(
        self, session_key: str, *, names: list[str] | None = None,
        allow_writes: bool = False, ttl: float = 3600,
        context: contextvars.Context | None = None,
        allow_empty: bool = False,
        authority_check: Callable[[], bool] | None = None,
    ) -> dict:
        if self._closed:
            raise ToolBridgeError("Tool runtime is stopped")
        if not isinstance(session_key, str) or not 1 <= len(session_key) <= 512 or ":" not in session_key:
            raise ToolBridgeError("An exact Flowly session key is required")
        if not isinstance(ttl, (int, float)) or isinstance(ttl, bool) or not 1 <= ttl <= 28800:
            raise ToolBridgeError("Grant duration must be between 1 and 28800 seconds")
        if not isinstance(allow_writes, bool):
            raise ToolBridgeError("allow_writes must be a boolean")
        if names is not None and (
            not isinstance(names, list) or len(names) > 64
            or any(not isinstance(name, str) for name in names)
        ):
            raise ToolBridgeError("tools must be a list of at most 64 names")
        if len(self._grants) >= 128:
            raise ToolBridgeError("Too many active tool grants")
        permitted = frozenset(name for name in self.owner.tools.tool_names if self._permitted(
            self.owner.tools.get(name), allow_writes=allow_writes,
        ))
        selected = frozenset(names) if names is not None else permitted
        if not selected <= permitted:
            raise ToolBridgeError("Requested tools exceed the permitted bridge capabilities")
        selected &= frozenset(self.owner.tools.get_available_names(**self._route(session_key)))
        from flowly.agent.tool_context import current_tool_origin

        origin = current_tool_origin()
        if origin is not None:
            if origin.session_key != session_key:
                raise ToolBridgeError("A delegated grant cannot change its owning session")
            if origin.allowed_tools is not None:
                selected &= origin.allowed_tools
        if not selected and not allow_empty:
            raise ToolBridgeError("No requested tools are available in this session")
        token = secrets.token_urlsafe(32)
        digest = hashlib.sha256(token.encode()).hexdigest()
        grant = _Grant(
            session_key, selected, context if context is not None else contextvars.copy_context(),
            time.monotonic() + ttl, allow_writes=allow_writes,
            authority_check=authority_check,
        )
        self._grants[digest] = grant
        grant.timer = asyncio.get_running_loop().call_later(ttl, self._revoke_digest, digest)
        return {"token": token, "session_key": session_key, "tools": sorted(selected), "ttl_seconds": ttl}

    def _grant(self, token: str) -> _Grant:
        if not isinstance(token, str) or not 32 <= len(token) <= 256:
            raise ToolBridgeError("Invalid or expired tool grant")
        grant = self._grants.get(hashlib.sha256(token.encode()).hexdigest())
        if grant is None or not grant.active or time.monotonic() >= grant.expires_at:
            raise ToolBridgeError("Invalid or expired tool grant")
        return grant

    def _revoke_digest(self, digest: str) -> None:
        grant = self._grants.pop(digest, None)
        if grant is None:
            return
        grant.active = False
        grant.timer.cancel()
        for _, task in grant.calls.values():
            if task is not None and not task.done():
                task.cancel()

    def revoke(self, token: str) -> list[asyncio.Task]:
        grant = self._grant(token)
        tasks = [task for _, task in grant.calls.values() if task is not None]
        self._revoke_digest(hashlib.sha256(token.encode()).hexdigest())
        return tasks

    def revoke_session(self, session_key: str) -> list[asyncio.Task]:
        """Revoke before deleting the owner; return calls for the owner to drain."""
        tasks = []
        for digest, grant in list(self._grants.items()):
            if grant.session_key == session_key:
                tasks.extend(task for _, task in grant.calls.values() if task is not None)
                self._revoke_digest(digest)
        return tasks

    def stop(self) -> None:
        """Immediately withdraw authority even before gateway shutdown awaits."""
        self._closed = True
        for digest in list(self._grants):
            self._revoke_digest(digest)

    async def close(self) -> None:
        self.stop()
        await asyncio.gather(*list(self._tasks), return_exceptions=True)

    def list_tools(self, token: str) -> list[dict]:
        grant = self._grant(token)
        return self._definitions(grant)

    def available_tools(self, session_key: str) -> list[dict]:
        """Owner-only capability preview; it issues no executable grant."""
        return self._definitions(_Grant(
            session_key, frozenset(self.owner.tools.tool_names), contextvars.copy_context(),
            time.monotonic() + 1, allow_writes=True,
        ))

    def _definitions(self, grant: _Grant) -> list[dict]:
        definitions = []
        for name in sorted(grant.names):
            if not self._available(grant, name):
                continue
            tool = self.owner.tools.get(name)
            definition = {
                "name": name, "description": tool.description, "inputSchema": copy.deepcopy(tool.parameters),
                "annotations": {
                    "readOnlyHint": name in READ_TOOLS, "destructiveHint": name == "board_update",
                    "idempotentHint": name in READ_TOOLS,
                    "openWorldHint": name == "board_run" or not name.startswith("board_"),
                },
                "_meta": {"source": "flowly", "stateless": False},
            }
            output = getattr(tool, "output_schema", None)
            if isinstance(output, dict):
                definition["outputSchema"] = copy.deepcopy(output)
            annotations = getattr(tool, "annotations", None)
            if isinstance(annotations, dict):
                definition["annotations"] = copy.deepcopy(annotations)
            definitions.append(definition)
        return definitions

    def cancel(self, token: str, request_id: str) -> None:
        grant = self._grant(token)
        record = grant.calls.get(request_id)
        if record and record[1] is not None:
            record[1].cancel()

    async def call(self, token: str, request_id: str, name: str, arguments: dict) -> dict:
        grant = self._grant(token)
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= 128:
            raise ToolBridgeError("A bounded request_id is required")
        if not isinstance(name, str) or not self._available(grant, name):
            raise ToolBridgeError("Tool is not permitted or is no longer available")
        if not isinstance(arguments, dict):
            raise ToolBridgeError("Arguments must be an object")
        try:
            serialized = _json([name, arguments])
        except (ValueError, TypeError, RecursionError):
            raise ToolBridgeError("Arguments must be finite JSON values") from None
        encoded = serialized.encode()
        argument_bytes = len(encoded)
        if argument_bytes > MAX_ARGUMENT_BYTES:
            raise ToolBridgeError("Tool arguments exceed the size limit")
        fingerprint = hashlib.sha256(encoded).hexdigest()
        del serialized, encoded
        existing = grant.calls.get(request_id)
        if existing:
            if existing[0] != fingerprint:
                raise ToolBridgeError("request_id was already used with different arguments")
            if existing[1] is None:
                raise ToolBridgeError("Request already finished; it will not be replayed")
            return await asyncio.shield(existing[1])
        # Finished IDs remain as tombstones for the grant lifetime. No silent
        # eviction that could execute a duplicated write after a lost reply.
        if len(grant.calls) >= 1024:
            raise ToolBridgeError("Tool grant request limit reached; open a new grant")
        if (
            self._pending >= MAX_PENDING_CALLS or grant.pending >= MAX_GRANT_PENDING_CALLS
            or self._pending_bytes + argument_bytes > MAX_PENDING_ARGUMENT_BYTES
        ):
            raise ToolBridgeError("Tool bridge is at capacity; wait for pending calls to finish")
        task = asyncio.create_task(
            self._execute(grant, request_id, name, copy.deepcopy(arguments)),
            context=grant.context.copy(),
        )
        grant.calls[request_id] = (fingerprint, task)
        self._tasks.add(task)
        grant.pending += 1
        self._pending += 1
        self._pending_bytes += argument_bytes

        def finished(done):
            self._tasks.discard(done)
            grant.pending -= 1
            self._pending -= 1
            self._pending_bytes -= argument_bytes
            grant.calls[request_id] = (fingerprint, None)
            if not done.cancelled():
                done.exception()  # A disconnected client must not leave an unobserved exception.

        task.add_done_callback(finished)
        return await asyncio.shield(task)

    def _read_roots(self) -> tuple[Path, ...]:
        from flowly.profile import get_flowly_home

        workspace = Path(self.owner.workspace).resolve()
        return (workspace, (get_flowly_home() / "media").resolve())

    def _safe_file(self, value: str) -> Path:
        try:
            path = Path(value.removeprefix("file://")).expanduser().resolve(strict=True)
        except (OSError, ValueError, RuntimeError):
            # Neither a server traceback nor the supplied private path belongs
            # in the public HTTP/MCP failure. Includes missing files, NUL bytes,
            # unavailable home expansion and symlink loops on older Python.
            raise ToolBridgeError("Media file does not exist or cannot be safely accessed") from None
        if not path.is_file() or not any(path.is_relative_to(root) for root in self._read_roots()):
            raise ToolBridgeError("Media path is outside this runtime's workspace and media directory")
        return path

    @staticmethod
    def _tool_secrets(tool: Any) -> tuple[str, ...]:
        # Only fields on the already-selected tool/provider. Do not instantiate
        # providers, call configuration getters, or scan unrelated accounts.
        values = []
        for source in (tool, getattr(tool, "provider", None), getattr(tool, "_elevenlabs", None)):
            for key in ("api_key", "_api_key", "_auth_token"):
                value = getattr(source, key, None)
                if isinstance(value, str) and value:
                    values.append(value)
        return tuple(values)

    async def _execute(self, grant: _Grant, request_id: str, name: str, arguments: dict) -> dict:
        # A busy grant must not occupy global slots while waiting for its own.
        async with grant.semaphore, self._parallel:
            if not self._available(grant, name):
                raise ToolBridgeError("Tool permission changed before execution")
            original = self.owner.tools.get(name)

            def dispatch_guard():
                if not self._available(grant, name):
                    return False
                _validate_arguments(original, arguments)
                if name in {"video_analyze", "image_analyze"}:
                    key = "video_url" if name == "video_analyze" else "image_url"
                    value = arguments.get(key, "")
                    if isinstance(value, str) and urlparse(value).scheme not in {"http", "https", "data"}:
                        arguments[key] = str(self._safe_file(value))
                return True

            dispatch_guard()
            # Context-bearing Board tools are never mutated on the shared
            # registry instance. Their store/orchestrator remains the live one.
            bound = copy.copy(original)
            from flowly.mcp.tool import MCPTool

            native_mcp = isinstance(bound, MCPTool)
            if native_mcp:
                # Keep the public MCP result shape through the registry hooks;
                # do not turn remote media into internal local-path envelopes.
                bound._bridge_native_result = True
            if name == "video_analyze":
                from flowly.agent.media_files import read_media_file

                bound._local_file_reader = lambda path: read_media_file(
                    path, self._read_roots(), MAX_INLINE_MEDIA_BYTES,
                )
            if name.startswith("board_"):
                channel, chat_id = grant.session_key.split(":", 1)
                bound.set_context(channel, chat_id)
                identity = f"mcp:{grant.audit_id}"
                idempotency_key = hashlib.sha256(f"{grant.audit_id}:{request_id}".encode()).hexdigest()
                bound.set_identity(identity, created_by=identity, request_id=idempotency_key)
            error_secrets = self._tool_secrets(bound)
            try:
                from flowly.agent.tool_context import tool_execution_scope

                with tool_execution_scope(grant.session_key, allowed_tools=grant.names):
                    result = await asyncio.wait_for(self.owner.tools.execute(
                        name, arguments, session_key=grant.session_key, **self._route(grant.session_key),
                        _bound_tool=bound, _dispatch_guard=dispatch_guard,
                    ), timeout=600)
                return await asyncio.to_thread(
                    self._format_result, name, result, native_mcp=native_mcp,
                    error_secrets=error_secrets + self._tool_secrets(bound),
                )
            except asyncio.CancelledError:
                raise
            except ToolBridgeError as exc:
                return _error(str(exc))
            except Exception:
                return _error(f"Tool '{name}' failed; check the live runtime diagnostics")

    def _format_result(
        self, name: str, result: Any, *, native_mcp: bool = False, error_secrets: tuple[str, ...] = (),
    ) -> dict:
        text = result if isinstance(result, str) else _json(result)
        if len(text.encode()) > MAX_RESULT_BYTES:
            return _error("Tool result exceeds the bridge response size limit")
        if native_mcp:
            try:
                value = json.loads(text)
            except (ValueError, TypeError):
                value = None
            if isinstance(value, dict) and not value.get("isError") and isinstance(value.get("content"), list):
                return value
        content = []
        structured = None
        try:
            value = json.loads(text)
            if isinstance(value, (dict, list)):
                structured = value
        except (ValueError, TypeError):
            pass
        is_error = text.lstrip().lower().startswith((
            "error", "[blocked:", "image generation failed", "voice generation failed",
            "image generation error", "voice generation error",
        ))
        if isinstance(structured, dict):
            is_error |= bool(structured.get("error")) or structured.get("isError") is True or structured.get("ok") is False or structured.get("success") is False
        if is_error:
            from flowly.mcp.security import sanitize_error

            # Detect errors before peeling the media envelope: a benign summary
            # must not override failure flags or cause partial files to be read.
            return _error(sanitize_error(text, secrets=error_secrets))
        if name in MEDIA_TOOLS:
            from flowly.agent.reply_media import extract_reply_media

            try:
                paths, summary = extract_reply_media(text, require_existing=False, strict=True)
            except ValueError:
                raise ToolBridgeError("Invalid generated media result") from None
            if paths:
                structured = None
                if len(paths) > 8:
                    raise ToolBridgeError("Too many generated media attachments")
                text = summary or "Generated media"
                media_bytes = len(text.encode())
                for raw_path in paths:
                    path = self._safe_file(raw_path)
                    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
                    if mime == "audio/x-wav":
                        mime = "audio/wav"
                    if not mime.startswith(("image/", "audio/")):
                        raise ToolBridgeError("Unsupported generated media format")
                    from flowly.agent.media_files import read_media_file

                    try:
                        data = read_media_file(path, self._read_roots(), MAX_INLINE_MEDIA_BYTES)
                    except (ValueError, OSError):
                        raise ToolBridgeError("Generated media cannot be safely read within the inline size limit") from None
                    media_bytes += 4 * ((len(data) + 2) // 3) + 256
                    if media_bytes > MAX_RESULT_BYTES:
                        raise ToolBridgeError("Generated media exceeds the bridge response size limit")
                    content.append({
                        "type": "image" if mime.startswith("image/") else "audio",
                        "mimeType": mime, "data": base64.b64encode(data).decode(),
                    })
        output = {"content": [{"type": "text", "text": text}, *content], "isError": False}
        if structured is not None:
            output["structuredContent"] = structured
        if len(_json(output).encode()) > MAX_RESULT_BYTES:
            return _error("Tool result exceeds the bridge response size limit")
        return output
