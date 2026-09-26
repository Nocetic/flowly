"""Finish optional introduction without changing identity, memory or access."""

import json

from flowly.agent.tools.base import Tool


class AgentSetupFinishTool(Tool):
    toolset = "interactive"
    name = "agent_setup_finish"
    description = "Finish optional initial setup when the owner skips it or gives a first actionable task. Does not save preferences or grant access."
    parameters = {"type": "object", "properties": {}, "additionalProperties": False}

    async def execute(self, **kwargs) -> str:
        from flowly.agent.tool_context import current_tool_origin
        from flowly.agent_home import AgentHomeError, finish_setup, is_agent_home

        origin = current_tool_origin()
        if origin is None or not is_agent_home(origin.session_key):
            return json.dumps(
                {"error": "Only the agent's own direct conversation can finish setup."}
            )
        try:
            return json.dumps(finish_setup({"state": "complete"}))
        except AgentHomeError as exc:
            return json.dumps({"error": str(exc), "code": exc.code})
