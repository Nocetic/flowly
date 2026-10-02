"""Flowlet reminder push notifications.

Mirrors board_push: a best-effort APNs/FCM push through the relay registry
(gateway registrations → ``gatewayId`` payloads, relay → ``serverId``). The
client only needs ``type=flowlet`` + ``flowletId`` to deep-link to the screen.

Desktop clients don't get an APNs push (the desktop *is* the bot); they receive
a ``flowlet.reminder`` gateway event instead — the caller passes ``broadcast``
(the same fan-out the flowlet tool already uses) and we emit that event so the
NativeNotificationsListener can raise a native OS notification.
"""

from __future__ import annotations

from typing import Awaitable, Callable

from loguru import logger


async def notify_flowlet(
    flowlet_id: str,
    title: str,
    body: str,
    *,
    broadcast: Callable[[str, dict], Awaitable[None]] | None = None,
) -> None:
    """Send a flowlet reminder to the user's devices (APNs/FCM) and, when a
    ``broadcast`` fan-out is provided, a ``flowlet.reminder`` event for desktop."""
    title = (title or "Flowlet").strip()[:80]
    body = (body or "").strip()[:140]

    # Mobile (backgrounded iOS/Android) — APNs/FCM via the relay registry.
    try:
        from flowly.push import notifications

        # A reminder may legitimately repeat, so every one is its own event.
        await notifications.deliver(notifications.Notice(
            kind="flowlet",
            key=notifications.unique_key("flowlet", flowlet_id),
            title=title,
            body=body,
            data={"flowletId": str(flowlet_id or "")},
        ))
    except Exception as exc:  # pragma: no cover — push is best-effort
        logger.debug("[flowlet] push notify skipped: {}", exc)

    # Desktop (connected gateway clients) — a native OS notification.
    if broadcast is not None:
        try:
            await broadcast(
                "flowlet.reminder",
                {"id": str(flowlet_id or ""), "title": title, "body": body},
            )
        except Exception as exc:  # pragma: no cover
            logger.debug("[flowlet] reminder broadcast skipped: {}", exc)
