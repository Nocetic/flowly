"""Owner-only connection management, shared by local/direct/relay transports.

This surface belongs to authenticated feature RPC, not the model-facing tool
registry or the public MCP server. No credentials or launch arguments are
included in connection listings.
"""

from __future__ import annotations

import inspect
from pathlib import Path

from flowly.config.loader import convert_keys
from flowly.config.schema import MCPServerConfig, MCPServerToolsFilter
from flowly.mcp.oauth_handoff import OAuthHandoffError
from flowly.mcp.setup import MCPSetupError, MCPSetupManager


class MCPConnectionService:
    def __init__(self, path: Path, registry_provider, reload_config=None, *, probe=None, apply=None, external_provider=None):
        from flowly.mcp.probe import probe_tool_names_async

        self.registry_provider = registry_provider
        self.reload_config = reload_config
        self.external_provider = external_provider
        self.manager = MCPSetupManager(path, probe or probe_tool_names_async, apply or self._apply)
        from flowly.mcp.chat_setup import MCPChatSetupManager
        self.chat = MCPChatSetupManager(self.manager)

    @property
    def external(self):
        return self.external_provider() if self.external_provider else None

    async def _apply(self, name: str) -> dict:
        from flowly.mcp.client import reload_mcp_server

        registry = self.registry_provider()
        if registry is None:
            raise MCPSetupError("UNAVAILABLE", "The agent's MCP runtime is unavailable")
        current, _ = self.manager.store.entry(name)
        try:
            config = MCPServerConfig.model_validate(convert_keys(current)) if current is not None else None
        except ValueError:
            raise MCPSetupError("CONFIG_INVALID", "The saved connection has invalid settings") from None
        result = await reload_mcp_server(name, config, registry)
        if self.reload_config:
            refreshed = self.reload_config()
            if inspect.isawaitable(refreshed):
                await refreshed
        return result

    def list(self) -> dict:
        from flowly.integrations.mcp_io import list_mcp_servers
        from flowly.mcp.security import diagnostic_secrets, sanitize_error

        raw_config = self.manager.store.read()
        configured = raw_config.get("mcpServers", {})
        rows = []
        for entry in list_mcp_servers(raw_config):
            raw = configured.get(entry.name, {})
            config = raw if isinstance(raw, dict) else {}
            hidden = diagnostic_secrets(convert_keys(config))
            try:
                permissions = MCPServerToolsFilter.model_validate(config.get("tools", {})).model_dump()
            except ValueError:
                permissions = {"mode": "none", "include": [], "exclude": [], "resources": False, "prompts": False}
            args = config.get("args", [])
            helper = Path(str(config.get("command", ""))).name in {"npx", "npx.cmd"} and any(
                isinstance(arg, str) and (arg == "mcp-remote" or arg.startswith("mcp-remote@"))
                for arg in (args if isinstance(args, list) else [])
            )
            state = "invalid" if entry.status == "invalid" else entry.runtime_state or ("disabled" if not entry.enabled else "not_started")
            rows.append({
                "name": entry.name, "source": entry.source,
                "description": entry.description,
                "transport": "http" if config.get("url") or (entry.source == "catalog" and entry.transport.startswith("http:")) else "stdio",
                "enabled": entry.enabled, "auth": entry.auth, "authorized": entry.authorized,
                "legacyHelper": helper, "permissions": permissions,
                "runtimeState": state, "connected": entry.connected is True,
                "lastError": sanitize_error(entry.error or "", secrets=hidden) or None,
                "reconnectCount": entry.reconnect_count, "lastFailureAt": entry.last_failure_at,
                "needsOauth": entry.needs_oauth, "needsSecrets": entry.needs_secrets,
                "secretFields": [
                    {"name": f.name, "prompt": f.prompt, "secret": f.secret, "default": f.default}
                    for f in (entry.secret_fields or [])
                ],
            })
        return {"servers": rows}

    async def invoke(self, action: str, params: dict) -> dict:
        if not isinstance(params, dict):
            raise MCPSetupError("INVALID", "MCP request must be an object")
        if self.manager._closed:
            raise MCPSetupError("UNAVAILABLE", "MCP runtime is stopping")
        try:
            if action == "chat.pending":
                return self.chat.pending(params.get("sessionKey"))
            if action == "chat.cancel":
                return await self.chat.cancel(params.get("id"))
            if action.startswith("access."):
                from flowly.mcp.external_access import ExternalAccessError

                external = self.external
                if external is None:
                    raise MCPSetupError("UNAVAILABLE", "Start or update the gateway to manage external MCP access")
                try:
                    return await external.owner(action.removeprefix("access."), params)
                except (ExternalAccessError, OSError, TimeoutError) as exc:
                    message = str(exc) if isinstance(exc, ExternalAccessError) else "External access state is busy or unavailable"
                    raise MCPSetupError("ACCESS_INVALID", message) from None
            if action == "list":
                return self.list()
            if action == "action":
                return self.manager.manage(params)
            if action == "begin":
                if "chatRequestId" in params:
                    return self.chat.begin(params)
                return self.manager.begin(params)
            if action == "pending":
                self.manager._prune()
                return {"operations": [op.snapshot() for op in self.manager.operations.values()]}
            if action == "cancel_request":
                return await self.manager.cancel_request(params)
            operation_id = params.get("id")
            if action == "status":
                return self.manager.get(operation_id).snapshot()
            if action == "confirm":
                return self.manager.confirm(operation_id, params.get("permissions"))
            if action == "callback":
                return self.manager.callback(operation_id, params.get("callback"))
            if action == "cancel":
                return await self.manager.cancel(operation_id)
        except OAuthHandoffError as exc:
            raise MCPSetupError("OAUTH_INVALID", str(exc)) from None
        raise MCPSetupError("UNKNOWN_METHOD", "Unknown MCP connection method")

    async def close(self) -> None:
        await self.chat.close()
        await self.manager.close()
