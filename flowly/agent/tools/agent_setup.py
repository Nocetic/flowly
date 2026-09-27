"""Setup bookkeeping for a named agent's own conversation.

The server (``flowly.agent_home``) owns the state; these tools only let the
model offer choices, propose a working style, and report a real task. None of
them saves preferences, grants access or connects accounts. Clients hide them
from the visible work log.
"""

import json

from flowly.agent.tools.base import Tool


def _origin_session() -> str:
    from flowly.agent.tool_context import current_tool_origin

    origin = current_tool_origin()
    return origin.session_key if origin is not None else ""


def _run(operation) -> str:
    from flowly.agent_home import AgentHomeError

    try:
        return json.dumps(operation(_origin_session()), ensure_ascii=False)
    except AgentHomeError as exc:
        return json.dumps({"error": str(exc), "code": exc.code}, ensure_ascii=False)


class AgentSetupAskTool(Tool):
    toolset = "interactive"
    name = "agent_setup_ask"
    description = (
        "Offer the owner two or three short choices for one setup question. The app shows them "
        "under your message and always allows a typed answer. Does not wait for the answer."
    )
    parameters = {
        "type": "object",
        "properties": {
            "question": {"type": "string", "description": "One short question, in the owner's language."},
            "options": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 2,
                "maxItems": 3,
                "description": "Two or three short, concrete, distinct choices. No 'Other'.",
            },
        },
        "required": ["question", "options"],
        "additionalProperties": False,
    }

    async def execute(self, question: str = "", options: list | None = None, **kwargs) -> str:
        from flowly.agent_home import ask

        return _run(lambda session_key: ask(session_key, question, options))


class AgentSetupProposeCardTool(Tool):
    toolset = "interactive"
    name = "agent_setup_propose_card"
    description = (
        "Propose your working style as a short card the owner can save or edit. Saving writes it "
        "to your SOUL.md and completes setup. Write every field in the owner's language."
    )
    parameters = {
        "type": "object",
        "properties": {
            "role": {"type": "string", "description": "Who you are for the owner, one line."},
            "focus": {"type": "string", "description": "What you will focus on first, one line."},
            "style": {"type": "string", "description": "How you communicate and deliver work, one line."},
            "notes": {"type": "string", "description": "Optional working agreements, one line."},
        },
        "required": ["role", "focus"],
        "additionalProperties": False,
    }

    async def execute(self, **kwargs) -> str:
        from flowly.agent_home import propose_card

        card = {key: kwargs.get(key) for key in ("role", "focus", "style", "notes")}
        return _run(lambda session_key: propose_card(session_key, card))


class AgentSetupFinishTool(Tool):
    toolset = "interactive"
    name = "agent_setup_finish"
    description = (
        "Finish setup because the owner gave a concrete task in a typed message. Not for tapped "
        "setup choices. Does not save preferences or grant access."
    )
    parameters = {
        "type": "object",
        "properties": {"reason": {"type": "string", "enum": ["task"]}},
        "additionalProperties": False,
    }

    async def execute(self, reason: str = "task", **kwargs) -> str:
        from flowly.agent_home import finish_for_task

        return _run(finish_for_task)
