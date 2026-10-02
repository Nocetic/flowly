"""Long-term memory writes from Live Voice, on the selected agent's own runtime.

Voice uses the agent's ordinary ``memory_append`` tool through the runtime's
tool registry, so the same content guard, duplicate protection, size cap and
tool routing apply as in a chat turn. Like ``voice.context`` this is served by
each runtime for itself: a named agent's note lands in that agent's memory.
"""
from __future__ import annotations

from typing import Any, Awaitable, Callable

from flowly.live_voice.sessions import VoiceError, bounded_text

MAX_NOTE_CHARS = 2000

Execute = Callable[[dict], Awaitable[Any]]


def validate_memory_append(params: dict) -> dict:
    if set(params) - {'content'}:
        raise VoiceError('INVALID_PARAMS', 'A voice memory note carries its content only; the runtime owns its scope.')
    return {'content': bounded_text(params.get('content'), 'content', maximum=MAX_NOTE_CHARS)}


def memory_receipt(result: Any) -> dict:
    """The tool's own outcome, typed for the voice backend. Never claims a save it did not make."""
    text = str(result or '')
    if text.startswith('Appended to MEMORY.md'):
        return {'status': 'saved'}
    if text.startswith('Rejected:'):
        return {'status': 'duplicate', 'reason': 'This is already in memory.'}
    if text.startswith("Error: Tool 'memory_append'"):
        # Not registered, routed off for voice, or outside the caller's permissions.
        return {'status': 'unavailable', 'reason': 'Memory writing is not available for this agent.'}
    if text.startswith('[blocked:'):
        return {'status': 'rejected', 'reason': 'A policy on this agent blocked the note.'}
    if text.startswith('Error writing memory') or not text.startswith('Error:'):
        return {'status': 'failed', 'reason': 'The note could not be saved.'}
    # The content guard's refusal (an injection or exfiltration pattern).
    return {'status': 'rejected', 'reason': 'The note looks like instructions or secret data and was not saved.'}


class VoiceMemory:
    def __init__(self, execute: Execute):
        self._execute = execute

    async def append(self, params: dict) -> dict:
        note = validate_memory_append(params)
        return memory_receipt(await self._execute(note))
