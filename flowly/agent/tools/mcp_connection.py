"""Human-reviewed MCP setup requests; no model-facing configuration mutation."""

import json

from flowly.agent.tool_context import current_tool_origin
from flowly.agent.tools.base import Tool
from flowly.mcp.setup import MCPSetupError


class MCPConnectionRequestTool(Tool):
    name = "mcp_connection"
    description = (
        "List MCP connections or ask the user to connect a service, reauthorize it, or review its tool permissions. "
        "A request pauses until the user reviews it in Desktop; it does not run commands, sign in, or grant tools by itself. "
        "Use catalog names where possible. Never put API keys, tokens or passwords into this tool: the user enters them privately in Desktop. "
        "After cancellation or failure, do not bypass setup with shell commands or config edits."
    )
    parameters = {
        "type": "object", "additionalProperties": False,
        "properties": {
            "action": {"type": "string", "enum": ["list", "request"]},
            "name": {"type": "string", "maxLength": 64},
            "reason": {"type": "string", "maxLength": 512},
            "intent": {"type": "string", "enum": ["connect", "reauthorize", "permissions"]},
            "config": {"type": "object", "additionalProperties": False, "properties": {
                "url": {"type": "string"}, "command": {"type": "string"},
                "args": {"type": "array", "items": {"type": "string"}, "maxItems": 64},
                "auth": {"type": "string", "enum": ["", "oauth"]},
                "transport": {"type": "string", "enum": ["auto", "stdio", "http", "sse"]},
            }},
        }, "required": ["action"],
    }

    async def execute(self, **kwargs) -> str:
        from flowly.channels.feature_rpc import _mcp_connection_service
        from flowly.cron.context import in_cron_context

        service = _mcp_connection_service
        if service is None or service.manager._closed:
            return json.dumps({"error": "MCP setup requires the running gateway and Desktop"})
        try:
            if kwargs.get("action") == "list":
                return json.dumps(service.list())
            origin = current_tool_origin()
            if kwargs.get("action") != "request" or origin is None or in_cron_context():
                return json.dumps({"error": "MCP setup requires a live, runtime-owned conversation with a human"})
            return json.dumps(await service.chat.request(kwargs, origin.session_key))
        except MCPSetupError as exc:
            return json.dumps({"error": str(exc), "code": exc.code})
