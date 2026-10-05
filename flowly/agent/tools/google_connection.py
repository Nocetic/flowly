"""Ask for a Google connection in the current conversation, without handling secrets."""

import asyncio
import json

from flowly.agent.tool_context import current_tool_origin
from flowly.agent.tools.base import Tool
from flowly.integrations.gmail_connection import GmailConnection, GmailConnectionError
from flowly.integrations.google_chat import google_chat_requests


class GoogleConnectionTool(Tool):
    name = "google_connection"
    description = (
        "Check Google access or show a Gmail/Google Workspace connection request in the current Flowly chat. "
        "Use request when Gmail is not connected or a task needs additional Gmail management, Calendar, Drive, Contacts or Tasks access. "
        "Request only the single service needed. The user authorizes on Google's website through the app; this tool never grants access itself. "
        "Waits for completion, decline or expiry. Do not request passwords, tokens or client secrets, or bypass a declined request."
    )
    parameters = {
        "type": "object", "additionalProperties": False,
        "properties": {
            "action": {"type": "string", "enum": ["status", "request"]},
            "reason": {"type": "string", "minLength": 1, "maxLength": 512},
            "service": {"type": "string", "enum": ["gmail", "calendar", "drive", "contacts", "tasks"],
                        "description": "The single Google service needed for this task. Each service connects independently."},
        }, "required": ["action"],
    }

    async def execute(self, action, reason="", service="gmail", services=None, **kwargs):
        from flowly.channels.feature_rpc import _mcp_connection_service
        from flowly.cron.context import in_cron_context
        try:
            from flowly.integrations.google_permissions import service_permissions
            try:
                required = service_permissions(service)
            except ValueError:
                raise GmailConnectionError("INVALID_SERVICE") from None
            connection = GmailConnection(service=service)
            if action == "status":
                return json.dumps(await asyncio.to_thread(connection.status))
            origin = current_tool_origin()
            if action != "request" or not origin or in_cron_context() or _mcp_connection_service is None:
                return json.dumps({"error": "Google setup requires a live conversation and a supported Flowly app. Open the service in Connections."})
            status = await asyncio.to_thread(connection.status)
            if status.get("connected") and set(required) <= set(status.get("services", [])):
                return json.dumps(status)
            return json.dumps(await google_chat_requests().request(origin.session_key, reason, required, service=service))
        except GmailConnectionError as error:
            return json.dumps({"error": "Google setup could not complete.", "code": error.code})
