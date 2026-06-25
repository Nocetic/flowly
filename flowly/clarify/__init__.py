"""Clarify — agent-initiated disambiguation.

When the agent is uncertain how to proceed it can ask the user a focused
question, optionally offering a short list of choices. Surfaces (desktop,
TUI, mobile, chat channels) render the question, the user picks an option
or types a free-form answer, and the agent resumes with that answer.

The round-trip mirrors the exec-approval design: an asyncio.Future pauses
the agent coroutine while any connected surface resolves it.
"""

from flowly.clarify.types import ClarifyRequest
from flowly.clarify.manager import ClarifyManager, get_clarify_manager

__all__ = ["ClarifyRequest", "ClarifyManager", "get_clarify_manager"]
