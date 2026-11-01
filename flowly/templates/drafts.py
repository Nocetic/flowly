"""A drafted template leaves this agent as one ``template.drafted`` event.

The gateway registers how the event is carried (``flowly.cli.gateway_cmd``):
to the Flowly server for the main agent, to the agent host for a named one
(which passes it on under the agent's profile). The server keeps the draft
and puts a card in the conversation; the owner publishes it there
(flowly-server PROTOCOL B6d).
"""
from __future__ import annotations

import time
import uuid
from collections.abc import Awaitable, Callable

DraftedCallback = Callable[[str, dict], Awaitable[None]]

_callbacks: list[DraftedCallback] = []


class TemplatesUnavailableError(RuntimeError):
    """Nothing carries a draft from this process (no gateway, or a CLI turn)."""


def add_drafted_callback(callback: DraftedCallback) -> None:
    _callbacks.append(callback)


def reset_for_tests() -> None:
    _callbacks.clear()


async def announce(session_key: str, recipe: dict) -> str:
    """Send the draft once; its ``draftId``."""
    if not _callbacks:
        raise TemplatesUnavailableError("Templates need this agent's connection to Flowly.")
    draft_id = uuid.uuid4().hex
    data = {"draftId": draft_id, "sessionKey": session_key, "recipe": recipe, "createdAt": int(time.time())}
    for callback in tuple(_callbacks):
        await callback(session_key, data)
    return draft_id
