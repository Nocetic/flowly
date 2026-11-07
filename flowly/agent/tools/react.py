"""React tool: put one emoji on the user's latest message, the way a person does in a chat.

A reaction can acknowledge a task without a word (👍, then keep working), or be
the whole answer when nothing needs saying (❤️ after a thank-you). The user
reacts to the agent's messages too; the server tells the agent with a bracketed
note before their next message, or at once for 👎 / 🤔.
"""

from __future__ import annotations

from typing import Any

from flowly.agent.reactions import get_reaction_hub, is_reaction_emoji
from flowly.agent.tools.base import Tool


class ReactTool(Tool):
    @property
    def name(self) -> str:
        return "react"

    @property
    def description(self) -> str:
        return (
            "React to the user's latest message with a single emoji, the way a person does in a chat. "
            "Use it to acknowledge without words: 👍 when you take on a task (then keep working, no "
            "'on it' message needed), ❤️ or 🙏 when they thank you, 😂 when something is funny. "
            "A reaction can be your whole answer when nothing needs saying (a thank-you, an 'ok'): "
            "then end your turn without text. Use it sparingly, never on every message, and never "
            "instead of answering a question or reporting results. "
            "The user reacts to your messages too; you see it as a note like "
            "[The user reacted 👎 to your message: \"…\"]. 👎 😕 ❌ mean something was wrong: ask "
            "briefly what, or fix it. 🤔 ❓ mean it was unclear: explain. Other reactions need no "
            "reply; never thank them for a 👍."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "emoji": {
                    "type": "string",
                    "description": "One emoji, e.g. 👍 ❤️ 🙏 😂 🎉 👀. Nothing else: no words, no spaces.",
                },
            },
            "required": ["emoji"],
        }

    @property
    def supported_platforms(self) -> frozenset[str]:
        # Chats that draw reactions: the Flowly server and relay (web) and a
        # named agent's chat through its host (desktop). Not Telegram & co.
        return frozenset({"web", "desktop"})

    async def execute(self, **kwargs: Any) -> str:
        emoji = str(kwargs.get("emoji") or "").strip()
        if not is_reaction_emoji(emoji):
            return "Error: emoji must be a single emoji, nothing else."
        session_key = str(kwargs.get("session_key") or "")
        hub = get_reaction_hub()
        if not session_key or not hub.connected:
            return "Error: reactions are not available in this chat."
        if not await hub.emit(session_key, emoji):
            return "Error: the reaction could not be shown."
        return f"Reacted {emoji} to the user's latest message."
