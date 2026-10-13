"""Ring the phone for a message a named agent wrote first.

A named agent's runtime holds no device registrations (they live on the
primary host; see ``docs/engineering/profile-cron-push-2026-09-21.md``). When
its agenda delivers a message it only announces ``agenda.delivered`` with the
wake-up's id. The primary host, observing the runtime, or Desktop, managing
it, forwards that identity to ``profiles.agenda.notify``; this module reads
the exact settled wake-up from that agent's own agenda and pushes its preview.
A caller never supplies the text, the destination or a key, so it cannot
make the phone say anything the agent did not deliver.
"""

from __future__ import annotations

import re

from flowly.profile import ensure_profile_bot_id, validate_profile_name
from flowly.profile_host_contract import ProfileHostError
from flowly.push import relay_push

_ITEM_ID = re.compile(r"wk_[0-9a-f]{12}")


async def notify_profile_agenda(host_id: str, name: str, item_id: str) -> dict:
    if name == "default":
        return {"ok": True, "sent": False}
    try:
        validate_profile_name(name)
    except (TypeError, ValueError) as exc:
        raise ProfileHostError("INVALID_PARAMS", "Invalid profile name.") from exc
    if not isinstance(item_id, str) or not _ITEM_ID.fullmatch(item_id):
        raise ProfileHostError("INVALID_PARAMS", "Invalid agenda identity.")

    from flowly.agenda.store import AgendaStore

    profile = ensure_profile_bot_id(name)
    store = AgendaStore(profile.path)
    record = next(
        (item for item in reversed(store.snapshot()["history"]) if item.get("id") == item_id),
        None,
    )
    if record is None or record.get("outcome") != "delivered":
        return {"ok": True, "sent": False}
    if not relay_push.get_push_registry().list():
        return {"ok": True, "sent": False}

    # The primary host and Desktop may both forward the same delivery; one
    # claim in the agent's own agenda folder makes it one push.
    claim = store.path.parent / f".{item_id}.mobile-push"
    try:
        with claim.open("x", encoding="utf-8") as file:
            file.write(host_id)
    except FileExistsError:
        return {"ok": True, "sent": False, "duplicate": True}

    from flowly.push import notifications

    await notifications.deliver(notifications.Notice(
        kind="chat",
        key=notifications.event_key("chat", "agenda", name, item_id),
        title=profile.display_name or name,
        body=str(record.get("preview") or "") or "Sent you a message.",
        data={
            "agenda": "1",
            "profileHostId": host_id,
            "profileBotId": profile.bot_id,
            "profileName": name,
        },
    ))
    return {"ok": True, "sent": True}
