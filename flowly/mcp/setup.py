"""Runtime-owned, cancellable MCP setup shared by every owner UI.

Discovery and OAuth prepare a draft. Only an explicit permission decision can
publish it. A new credential slot is made durable before the atomic config
pointer swap; a failed or interrupted setup never overwrites working tokens.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import re
import secrets
import time
import uuid
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

from flowly.config.loader import convert_keys, convert_to_camel
from flowly.config.schema import MCPServerConfig, MCPServerToolsFilter
from flowly.config.transaction import config_write_lock
from flowly.mcp.oauth import clear_tokens, oauth_login
from flowly.mcp.oauth_handoff import DesktopOAuthHandoff, desktop_oauth_handoff
from flowly.mcp.oauth_state import atomic_private_write, read_private

TERMINAL = frozenset({"complete", "failed", "cancelled", "expired"})
MAX_OPERATIONS = 32
SETUP_TIMEOUT = 600.0


class MCPSetupError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _revision(entry: dict | None) -> str:
    return hashlib.sha256(json.dumps(entry, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class MCPConfigStore:
    def __init__(self, path: Path):
        self.path = path

    def read(self) -> dict:
        try:
            raw = json.loads(read_private(self.path))
        except FileNotFoundError:
            return {}
        except (ValueError, OSError):
            raise MCPSetupError("CONFIG_INVALID", "Configuration cannot be safely read; no changes were made") from None
        if not isinstance(raw, dict) or not isinstance(raw.get("mcpServers", {}), dict):
            raise MCPSetupError("CONFIG_INVALID", "Configuration is invalid; no changes were made")
        return raw

    def entry(self, name: str) -> tuple[dict | None, str]:
        entry = self.read().get("mcpServers", {}).get(name)
        if entry is not None and not isinstance(entry, dict):
            raise MCPSetupError("CONFIG_INVALID", "This MCP connection has invalid configuration")
        return copy.deepcopy(entry), _revision(entry)

    def publish(self, name: str, config: dict | None, revision: str, before_publish: Callable[[], None]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with config_write_lock(self.path):
            raw = self.read()
            current = raw.get("mcpServers", {}).get(name)
            if _revision(current) != revision:
                raise MCPSetupError("CONFLICT", "This connection changed during setup; review the latest settings")
            previous = json.dumps(raw, ensure_ascii=False, indent=2).encode()
            if config is None:
                raw.get("mcpServers", {}).pop(name, None)
            else:
                raw.setdefault("mcpServers", {})[name] = convert_to_camel(config)
            rendered = json.dumps(raw, ensure_ascii=False, indent=2).encode()
            if len(rendered) > 1024 * 1024:
                raise MCPSetupError("CONFIG_LIMIT", "Configuration is too large to update safely")
            # Preparation may publish a NEW token slot, never the working one.
            before_publish()
            if self.path.exists():
                atomic_private_write(self.path.with_suffix(".json.bak"), previous)
            atomic_private_write(self.path, rendered)


@dataclass
class SetupOperation:
    id: str
    request_id: str
    request_digest: str
    name: str
    config: dict
    revision: str
    session_key: str | None
    created_at: float = field(default_factory=time.time)
    phase: str = "checking"
    tools: list[str] = field(default_factory=list)
    capabilities: dict = field(default_factory=lambda: {"resources": False, "prompts": False})
    error: dict | None = None
    saved: bool = False
    runtime: dict | None = None
    handoff: DesktopOAuthHandoff | None = None
    decision: asyncio.Future | None = None
    task: asyncio.Task | None = None

    def snapshot(self) -> dict:
        handoff = self.handoff.snapshot() if self.handoff else {}
        phase = self.phase
        if phase == "checking" and handoff.get("authorizationUrl") and not handoff.get("callbackReceived"):
            phase = "awaiting_authorization"
        return {
            "id": self.id, "requestId": self.request_id,
            "name": self.name, "phase": phase, "sessionKey": self.session_key,
            "createdAt": self.created_at, "expiresAt": self.created_at + SETUP_TIMEOUT,
            "authorizationUrl": handoff.get("authorizationUrl") if phase == "awaiting_authorization" else None,
            "tools": list(self.tools), "error": self.error, "saved": self.saved,
            "capabilities": dict(self.capabilities),
            "runtime": copy.deepcopy(self.runtime),
            "permissions": copy.deepcopy(self.config.get("tools", {})),
        }


Probe = Callable[..., Awaitable[tuple[bool, list[str], str]]]
Apply = Callable[[str], Awaitable[dict]]


class MCPSetupManager:
    def __init__(self, path: Path, probe: Probe, apply: Apply):
        self.store = MCPConfigStore(path)
        self.probe = probe
        self.apply = apply
        self.operations: dict[str, SetupOperation] = {}
        self._cancelled_requests: dict[str, tuple[str, float]] = {}
        self._closed = False

    def _prune(self) -> None:
        now = time.time()
        for key, (_, deadline) in list(self._cancelled_requests.items()):
            if now >= deadline:
                self._cancelled_requests.pop(key, None)
        for key, op in list(self.operations.items()):
            if op.phase in TERMINAL and now - op.created_at > SETUP_TIMEOUT + 300:
                self.operations.pop(key, None)

    def get(self, operation_id: str) -> SetupOperation:
        if not isinstance(operation_id, str) or operation_id not in self.operations:
            raise MCPSetupError("NOT_FOUND", "Setup is no longer available; start again")
        return self.operations[operation_id]

    def begin(self, params: dict) -> dict:
        name, request_id, digest, existing = self._request(params, "setup")
        if existing:
            return existing.snapshot()
        return self._begin(params, name, request_id, digest)

    def _request(self, params: dict, kind: str) -> tuple[str, str, str, SetupOperation | None]:
        if self._closed:
            raise MCPSetupError("UNAVAILABLE", "MCP runtime is stopping")
        if not isinstance(params, dict):
            raise MCPSetupError("INVALID", "Setup settings must be an object")
        self._prune()
        name = params.get("name")
        request_id = params.get("requestId")
        if not isinstance(name, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", name):
            raise MCPSetupError("INVALID", "Use a connection name with 1–64 letters, numbers, hyphens or underscores")
        if not isinstance(request_id, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{16,128}", request_id):
            raise MCPSetupError("INVALID", "A unique setup request ID is required")
        if request_id in self._cancelled_requests:
            raise MCPSetupError("CANCELLED", "This setup request was cancelled; start a new request")
        digest = _revision({"kind": kind, "params": params})
        for existing in self.operations.values():
            if existing.request_id == request_id:
                if existing.request_digest != digest:
                    raise MCPSetupError("CONFLICT", "Setup request ID was already used with different settings")
                return name, request_id, digest, existing
            if existing.name == name and existing.phase not in TERMINAL:
                raise MCPSetupError("BUSY", "Finish or cancel the current setup for this connection")
        if len(self.operations) >= MAX_OPERATIONS:
            raise MCPSetupError("BUSY", "Too many setup requests; try again shortly")
        return name, request_id, digest, None

    def _begin(self, params: dict, name: str, request_id: str, digest: str) -> dict:
        original, revision = self.store.entry(name)
        intent = params.get("intent", "connect")
        if intent not in ("connect", "reauthorize", "permissions"):
            raise MCPSetupError("INVALID", "Unknown setup action")
        supplied = params.get("config")
        if params.get("catalog") is True:
            if supplied is not None or original is not None or intent != "connect":
                raise MCPSetupError("INVALID", "Choose an unconfigured catalog connection")
            from flowly.mcp.catalog import build_server_config, get_entry

            entry = get_entry(name)
            if entry is None:
                raise MCPSetupError("NOT_FOUND", "Catalog connection is unavailable")
            values = params.get("envValues", {})
            if not isinstance(values, dict) or set(values) - {item.name for item in entry.env}:
                raise MCPSetupError("INVALID", "Unknown catalog credential field")
            supplied = build_server_config(entry)
            # Draft-only values: do not change the shared .env during a probe.
            # They are persisted privately with this connection after consent.
            for item in entry.env:
                value = values.get(item.name, item.default)
                if not isinstance(value, str) or not value or len(value) > 16_384:
                    raise MCPSetupError("INVALID", "Complete the required catalog fields")
                supplied.setdefault("env", {})[item.name] = value
            # Substitute only catalog-declared placeholders, never arbitrary
            # user-controlled expressions. HTTP entries may put keys in URLs.
            def substitute(value):
                if isinstance(value, str):
                    for key, replacement in supplied.get("env", {}).items():
                        value = value.replace("${" + key + "}", replacement)
                    return value
                if isinstance(value, list):
                    return [substitute(item) for item in value]
                if isinstance(value, dict):
                    return {key: substitute(item) for key, item in value.items()}
                return value
            supplied = substitute(supplied)
        if intent in {"reauthorize", "permissions"} and (original is None or supplied is not None):
            raise MCPSetupError("INVALID", "This action requires a saved connection")
        if supplied is not None and not isinstance(supplied, dict):
            raise MCPSetupError("INVALID", "Connection settings must be an object")
        source = copy.deepcopy(supplied if supplied is not None else original)
        if source is None:
            raise MCPSetupError("INVALID", "Connection settings are required")
        # Explicitly migrate only recognizable helper configurations. We never
        # reinterpret arbitrary commands or silently move their credentials.
        if params.get("migrate") is True:
            if supplied is not None or not original:
                raise MCPSetupError("INVALID", "Migration requires an existing connection")
            args = source.get("args", [])
            command = Path(str(source.get("command", ""))).name
            if command not in {"npx", "npx.cmd"} or not isinstance(args, list) or len(args) != 3 or args[0] != "-y" or not re.fullmatch(r"mcp-remote(?:@[a-zA-Z0-9._-]+)?", str(args[1])):
                raise MCPSetupError("INVALID", "This helper configuration needs manual migration")
            source = {"url": args[2], "transport": "http", "auth": "oauth", "tools": source.get("tools", {})}
        try:
            config = MCPServerConfig.model_validate(convert_keys(source)).model_dump()
        except (TypeError, ValueError):
            raise MCPSetupError("INVALID", "Invalid MCP connection settings") from None
        if bool(config["command"]) == bool(config["url"]):
            raise MCPSetupError("INVALID", "Choose either a local command or a remote URL")
        if config["url"]:
            from flowly.mcp.client import _validate_http_url
            from flowly.mcp.env_loader import load_flowly_dotenv
            from flowly.mcp.security import interpolate_env_vars

            load_flowly_dotenv()
            try:
                resolved_url = _validate_http_url(name, interpolate_env_vars(config)["url"])
            except (ValueError, TypeError):
                raise MCPSetupError("INVALID", "Use a valid HTTP or HTTPS MCP address") from None
        else:
            resolved_url = ""
        session_key = params.get("sessionKey")
        if session_key is not None and (not isinstance(session_key, str) or not 1 <= len(session_key) <= 512 or ":" not in session_key):
            raise MCPSetupError("INVALID", "Invalid owning conversation")
        handoff = None
        if config["auth"] == "oauth" and intent != "permissions":
            if not config["url"]:
                raise MCPSetupError("INVALID", "Native OAuth requires an HTTP connection")
            handoff = DesktopOAuthHandoff(params.get("redirectUri"))
            config["oauth_credential_id"] = uuid.uuid4().hex
        elif supplied is not None:
            config["oauth_credential_id"] = ""
        op = SetupOperation(
            id=secrets.token_urlsafe(32), request_id=request_id, request_digest=digest,
            name=name, config=config, revision=revision, session_key=session_key, handoff=handoff,
        )
        op.decision = asyncio.get_running_loop().create_future()
        self.operations[op.id] = op
        op.task = asyncio.create_task(self._run(op, resolved_url), name="mcp-owner-setup")
        return op.snapshot()

    async def _run(self, op: SetupOperation, resolved_url: str) -> None:
        try:
            with ExitStack() as stack:
                login = None
                if op.handoff:
                    login = stack.enter_context(oauth_login(op.name, resolved_url, credential_id=op.config["oauth_credential_id"]))
                    stack.enter_context(desktop_oauth_handoff(op.name, resolved_url, op.handoff))
                # This deadline covers human waiting, not an already accepted
                # commit. Once publication starts its final outcome must be known.
                async with asyncio.timeout(SETUP_TIMEOUT):
                    ok, names, error = await self.probe(op.name, op.config, interactive=op.handoff is not None)
                    if not ok:
                        raise MCPSetupError("CONNECTION_FAILED", error)
                    op.tools = sorted(set(names))
                    op.capabilities = dict(getattr(names, "capabilities", {"resources": False, "prompts": False}))
                    op.phase = "review"
                    policy = await op.decision
                op.config["tools"] = policy
                op.config["enabled"] = policy["mode"] != "none"
                # No await inside the short publication critical section:
                # cancellation cannot abandon a thread that later swaps config.
                self.store.publish(op.name, op.config, op.revision, login.commit if login else lambda: None)
                op.saved = True
            result = await self.apply(op.name)
            op.runtime = result
            if result.get("ok") is not True:
                raise MCPSetupError("APPLY_FAILED", "Settings were saved but the connection could not be applied; retry")
            op.phase = "complete"
        except asyncio.CancelledError:
            op.phase = "failed" if op.saved else "cancelled"
            if op.saved:
                op.error = {"code": "APPLY_INTERRUPTED", "message": "Settings were saved; reconnect to verify them"}
        except TimeoutError:
            op.phase = "expired"
            op.error = {"code": "EXPIRED", "message": "Setup timed out; start again"}
        except Exception as exc:
            from flowly.mcp.security import diagnostic_secrets, exception_diagnostic

            op.phase = "failed"
            op.error = {
                "code": exc.code if isinstance(exc, MCPSetupError) else "SETUP_FAILED",
                "message": exception_diagnostic(exc, secrets=diagnostic_secrets(op.config)),
            }
        finally:
            if op.handoff:
                op.handoff.close()
                if not op.saved:
                    # fsync/permission failures can happen AFTER atomic replace.
                    # Never erase a slot unless config proves it is unreferenced.
                    try:
                        current, _ = self.store.entry(op.name)
                        selected = (current or {}).get("oauthCredentialId", "")
                        if selected == op.config["oauth_credential_id"]:
                            op.saved = True
                        else:
                            clear_tokens(op.name, credential_id=op.config["oauth_credential_id"])
                    except (MCPSetupError, OSError):
                        pass
            # Config can contain API keys. Keep only the policy for terminal UI.
            op.config = {"tools": op.config.get("tools", {})}

    def confirm(self, operation_id: str, permissions: dict) -> dict:
        op = self.get(operation_id)
        if not isinstance(permissions, dict) or permissions.get("mode") not in ("all", "selected", "none"):
            raise MCPSetupError("INVALID", "Choose all, selected or no tools")
        try:
            policy = MCPServerToolsFilter.model_validate(permissions).model_dump()
        except ValueError:
            raise MCPSetupError("INVALID", "Invalid tool permissions") from None
        if any(policy[key] and not op.capabilities.get(key) for key in ("resources", "prompts")):
            raise MCPSetupError("INVALID", "This connection did not advertise the requested capability")
        if not set(policy["exclude"]) <= set(op.tools):
            raise MCPSetupError("INVALID", "An excluded tool was not returned by this connection")
        if not set(policy["include"]) <= set(op.tools):
            raise MCPSetupError("INVALID", "A selected tool was not returned by this connection")
        if policy["mode"] != "selected":
            policy["include"] = []
        elif not policy["include"] and not policy["resources"] and not policy["prompts"]:
            policy["mode"] = "none"
        if policy["mode"] == "none":
            policy.update(exclude=[], resources=False, prompts=False)
        if op.phase in {"committing", "complete"}:
            if policy != op.config.get("tools"):
                raise MCPSetupError("CONFLICT", "Setup already has a different permission decision")
            return op.snapshot()
        if op.phase != "review" or op.decision is None or op.decision.done():
            raise MCPSetupError("NOT_READY", "Wait for connection verification before choosing permissions")
        op.phase = "committing"
        op.config["tools"] = policy
        op.decision.set_result(policy)
        return op.snapshot()

    def callback(self, operation_id: str, payload: dict) -> dict:
        op = self.get(operation_id)
        if not op.handoff or op.phase != "checking":
            raise MCPSetupError("NOT_READY", "This setup is not waiting for OAuth")
        op.handoff.submit(payload)
        return op.snapshot()

    async def cancel(self, operation_id: str) -> dict:
        op = self.get(operation_id)
        if op.phase == "committing":
            raise MCPSetupError("BUSY", "Setup is being applied; wait for the final result")
        if op.phase not in TERMINAL and op.task:
            op.task.cancel()
            await asyncio.gather(op.task, return_exceptions=True)
            # Cancellation before the worker's first instruction still counts.
            op.phase = "cancelled"
            if op.handoff:
                op.handoff.close()
            op.config = {"tools": op.config.get("tools", {})}
        return op.snapshot()

    async def cancel_request(self, params: dict) -> dict:
        """Cancel even when the client never received the begin acknowledgment.

        A short-lived tombstone rejects a delayed begin frame from an old
        socket. It cannot silently start a new probe after cancellation.
        """
        self._prune()
        request_id, name = params.get("requestId"), params.get("name")
        if not isinstance(request_id, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{16,128}", request_id) or not isinstance(name, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", name):
            raise MCPSetupError("INVALID", "A valid setup name and request ID are required")
        for op in self.operations.values():
            if op.request_id == request_id:
                if op.name != name:
                    raise MCPSetupError("CONFLICT", "Setup request belongs to a different connection")
                return await self.cancel(op.id)
        previous = self._cancelled_requests.get(request_id)
        if previous and previous[0] != name:
            raise MCPSetupError("CONFLICT", "Setup request belongs to a different connection")
        if not previous and len(self._cancelled_requests) >= MAX_OPERATIONS:
            raise MCPSetupError("BUSY", "Too many cancelled requests; retry shortly")
        self._cancelled_requests[request_id] = (name, time.time() + SETUP_TIMEOUT + 300)
        return {"ok": True, "phase": "cancelled"}

    async def close(self) -> None:
        # Shutdown drains an accepted commit instead of reporting a cancelled
        # operation that silently changes configuration in a worker thread.
        self._closed = True
        tasks = []
        for op in self.operations.values():
            if op.task and not op.task.done():
                if op.phase != "committing":
                    op.task.cancel()
                tasks.append(op.task)
        await asyncio.gather(*tasks, return_exceptions=True)

    def manage(self, params: dict) -> dict:
        """Accept a bounded owner action; its lifetime is not a socket's lifetime."""
        name, request_id, digest, existing = self._request(params, "manage")
        if existing:
            return existing.snapshot()
        action = params.get("action")
        if action not in {"retry", "enable", "disable", "remove"}:
            raise MCPSetupError("INVALID", "Unknown MCP connection action")
        config, revision = self.store.entry(name)
        if config is None:
            raise MCPSetupError("NOT_FOUND", "This connection is no longer configured")
        if action == "enable":
            try:
                policy = MCPServerToolsFilter.model_validate(config.get("tools", {}))
            except ValueError:
                raise MCPSetupError("INVALID", "Review this connection's permissions first") from None
            if policy.mode == "none" or (policy.mode == "selected" and not policy.include and not policy.resources and not policy.prompts):
                raise MCPSetupError("INVALID", "Choose tool permissions before enabling this connection")
        op = SetupOperation(
            id=secrets.token_urlsafe(32), request_id=request_id, request_digest=digest,
            name=name, config=convert_keys(config), revision=revision,
            session_key=None, phase="committing",
        )
        self.operations[op.id] = op
        op.task = asyncio.create_task(self._run_action(op, action), name="mcp-owner-action")
        return op.snapshot()

    async def _run_action(self, op: SetupOperation, action: str) -> None:
        try:
            if action != "retry":
                if action != "remove":
                    op.config["enabled"] = action == "enable"
                self.store.publish(op.name, None if action == "remove" else op.config, op.revision, lambda: None)
                op.saved = True
            result = await self.apply(op.name)
            op.runtime = result
            if result.get("ok") is not True:
                raise MCPSetupError("APPLY_FAILED", "Connection could not be applied; retry")
            if action == "remove":
                # A draining request may refresh a token. Erase credentials
                # only after the retired transport has fully closed.
                from flowly.mcp.oauth import clear_all_tokens
                clear_all_tokens(op.name)
            op.phase = "complete"
        except asyncio.CancelledError:
            op.phase = "failed"
            op.error = {"code": "APPLY_INTERRUPTED", "message": "Reconnect to verify the connection state"}
        except Exception as exc:
            from flowly.mcp.security import diagnostic_secrets, exception_diagnostic
            op.phase = "failed"
            op.error = {
                "code": exc.code if isinstance(exc, MCPSetupError) else "APPLY_FAILED",
                "message": exception_diagnostic(exc, secrets=diagnostic_secrets(op.config)),
            }
        finally:
            op.config = {"tools": op.config.get("tools", {})}
