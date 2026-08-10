"""Wire serialization for agent clarify questions.

Sibling of :mod:`flowly.exec.wire`: ``agent.clarify.requested`` (gateway WS and
relay) and ``agent.clarify.list`` (gateway RPC) describe the same object, so
they share one serializer.

``choices`` is ``None`` for an open-ended question and at most
:data:`~flowly.clarify.types.MAX_CHOICES` entries otherwise. Surfaces always
add their own free-text affordance on top — the agent never supplies a
catch-all option — so a surface that can only render buttons is not a complete
clarify surface.
"""

from __future__ import annotations

from typing import Any

from flowly.clarify.types import ClarifyRequest


def clarify_to_wire(pending: ClarifyRequest) -> dict[str, Any]:
    """Serialize a pending clarify question for any surface."""
    return {
        "id": pending.id,
        "question": pending.question,
        "choices": pending.choices,
        "sessionKey": pending.session_key,
        "createdAt": pending.created_at,
        "expiresAt": pending.expires_at,
        "riskReasons": list(pending.risk_reasons or []),
    }


def clarify_closed_to_wire(
    clarify_id: str, reason: str, session_key: str | None,
) -> dict[str, Any]:
    """Serialize "this question stopped waiting" (``answered`` / ``timeout``)."""
    return {
        "id": clarify_id,
        "reason": reason,
        "sessionKey": session_key or "",
    }
