"""``template_draft``: the agent drafts a template of itself for its owner to share.

The agent looks at what is its own (profile, SOUL.md, its workspace skills,
its routines, the names of its apps; memories only those the owner named),
builds the recipe and sends it as a draft. The server keeps it and shows a
card in this conversation; the owner reviews and publishes it there. The
agent never publishes (flowly-server docs/cloud/design/agent-templates.md).
"""
from __future__ import annotations

import json
from typing import Any

from flowly.agent.tools.base import Tool
from flowly.templates.recipe import MAX_MEMORIES, MAX_MEMORY_CHARS, RecipeError, recipe_counts


def _result(**fields: Any) -> str:
    return json.dumps(fields, ensure_ascii=False)


def _refused(code: str, message: str, field: str = "") -> str:
    return _result(status="refused", code=code, field=field, error=message)


def _session_key() -> str:
    from flowly.agent.tool_context import current_tool_origin

    origin = current_tool_origin()
    return origin.session_key if origin is not None else ""


def _nobody_to_answer(session_key: str) -> bool:
    """A turn no person watches: a scheduled run, another agent's message, a subagent's thread."""
    from flowly.profile_host_contract import is_internal_profile_session
    from flowly.session.keys import is_subagent_thread

    try:
        from flowly.cron.context import in_cron_context

        if in_cron_context():
            return True
    except Exception:
        pass
    return is_internal_profile_session(session_key) or is_subagent_thread(session_key)


def _strings(value: Any) -> list[str] | None:
    if value is None:
        return None
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise RecipeError("BAD_RECIPE", "", "Name skills and routines as a list of names.")
    return [item.strip() for item in value if item.strip()]


class DraftTemplateTool(Tool):
    def __init__(self, agent: Any) -> None:
        self._agent = agent

    @property
    def name(self) -> str:
        return "template_draft"

    @property
    def description(self) -> str:
        return (
            "Draft a shareable template of yourself when your owner asks for one (\"make a template of you\", "
            "\"share this agent\"). It carries your name, description, avatar and SOUL.md, your own skills, "
            "your routines as suggestions (prompt and schedule), and the names of your apps, never their keys. "
            "Memories go in only when the owner names them: pass their exact words in `memories`, never pick "
            "memories yourself. A card appears in this chat; the owner reviews and publishes it there, you never "
            "publish. After drafting, tell the owner in two or three sentences what is in it and what was left "
            "out. If it is refused for a secret, say which part holds it and offer to remove it."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "The template's name; your own name if omitted."},
                "description": {"type": "string",
                                "description": "One sentence on what an agent made from it does; yours if omitted."},
                "skills": {"type": "array", "items": {"type": "string"},
                           "description": "Slugs of your skills to include; all of your own skills if omitted."},
                "routines": {"type": "array", "items": {"type": "string"},
                             "description": "Names of your routines to include; all if omitted, [] for none."},
                "apps": {"type": "boolean", "description": "Include the names of your apps (default true)."},
                "memories": {"type": "array", "maxItems": MAX_MEMORIES,
                             "items": {"type": "string", "maxLength": MAX_MEMORY_CHARS},
                             "description": "Memories the owner explicitly asked to include, in their words."},
            },
        }

    async def execute(self, name: Any = None, description: Any = None, skills: Any = None, routines: Any = None,
                      apps: Any = True, memories: Any = None, **_: Any) -> str:
        from flowly.agent_home import HOME_SESSION
        from flowly.config.loader import load_config
        from flowly.profile import current_profile_name, get_flowly_home
        from flowly.secrets.redact import current_redactor
        from flowly.templates import drafts
        from flowly.templates.export import build_recipe

        session_key = _session_key()
        if not session_key or _nobody_to_answer(session_key):
            return _refused("NOBODY_TO_ANSWER", "Only your owner can ask for a template, in their conversation with you.")
        if current_profile_name() != "default" and session_key != HOME_SESSION:
            return _refused("WRONG_CONVERSATION", "Draft your template in your own conversation with your owner.")
        try:
            cron = getattr(self._agent, "cron_service", None)
            config = getattr(self._agent, "_main_config", None) or load_config()
            recipe, skipped = build_recipe(
                home=get_flowly_home(),
                workspace=self._agent.workspace,
                cron_jobs=cron.list_jobs(include_disabled=True) if cron is not None else [],
                mcp_servers=getattr(config, "mcp_servers", None) or {},
                name=name if isinstance(name, str) else None,
                description=description if isinstance(description, str) else None,
                skills=_strings(skills),
                routines=_strings(routines),
                include_apps=apps is not False,
                memories=_strings(memories) or [],
                redactor=current_redactor(),
            )
        except RecipeError as exc:
            return _refused(exc.code, str(exc), exc.field)
        try:
            draft_id = await drafts.announce(session_key, recipe)
        except drafts.TemplatesUnavailableError as exc:
            return _refused("TEMPLATES_UNAVAILABLE", str(exc))
        return _result(
            status="drafted", draftId=draft_id, counts=recipe_counts(recipe), skipped=skipped,
            note=("A card for the template is in this chat. Tell your owner in two or three sentences what is in it "
                  "and what was left out; they publish it from the card. Do not publish or share it yourself."),
        )
