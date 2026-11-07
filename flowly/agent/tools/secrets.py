"""``request_secret`` and ``list_secrets``: the agent asks for a key it never sees.

The agent asks for an API key or token by name; the person types it into a
card in the chat; it is sealed to this agent's key on the way, saved in the
agent's vault and handed to its commands as an environment variable. The model
only ever sees names.

A request does not hold the turn: the agent says what it asked for and ends
its reply; the conversation gets a turn of its own when the person saves,
declines, or the request expires (``flowly.secrets.requests``).
"""

from __future__ import annotations

import json
from typing import Any

from flowly.agent.tools.base import Tool
from flowly.secrets.names import SecretNameError, check_name
from flowly.secrets.requests import (
    MAX_DESCRIPTION_CHARS,
    MAX_FIELDS,
    MAX_LABEL_CHARS,
    MAX_TITLE_CHARS,
    get_secret_requests,
)

_RULES = (
    "Only for keys and tokens your commands use (API keys, access tokens). Never for card numbers, "
    "website passwords or addresses, and never ask for a secret's value in chat. A saved secret is in your "
    "commands' environment as $NAME ($env:NAME in PowerShell); never print, echo, log or write its value "
    "anywhere."
)


def _result(**fields: Any) -> str:
    return json.dumps(fields, ensure_ascii=False)


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


class RequestSecretTool(Tool):
    def __init__(self) -> None:
        self._channel = ""
        self._chat_id = ""

    def set_context(self, channel: str, chat_id: str) -> None:
        self._channel, self._chat_id = channel, chat_id

    @property
    def name(self) -> str:
        return "request_secret"

    @property
    def description(self) -> str:
        return (
            "Ask the person to save an API key or token for you. They type it into a secure card in this chat; "
            "you never see the value. Do not wait for it: say in one short sentence what you asked for and why, "
            "then end your reply. The conversation continues on its own once they save it, decline, or the "
            "request expires (a week). Check list_secrets first; a name already saved needs no request unless "
            "it must change (replace=true). Image and video generation use your image_generate / video_generate "
            "tools: when you have them, their key is set and needs no request; without them, ask for FAL_KEY "
            "(by that name) and they are yours from the next turn. " + _RULES
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Short title for the card, e.g. 'OpenRouter API key'."},
                "description": {"type": "string",
                                "description": "One or two sentences: what you will use it for and where they find it."},
                "fields": {
                    "type": "array", "minItems": 1, "maxItems": MAX_FIELDS,
                    "description": "The values to ask for together, one per environment variable.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string",
                                     "description": "Environment variable name, e.g. OPENROUTER_API_KEY."},
                            "label": {"type": "string", "description": "What the person sees, e.g. 'API key'."},
                        },
                        "required": ["name", "label"],
                    },
                },
                "replace": {"type": "boolean", "description": "Ask again for a name already saved, to change it."},
            },
            "required": ["title", "description", "fields"],
        }

    async def execute(self, title: Any = "", description: Any = "", fields: Any = None, replace: Any = False,
                      **_: Any) -> str:
        from flowly.secrets import service
        from flowly.secrets.vault import SecretsError, get_vault

        title = str(title or "").strip()[:MAX_TITLE_CHARS]
        description = str(description or "").strip()[:MAX_DESCRIPTION_CHARS]
        if not title or not description:
            return _result(error="A title and a description are required.")
        if not isinstance(fields, list) or not 1 <= len(fields) <= MAX_FIELDS:
            return _result(error=f"Ask for 1 to {MAX_FIELDS} values.")
        clean: list[dict[str, str]] = []
        for item in fields:
            if not isinstance(item, dict):
                return _result(error="Each field needs a name and a label.")
            try:
                name = check_name(item.get("name"))
            except SecretNameError as exc:
                return _result(error=str(exc))
            label = str(item.get("label") or "").strip()[:MAX_LABEL_CHARS] or name
            if any(existing["name"] == name for existing in clean):
                return _result(error=f"{name} is asked for twice.")
            clean.append({"name": name, "label": label})
        session_key = _session_key()
        if not session_key or _nobody_to_answer(session_key):
            return _result(error=("Nobody can answer a secret request in this turn. Tell the person what you "
                                  "need in your reply; they can save it in Settings, or you can ask with "
                                  "request_secret in their conversation."))
        saved = set(service.saved_names())
        already = [item["name"] for item in clean if item["name"] in saved]
        if already and not replace:
            return _result(status="already_saved", names=already,
                           note="These are saved; use them as $NAME. Ask again only to change one (replace=true).")
        try:
            key = get_vault().load_or_create().key_wire()
        except SecretsError as exc:
            return _result(error=str(exc), code=exc.code)
        try:
            request = await get_secret_requests().open(
                session_key=session_key, channel=self._channel, chat_id=self._chat_id, title=title,
                description=description, fields=clean, replace=bool(replace), key=key,
            )
        except ValueError as exc:
            return _result(error=str(exc))
        return _result(
            status="requested", requestId=request.id, names=request.names,
            note=("A card is waiting in the chat. Say in one short sentence what you asked for and why, then end "
                  "your reply; do not wait, poll or ask for the value in chat. The task continues in a new turn "
                  "when they answer."),
        )


class ListSecretsTool(Tool):
    @property
    def name(self) -> str:
        return "list_secrets"

    @property
    def description(self) -> str:
        return ("The names of the secrets saved for you (never their values), and the requests still waiting. "
                + _RULES)

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def execute(self, **_: Any) -> str:
        from flowly.secrets.vault import SecretsError, get_vault

        try:
            data = get_vault().load()
        except SecretsError as exc:
            return _result(error=str(exc), code=exc.code)
        secrets = [{"name": name, "description": secret.description}
                   for name, secret in sorted((data.secrets if data else {}).items())]
        waiting = [{"requestId": request.id, "names": request.names}
                   for request in get_secret_requests().pending()]
        return _result(secrets=secrets, waiting=waiting)
