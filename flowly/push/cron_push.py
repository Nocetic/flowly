"""Cron push policy shared by the primary and isolated profile runtimes."""

from __future__ import annotations

from typing import Any


def should_push_cron_completion(job: Any, data: dict) -> bool:
    if not data.get("outputPersisted") or data.get("silent"):
        return False
    if not getattr(getattr(job, "payload", None), "deliver", False):
        return False
    origin = getattr(job, "origin", None)
    channel = (
        (getattr(origin, "platform", None) if origin else None)
        or getattr(job.payload, "channel", None)
        or ""
    )
    target = getattr(job.payload, "to", None)
    return not target or channel in ("cli", "tui", "desktop", "ios", "android")


def cron_notification_snapshot(
    job: Any, response: str | None, *, silent: bool, terminal: bool
) -> dict:
    """Persist only the preview and delivery policy for this exact finished run."""
    return {
        "eligible": should_push_cron_completion(
            job, {"outputPersisted": True, "silent": silent}
        ) and terminal,
        "body": next(
            (line.strip() for line in (response or "").splitlines() if line.strip()),
            job.name or "Scheduled task",
        )[:140],
    }
