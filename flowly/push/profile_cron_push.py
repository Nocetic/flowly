"""Send an isolated profile's retained cron result through its owning host."""

from __future__ import annotations

import re

from flowly.cron.service import CronService
from flowly.profile import ensure_profile_bot_id, validate_profile_name
from flowly.profile_host_contract import ProfileHostError
from flowly.push import relay_push


async def notify_profile_cron(host_id: str, name: str, job_id: str, run_id: str) -> dict:
    # Desktop forwards identities, never notification text, destinations or keys.
    # Resolve the archive on the host so callers cannot manufacture a push body.
    if name == "default":
        return {"ok": True, "sent": False}
    try:
        validate_profile_name(name)
    except (TypeError, ValueError) as exc:
        raise ProfileHostError("INVALID_PARAMS", "Invalid profile name.") from exc
    for value in (job_id, run_id):
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
            raise ProfileHostError("INVALID_PARAMS", "Invalid cron result identity.")
    profile = ensure_profile_bot_id(name)
    service = CronService(profile.path / "cron" / "jobs.json")
    # Do not load/start the scheduler or write jobs.json in a second process.
    record = next(
        (r for r in service._read_run_records(job_id) if r.get("runId") == run_id),
        None,
    )
    if record is None or record.get("jobId") != job_id or record.get("expired"):
        return {"ok": True, "sent": False}
    notification = record.get("mobileNotification")
    if not isinstance(notification, dict) or notification.get("eligible") is not True:
        return {"ok": True, "sent": False}
    if not relay_push.get_push_registry().list():
        return {"ok": True, "sent": False}

    # Both Desktop and the headless host may observe the same runtime. A tiny
    # archive-local claim survives reconnects and host restarts. Delivery keeps
    # the existing best-effort semantics; this is not a push retry queue.
    claim = service._job_output_dir(job_id) / f".{run_id}.mobile-push"
    try:
        with claim.open("x", encoding="utf-8") as file:
            file.write(host_id)
    except FileExistsError:
        return {"ok": True, "sent": False, "duplicate": True}

    from flowly.push import notifications

    await notifications.deliver(notifications.Notice(
        kind="cron",
        key=notifications.event_key("cron", name, job_id, run_id),
        title=f"{profile.display_name or name} · {record.get('jobName') or 'Scheduled task'}",
        body=str(notification.get("body") or record.get("jobName") or "Scheduled task"),
        data={
            "jobId": job_id,
            "jobName": str(record.get("jobName") or "Scheduled task"),
            "runId": run_id,
            "profileHostId": host_id,
            "profileBotId": profile.bot_id,
            "profileName": name,
        },
    ))
    return {"ok": True, "sent": True}
