"""How often the agent may write first, as the owner asks for it in words.

The owner can set it in the app; they can also just say "don't write so
much", "you can check in more", "nothing before ten". This tool is how the
agent honours that: it changes the same settings the app shows
(:mod:`flowly.agenda.store`), so the limits the code enforces follow at once.
It cannot raise anything past what the owner asked; it only records it.
"""

from __future__ import annotations

import json
from typing import Any

from flowly.agent.tools.base import Tool
from flowly.objectives.store import ObjectiveError


class ProactivityTool(Tool):
    @property
    def name(self) -> str:
        return "proactivity"

    @property
    def description(self) -> str:
        return (
            "Read or change how often you may write to the owner first, and their quiet hours. "
            "Use it only when the owner tells you how they want it (\"write less\", \"you can check in "
            "more\", \"never before 10\", \"stop messaging me first\"); then confirm in a few words. "
            "Levels: off (never first), low (1 a day), normal (2 a day), high (4 a day). "
            "Actions: get, set."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["get", "set"]},
                "level": {"type": "string", "enum": ["off", "low", "normal", "high"]},
                "quiet_start": {"type": "string", "description": "Start of quiet hours, HH:MM."},
                "quiet_end": {"type": "string", "description": "End of quiet hours, HH:MM."},
            },
            "required": ["action"],
        }

    async def execute(self, action: str = "get", **kwargs: Any) -> str:
        from flowly.agenda.store import get_agenda_store

        store = get_agenda_store()
        try:
            if action == "get":
                settings = store.settings()
                return json.dumps({k: settings.get(k) for k in ("level", "quietStart", "quietEnd")})
            if action == "set":
                changes = {
                    key: kwargs[arg]
                    for arg, key in (("level", "level"), ("quiet_start", "quietStart"), ("quiet_end", "quietEnd"))
                    if kwargs.get(arg)
                }
                if not changes:
                    return "Error: give a level or quiet hours to change."
                settings = store.update_settings(changes)
                return (
                    f"Saved: level {settings['level']}, quiet hours {settings['quietStart']}–{settings['quietEnd']}. "
                    "The owner sees the same in the app."
                )
        except ObjectiveError as exc:
            return f"Error: {exc}"
        return f"Error: unknown action {action!r}."
