"""Chat proposals are inert until an authenticated owner starts Desktop setup."""

from __future__ import annotations

import asyncio
import copy
import json
import re
import secrets
import time
from dataclasses import dataclass, field

from flowly.config.loader import convert_keys
from flowly.config.schema import MCPServerConfig
from flowly.mcp.catalog import build_server_config, get_entry
from flowly.mcp.setup import MAX_OPERATIONS, TERMINAL, MCPSetupError, MCPSetupManager, _revision

CHAT_SETUP_TIMEOUT = 900


@dataclass
class ChatSetupRequest:
    id: str
    name: str
    session_key: str
    reason: str
    params: dict
    preview: dict
    revision: str
    catalog_revision: str | None
    oauth: bool
    future: asyncio.Future
    created_at: float = field(default_factory=time.time)
    phase: str = "proposed"
    operation_id: str | None = None
    task: asyncio.Task | None = None


class MCPChatSetupManager:
    def __init__(self, manager: MCPSetupManager):
        self.manager = manager
        self.requests: dict[str, ChatSetupRequest] = {}
        self.closed = False

    def _get(self, request_id: str) -> ChatSetupRequest:
        if not isinstance(request_id, str) or request_id not in self.requests:
            raise MCPSetupError("NOT_FOUND", "Connection request is no longer available")
        return self.requests[request_id]

    def snapshot(self, req: ChatSetupRequest) -> dict:
        return {
            "id": req.id, "name": req.name, "reason": req.reason, "sessionKey": req.session_key,
            "createdAt": req.created_at, "expiresAt": req.created_at + CHAT_SETUP_TIMEOUT,
            "phase": req.phase, "operationId": req.operation_id, "oauth": req.oauth,
            "preview": copy.deepcopy(req.preview),
        }

    def pending(self, session_key: str | None = None) -> dict:
        return {"requests": [self.snapshot(req) for req in self.requests.values()
                             if req.phase not in TERMINAL and (session_key is None or req.session_key == session_key)]}

    def _propose(self, params: dict, session_key: str) -> ChatSetupRequest:
        if self.closed or self.manager._closed:
            raise MCPSetupError("UNAVAILABLE", "Connection setup is unavailable")
        for key, req in list(self.requests.items()):
            if req.phase in TERMINAL and time.time() > req.created_at + CHAT_SETUP_TIMEOUT + 300:
                self.requests.pop(key, None)
        if len(self.requests) >= MAX_OPERATIONS:
            raise MCPSetupError("BUSY", "Too many connection requests; try again later")
        name, reason = params.get("name"), params.get("reason", "")
        if not isinstance(name, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", name):
            raise MCPSetupError("INVALID", "A bounded connection name is required")
        if not isinstance(reason, str) or not 1 <= len(reason) <= 512 or not isinstance(session_key, str) or not 1 <= len(session_key) <= 512 or ":" not in session_key:
            raise MCPSetupError("INVALID", "A reason and runtime-owned conversation are required")
        if any(req.session_key == session_key and req.phase not in TERMINAL for req in self.requests.values()):
            raise MCPSetupError("BUSY", "Finish or cancel this conversation's pending connection request")
        original, revision = self.manager.store.entry(name)
        intent = params.get("intent", "connect")
        if intent not in {"connect", "permissions", "reauthorize"}:
            raise MCPSetupError("INVALID", "Unknown connection setup intent")
        draft = params.get("config")
        catalog_revision = None
        start = {"name": name, "intent": intent, "sessionKey": session_key}
        if original is not None:
            if draft is not None:
                raise MCPSetupError("INVALID", "Edit an existing connection in Desktop; do not replace it from chat")
            source = original
            preview = {"kind": "existing", "intent": intent, "fields": []}
        elif intent != "connect":
            raise MCPSetupError("INVALID", "This action requires an existing connection")
        elif draft is not None:
            if not isinstance(draft, dict) or set(draft) - {"command", "args", "url", "transport", "auth"}:
                raise MCPSetupError("INVALID", "Enter credentials and advanced settings privately in Desktop")
            try:
                if len(json.dumps(draft, allow_nan=False).encode()) > 16_384:
                    raise ValueError
                source = MCPServerConfig.model_validate(draft).model_dump()
                if bool(source["url"]) == bool(source["command"]):
                    raise ValueError
            except (ValueError, TypeError, RecursionError):
                raise MCPSetupError("INVALID", "Use one bounded local command or HTTP connection") from None
            start["config"] = copy.deepcopy(draft)
            preview = {"kind": "manual", "config": copy.deepcopy(draft), "fields": []}
        else:
            entry = get_entry(name)
            if entry is None:
                raise MCPSetupError("NOT_FOUND", "Choose a catalog connection or supply non-secret connection settings")
            source = build_server_config(entry)
            catalog_revision = _revision(source)
            start["catalog"] = True
            preview = {"kind": "catalog", "description": entry.description,
                       "config": {k: source[k] for k in ("url", "command", "args") if k in source},
                       "fields": [{"name": item.name, "prompt": item.prompt, "secret": item.secret,
                                   "default": item.default} for item in entry.env]}
        try:
            config = MCPServerConfig.model_validate(convert_keys(source)).model_dump()
        except (TypeError, ValueError):
            raise MCPSetupError("INVALID", "This connection needs to be repaired in Desktop first") from None
        # Never start a helper-based login implicitly from a chat request.
        if any(isinstance(arg, str) and (arg == "mcp-remote" or arg.startswith("mcp-remote@")) for arg in config.get("args", [])):
            raise MCPSetupError("MIGRATION_REQUIRED", "Use Desktop's explicit native sign-in migration for this connection first")
        req = ChatSetupRequest(
            secrets.token_urlsafe(24), name, session_key, reason, start, preview, revision,
            catalog_revision, config.get("auth") == "oauth" and intent != "permissions",
            asyncio.get_running_loop().create_future(),
        )
        self.requests[req.id] = req
        return req

    async def request(self, params: dict, session_key: str) -> dict:
        req = self._propose(params, session_key)
        try:
            async with asyncio.timeout(CHAT_SETUP_TIMEOUT):
                return await asyncio.shield(req.future)
        except TimeoutError:
            try:
                await self.cancel(req.id, phase="expired")
            except MCPSetupError as exc:
                if exc.code != "BUSY":
                    raise
                # A human already accepted this commit; observe its true result.
                return await asyncio.shield(req.future)
            return req.future.result()
        except asyncio.CancelledError:
            try:
                await self.cancel(req.id)
            except MCPSetupError:
                pass  # Accepted commits drain independently of a stopped turn.
            raise

    def begin(self, params: dict) -> dict:
        req = self._get(params.get("chatRequestId"))
        if self.closed or req.phase in TERMINAL:
            raise MCPSetupError("CANCELLED", "This connection request ended; ask for a new request")
        if req.operation_id:
            op = self.manager.get(req.operation_id)
            if params.get("requestId") != op.request_id:
                raise MCPSetupError("BUSY", "This request was started on another Desktop; finish it there")
            return op.snapshot()
        if time.time() >= req.created_at + CHAT_SETUP_TIMEOUT:
            raise MCPSetupError("EXPIRED", "This connection request expired")
        _, revision = self.manager.store.entry(req.name)
        if revision != req.revision:
            raise MCPSetupError("CONFLICT", "This connection changed; ask for a new request")
        if req.catalog_revision is not None:
            entry = get_entry(req.name)
            if entry is None or _revision(build_server_config(entry)) != req.catalog_revision:
                raise MCPSetupError("CONFLICT", "The catalog connection changed; ask for a new request")
        # The user may add catalog credentials and the private Desktop callback,
        # but cannot silently rebind this conversation's request to another name.
        start = {**req.params, **{k: params[k] for k in ("requestId", "redirectUri", "envValues") if k in params}}
        snapshot = self.manager.begin(start)
        req.operation_id = snapshot["id"]
        req.phase = "started"
        req.task = asyncio.create_task(self._observe(req), name="mcp-chat-setup-result")
        return snapshot

    def _finish(self, req: ChatSetupRequest, phase: str, *, saved=False, connected=False, error=None) -> None:
        req.phase = phase
        req.params = {}
        req.preview = {}
        if not req.future.done():
            req.future.set_result({"name": req.name, "status": phase, "saved": saved, "connected": connected, "error": copy.deepcopy(error),
                                   "note": "Use only the permissions the owner saved. Do not bypass cancelled or failed setup."})

    async def _observe(self, req: ChatSetupRequest) -> None:
        op = self.manager.get(req.operation_id)
        await asyncio.gather(asyncio.shield(op.task), return_exceptions=True)
        phase = op.phase if op.phase in TERMINAL else ("failed" if op.saved else "cancelled")
        self._finish(req, phase, saved=op.saved, connected=(op.runtime or {}).get("connected") is True, error=op.error)

    async def cancel(self, request_id: str, *, phase="cancelled") -> dict:
        req = self._get(request_id)
        if req.phase in TERMINAL:
            return self.snapshot(req)
        if req.operation_id:
            result = await self.manager.cancel(req.operation_id)
            if result["saved"]:
                self._finish(req, result["phase"], saved=True, connected=(result.get("runtime") or {}).get("connected") is True)
                return self.snapshot(req)
        self._finish(req, phase)
        return self.snapshot(req)

    async def close(self) -> None:
        self.closed = True
        for req in list(self.requests.values()):
            if req.phase not in TERMINAL:
                try:
                    await self.cancel(req.id)
                except MCPSetupError as exc:
                    if exc.code != "BUSY":
                        raise
        await asyncio.gather(*(req.task for req in self.requests.values() if req.task), return_exceptions=True)
