"""A named agent's first message rings the phone through the primary host."""

from __future__ import annotations

import pytest

from flowly import profile as profiles
from flowly.agenda.store import AgendaStore
from flowly.profile_host_contract import ProfileHostError
from flowly.push import notifications, profile_agenda_push, relay_push


@pytest.fixture
def named(tmp_path, monkeypatch):
    default = tmp_path / "flowly"
    monkeypatch.setattr(profiles, "_DEFAULT_HOME", default)
    monkeypatch.setattr(profiles, "_PROFILES_ROOT", default / "profiles")
    home = profiles.create_profile("coach", description="")
    sent: list = []

    async def deliver(notice):
        sent.append(notice)
        return True

    monkeypatch.setattr(notifications, "deliver", deliver)
    monkeypatch.setattr(relay_push.get_push_registry(), "list", lambda: [{"pushId": "p1"}])
    return home, sent


def _delivered(home, preview="How did the run go?") -> str:
    store = AgendaStore(home)
    item = store.schedule(kind="follow_up", due_at=1, note="run", session_key="desktop:profile-home")
    store.start(item["id"])
    store.settle(item["id"], "delivered", preview=preview)
    return item["id"]


@pytest.mark.asyncio
async def test_the_primary_pushes_what_the_agent_delivered_once(named):
    home, sent = named
    item_id = _delivered(home)
    first = await profile_agenda_push.notify_profile_agenda("host-1", "coach", item_id)
    again = await profile_agenda_push.notify_profile_agenda("host-1", "coach", item_id)
    assert first == {"ok": True, "sent": True}
    assert again == {"ok": True, "sent": False, "duplicate": True}
    [notice] = sent
    assert notice.kind == "chat" and notice.body == "How did the run go?"
    assert notice.data["profileName"] == "coach" and notice.data["profileHostId"] == "host-1"


@pytest.mark.asyncio
async def test_nothing_rings_for_a_silent_or_unknown_wake_up(named):
    home, sent = named
    store = AgendaStore(home)
    item = store.schedule(kind="follow_up", due_at=1, note="x", session_key="desktop:profile-home")
    store.settle(item["id"], "silent")
    assert (await profile_agenda_push.notify_profile_agenda("h", "coach", item["id"]))["sent"] is False
    assert (await profile_agenda_push.notify_profile_agenda("h", "coach", "wk_000000000000"))["sent"] is False
    assert (await profile_agenda_push.notify_profile_agenda("h", "default", item["id"]))["sent"] is False
    assert sent == []


@pytest.mark.asyncio
@pytest.mark.parametrize("name, item_id", [("../x", "wk_aaaaaaaaaaaa"), ("coach", "wk_x"), ("coach", "../../etc")])
async def test_identities_are_validated(named, name, item_id):
    with pytest.raises(ProfileHostError):
        await profile_agenda_push.notify_profile_agenda("h", name, item_id)


@pytest.mark.asyncio
async def test_the_primary_host_rings_when_a_named_runtime_announces_a_delivery(tmp_path, monkeypatch):
    import asyncio

    from flowly.profile_host import ProfileHost

    home = tmp_path / ".flowly"
    home.mkdir()
    monkeypatch.setattr(profiles, "_DEFAULT_HOME", home)
    monkeypatch.setattr(profiles, "_PROFILES_ROOT", home / "profiles")
    monkeypatch.setenv("FLOWLY_HOME", str(home))
    profiles.create_profile("writer", local_runtime=True)
    registry = relay_push.PushRegistry(home / "push_subs.json")
    registry.register(push_id="ios-device", push_secret="test", gateway_id="ios-gateway", platform="ios")
    monkeypatch.setattr(relay_push, "_registry", registry)
    monkeypatch.setattr(relay_push, "_relay_base", lambda: "https://relay.test")
    sent: list = []
    monkeypatch.setattr(relay_push, "_send_one", lambda base, sub, title, body, data: sent.append((title, body, data)) or 200)

    item_id = _delivered(home / "profiles" / "writer", preview="Your draft is ready.")
    host = ProfileHost()
    await host._handle_profile_event("writer", "agenda.delivered", {"itemId": item_id, "sessionKey": "desktop:profile-home"})
    await asyncio.gather(*tuple(host._background_tasks))
    [(title, body, data)] = sent
    assert body == "Your draft is ready." and data["profileName"] == "writer"
    # Desktop forwarding the same delivery does not ring twice.
    again = await host.dispatch("profiles.agenda.notify", {"name": "writer", "itemId": item_id})
    assert again["sent"] is False and len(sent) == 1
