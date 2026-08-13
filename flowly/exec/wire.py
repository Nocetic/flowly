"""Wire serialization for exec/tool approvals.

One schema, three transports. ``exec.approval.requested`` (gateway WS),
``exec.approval.requested`` (relay, via the web channel) and
``exec.approval.list`` (gateway RPC) all describe the same object, so they all
serialize through here — otherwise the three drift and a surface that works
over the local gateway breaks over the relay.

The key names are the ones surfaces already read: ``flowly/tui/client.py``
parses ``reasons``/``riskReasons``, ``cwd``, ``resolvedPath`` and
``supportsAlways`` off this payload today. Everything here already lives on
:class:`~flowly.exec.types.PendingApproval`; nothing is computed at
serialization time.
"""

from __future__ import annotations

from typing import Any

from flowly.exec.types import PendingApproval


def approval_to_wire(pending: PendingApproval) -> dict[str, Any]:
    """Serialize a pending approval for any surface.

    ``riskReasons`` comes from :func:`flowly.exec.safety.analyze_command` — the
    surface renders *why* the prompt fired ("Matches dangerous command
    pattern", "Touches protected path: ~/.ssh/id_rsa") instead of an
    unexplained approve/deny. Surfaces must not recompute it: duplicating the
    dangerous-pattern table client-side guarantees the two drift apart.

    ``supportsAlways`` is false whenever remembering the decision would do
    nothing — a sent email can't be allowlisted. A surface that offers "Always
    allow" anyway shows a button that silently re-asks next time.
    """
    return {
        "id": pending.id,
        "command": pending.request.command,
        "kind": getattr(pending, "kind", "action"),
        "sessionKey": pending.session_key,
        "createdAt": pending.created_at,
        "expiresAt": pending.expires_at,
        "supportsAlways": getattr(pending, "supports_always", True),
        "riskReasons": list(pending.risk_reasons or []),
        "cwd": pending.request.cwd,
        "resolvedPath": pending.resolved_path,
    }


def approval_closed_to_wire(
    approval_id: str, reason: str, session_key: str | None,
) -> dict[str, Any]:
    """Serialize "this approval stopped waiting".

    ``reason`` is the decision that settled it (``allow-once`` /
    ``allow-always`` / ``deny``) or ``timeout``. A surface uses it to retire a
    prompt it drew — without this, a command approved on the phone keeps its
    card on screen everywhere else, and the stale card's own countdown
    eventually fires a ``deny`` against an id the manager has already
    forgotten.
    """
    return {
        "id": approval_id,
        "reason": reason,
        "sessionKey": session_key or "",
    }
