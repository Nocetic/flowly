"""Profile-to-profile messaging through the authenticated Desktop broker."""

from __future__ import annotations

import json
import uuid
from typing import Any

from flowly.agent.tools.base import Tool


class MessageProfileTool(Tool):
    """Ask another isolated local profile for input and await its response."""

    def __init__(self, gateway_server: Any):
        self._gateway = gateway_server

    @property
    def name(self) -> str:
        return "message_profile"

    @property
    def description(self) -> str:
        return (
            "Send a focused message to another local profile listed in the "
            "profile collaboration context and wait for its reply. Use the "
            "stable profile id exactly as shown. Use this when the user "
            "mentions another profile or when its distinct role/expertise is "
            "materially useful. Do not message yourself, do not loop, and do "
            "not repeat a request that already has a result."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "target_profile": {
                    "type": "string",
                    "description": "Stable target profile id from the collaboration context.",
                },
                "message": {
                    "type": "string",
                    "description": "Self-contained request for the target profile.",
                },
            },
            "required": ["target_profile", "message"],
            "additionalProperties": False,
        }

    @property
    def toolset(self) -> str:
        return "delegation"

    @property
    def supported_platforms(self) -> frozenset[str]:
        return frozenset({"desktop"})

    def is_available(self) -> bool:
        return self._gateway is not None

    async def execute(self, target_profile: str, message: str) -> str:
        target = str(target_profile or "").strip()
        content = str(message or "").strip()
        if not target:
            return "Error: target_profile is required."
        if not content:
            return "Error: message is required."
        if len(content) > 32_000:
            return "Error: profile message exceeds 32,000 characters."
        result = await self._gateway.send_profile_message_request(
            request_id=str(uuid.uuid4()),
            target_profile=target,
            message=content,
        )
        if not isinstance(result, dict):
            return str(result)
        if result.get("error"):
            code = str(result.get("error_code") or "PROFILE_MESSAGE_FAILED")
            return f"Error ({code}): {result['error']}"
        return json.dumps(result, ensure_ascii=False, separators=(",", ":"))
