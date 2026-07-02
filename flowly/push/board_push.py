"""Board completion push notifications."""

from __future__ import annotations

from typing import Any

from loguru import logger


async def notify_board_finished(card: Any, outcome: str) -> None:
    """Send a best-effort board completion push via the relay.

    The relay_push registry owns the gateway-vs-relay distinction: gateway
    registrations become ``gatewayId`` payloads, relay registrations become
    ``serverId`` payloads. The app only needs ``type=board`` plus one of those
    identifiers to deep-link to the board.
    """
    if outcome not in ("done", "failed"):
        return
    try:
        from flowly.push import relay_push

        name = (getattr(card, "title", "") or "task").strip()
        if outcome == "failed":
            body = f"failed: {(getattr(card, 'error', '') or '').strip()}"
        else:
            result = (getattr(card, "result", "") or "").strip()
            body = next((ln.strip() for ln in result.splitlines() if ln.strip()), "done")
        data = {
            "type": "board",
            "cardId": str(getattr(card, "id", "") or ""),
            "outcome": outcome,
        }
        data = {k: v for k, v in data.items() if v}
        await relay_push.notify_devices(
            f"Board · {name}"[:80],
            body[:140],
            data=data,
        )
    except Exception as exc:  # pragma: no cover
        logger.debug(f"[board] push notify skipped: {exc}")
