"""Type definitions for agent clarify requests."""

from __future__ import annotations

from dataclasses import dataclass, field


# A clarify request offers at most this many predefined choices. Surfaces
# always append their own "type your own answer" affordance on top, so the
# agent never needs to (and must not) include a catch-all option itself.
MAX_CHOICES = 4


@dataclass
class ClarifyRequest:
    """A pending question the agent is waiting on an answer for.

    ``choices`` is either a short list of predefined options or ``None``
    for a purely open-ended question. The user's reply is always returned
    to the agent as free text — when they pick an option the surface sends
    that option's text back verbatim.
    """

    id: str
    question: str
    choices: list[str] | None = None
    session_key: str | None = None
    created_at: float = 0.0
    expires_at: float = 0.0
    risk_reasons: list[str] = field(default_factory=list)
