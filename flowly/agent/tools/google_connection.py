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
        "The user chooses services and authorizes on Google's website through the app; this tool never grants access itself. "
        "Waits for completion, decline or expiry. Do not request passwords, tokens or client secrets, or bypass a declined request."
    )
    parameters = {
        "type": "object", "additionalProperties": False,
        "properties": {
            "action": {"type": "string", "enum": ["status", "request"]},
            "reason": {"type": "string", "minLength": 1, "maxLength": 512},
            "services": {"type": "array", "minItems": 1, "maxItems": 6, "uniqueItems": True,
                         "items": {"type": "string", "enum": ["gmail", "gmail_manage", "calendar", "drive", "contacts", "tasks"]},
                         "description": "Always include gmail. Request only services needed for the user's task."},
        }, "required": ["action"],
    }

    async def execute(self, action, reason="", services=None, **kwargs):
        from flowly.channels.feature_rpc import _mcp_connection_service
        from flowly.cron.context import in_cron_context
        try:
            if action == "status":
                return json.dumps(await asyncio.to_thread(GmailConnection().status))
            origin = current_tool_origin()
            if action != "request" or not origin or in_cron_context() or _mcp_connection_service is None:
                return json.dumps({"error": "Google setup requires a live conversation and a supported Flowly app. Open Connections or run flowly gmail connect."})
            status = await asyncio.to_thread(GmailConnection().status)
            required = services if services is not None else ["gmail"]
            from flowly.integrations.google_permissions import normalize_services
            try:
                required = normalize_services(required)
            except ValueError:
                raise GmailConnectionError("INVALID_SERVICES") from None
            if status.get("connected") and set(required) <= set(status.get("services", [])):
                return json.dumps(status)
            return json.dumps(await google_chat_requests().request(origin.session_key, reason, services if services is not None else ["gmail"]))
        except GmailConnectionError as error:
            return json.dumps({"error": "Google setup could not complete.", "code": error.code})
