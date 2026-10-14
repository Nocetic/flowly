"""A change to the goals is announced, so the goals screens read them again.

The screens (Desktop's agent panel, the phones' "Goals and routines") used to
poll every minute: a goal the agent opened in a chat or a step ticked on
another device showed up a minute late. ``flowly/objectives/events.py`` raises
one ``objectives.changed`` (no goal content) per burst of committed changes; it
reaches local clients, relay sessions showing the goals, and remote clients of
a named agent as a profile event.
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from flowly.agenda.store import get_agenda_store
from flowly.bus.queue import MessageBus
from flowly.channels import feature_rpc
from flowly.channels.web import WebChannel
from flowly.config.schema import WebChannelConfig
from flowly.objectives import events
from flowly.objectives.store import get_objective_store
from flowly.profile import get_flowly_home

CHAT = "desktop:chat-1"


@pytest.fixture(autouse=True)
def fresh_home():
    home = get_flowly_home()
    for path in (home / "agenda" / "agenda.json", home / "objectives" / "objectives.json"):
        path.unlink(missing_ok=True)
    yield
    if events.announcing():
        events.set_broadcaster(None)


@pytest.fixture
async def heard(monkeypatch):
    monkeypatch.setattr(events, "COALESCE_SECONDS", 0.01)
    frames: list[dict] = []

    async def broadcaster(data: dict) -> None:
        frames.append(data)

    events.set_broadcaster(broadcaster)
    yield frames
    events.set_broadcaster(None)


async def _settle() -> None:
    await asyncio.sleep(0.05)


async def test_a_burst_of_changes_is_announced_once(heard):
    goal = get_objective_store().create(title="Run 5k", category="health", session_key=CHAT)
    get_objective_store().log(goal["id"], "Ran 2k")
    get_agenda_store().schedule(kind="objective", due_at=4_000_000_000_000, note="Day 1",
                                session_key=CHAT, objective_id=goal["id"])
    await _settle()
    assert len(heard) == 1 and set(heard[0]) == {"at"}
    get_objective_store().update(goal["id"], {"title": "Run 10k"})
    await _settle()
    assert len(heard) == 2


async def test_a_change_from_a_worker_thread_is_announced_on_the_loop(heard):
    worker = threading.Thread(target=lambda: get_objective_store().create(
        title="Read", category="learning", session_key=CHAT))
    worker.start()
    worker.join()
    await _settle()
    assert len(heard) == 1


async def test_only_what_a_goals_screen_shows_is_announced_from_the_agenda(heard):
    agenda = get_agenda_store()
    agenda.schedule(kind="follow_up", due_at=4_000_000_000_000, note="Ask about the trip", session_key=CHAT)
    agenda.owner_wrote(CHAT)
    await _settle()
    assert heard == []  # a follow-up with no goal is not on the goals screen
    agenda.update_settings({"quietStart": "23:00"})
    await _settle()
    assert len(heard) == 1


async def test_the_announcement_never_runs_in_the_writers_context(heard, monkeypatch):
    # A change made inside one account's request must not be scoped to it.
    marker: contextvars.ContextVar[str | None] = contextvars.ContextVar("marker", default=None)
    seen: list[str | None] = []

    async def broadcaster(data: dict) -> None:
        seen.append(marker.get())

    events.set_broadcaster(broadcaster)
    marker.set("owner-a")
    get_objective_store().create(title="Save", category="finance", session_key=CHAT)
    await _settle()
    assert seen == [None]


async def test_without_a_broadcaster_a_change_is_simply_not_announced():
    assert not events.announcing()
    get_objective_store().create(title="Read", category="learning", session_key=CHAT)


async def test_a_failing_broadcaster_never_breaks_a_write(monkeypatch):
    monkeypatch.setattr(events, "COALESCE_SECONDS", 0.01)

    async def broken(data: dict) -> None:
        raise RuntimeError("socket gone")

    events.set_broadcaster(broken)
    get_objective_store().create(title="Read", category="learning", session_key=CHAT)
    await _settle()
    assert len(get_objective_store().list()) == 1


async def test_clients_are_told_the_goals_announce_changes(heard):
    assert feature_rpc.system_capabilities()["objectivesEventsVersion"] == 1
    events.set_broadcaster(None)
    assert feature_rpc.system_capabilities()["objectivesEventsVersion"] == 0


def _channel() -> WebChannel:
    channel = WebChannel(WebChannelConfig(), MessageBus())
    channel._ws = AsyncMock()
    return channel


def _sent(channel: WebChannel) -> list[dict]:
    return [json.loads(call.args[0]) for call in channel._ws.send.await_args_list]


async def test_relay_sessions_that_read_the_goals_hear_they_changed():
    channel = _channel()
    await channel._handle_feature_rpc(channel._ws, "1", "phone", "objectives.list", {})
    await channel._handle_feature_rpc(channel._ws, "2", "laptop", "agenda.settings.get", {})
    await channel._handle_feature_rpc(channel._ws, "3", "stranger", "sessions.list", {})
    channel._ws.reset_mock()

    await channel.send_objectives_event({"at": 1})

    frames = _sent(channel)
    assert sorted(f["sessionId"] for f in frames) == ["laptop", "phone"]
    assert {f["event"] for f in frames} == {"objectives.changed"}


async def test_a_relay_session_stops_hearing_when_it_leaves_or_its_watch_ends():
    channel = _channel()
    for session in ("phone", "tablet"):
        await channel._handle_feature_rpc(channel._ws, "1", session, "objectives.list", {})
    await channel._handle_relay_message(channel._ws, {"type": "browser-disconnected", "sessionId": "phone"})
    channel._objectives_watchers["tablet"] = time.monotonic() - 1
    channel._ws.reset_mock()

    await channel.send_objectives_event({"at": 1})

    assert _sent(channel) == []


async def test_a_new_goals_screen_always_hears_even_at_the_limit(monkeypatch):
    import flowly.channels.web as web_module

    monkeypatch.setattr(web_module, "_OBJECTIVES_WATCH_LIMIT", 2)
    channel = _channel()
    for session in ("a", "b", "c"):
        await channel._handle_feature_rpc(channel._ws, "1", session, "objectives.list", {})
    assert set(channel._objectives_watchers) == {"b", "c"}


_CHANGED = {"hostId": "host", "profile": "writer", "botId": "bot", "type": "objectives.changed",
            "data": {"at": 1}}


async def test_relay_carries_a_named_agents_change_to_whatever_shows_it():
    channel = WebChannel(WebChannelConfig(), MessageBus())
    sent: list[dict] = []
    channel._send_or_queue = AsyncMock(side_effect=lambda frame: sent.append(json.loads(frame)))
    channel._deliver_relay_outbound = AsyncMock(
        side_effect=lambda pending: sent.append(json.loads(pending.payload)) or True)
    now = time.monotonic()
    channel._profile_directory_sessions = {"directory": now}
    channel._profile_conversation_sessions = {
        ("writer", "ios:a"): {"reader": now},
        ("reviewer", "ios:b"): {"other": now},
    }

    await channel._forward_profile_event(_CHANGED)

    assert sorted(frame["sessionId"] for frame in sent) == ["directory", "reader"]
    assert {frame["data"]["type"] for frame in sent} == {"objectives.changed"}


async def test_a_direct_gateway_carries_a_named_agents_change_to_whatever_shows_it(tmp_path, monkeypatch):
    import flowly.profile as profiles
    from flowly.gateway.server import GatewayServer

    default = tmp_path / ".flowly"
    monkeypatch.setattr(profiles, "_DEFAULT_HOME", default)
    monkeypatch.setattr(profiles, "_PROFILES_ROOT", default / "profiles")
    monkeypatch.setenv("FLOWLY_HOME", str(default))
    (default / "workspace").mkdir(parents=True)
    profiles.create_profile("writer", local_runtime=True)
    profiles.create_profile("reviewer", local_runtime=True)

    def socket():
        ws = SimpleNamespace(closed=False, messages=[])

        async def send_json(payload):
            ws.messages.append(payload)

        async def close():
            ws.closed = True

        ws.send_json, ws.close = send_json, close
        return ws

    server = GatewayServer(host="127.0.0.1", enable_profile_host=True)
    reader, other, directory = socket(), socket(), socket()
    server._ws_clients.update({"reader": reader, "other": other, "directory": directory})
    server._bind_profile_client_request("reader", "profiles.rpc", {
        "name": "writer", "method": "objectives.list", "params": {}})
    server._bind_profile_client_request("other", "profiles.rpc", {
        "name": "reviewer", "method": "objectives.list", "params": {}})
    server._bind_profile_client_request("directory", "profiles.list", {})

    await server._broadcast_profile_host_event(_CHANGED)

    assert [m["data"]["type"] for m in directory.messages] == ["objectives.changed"]
    assert [m["data"]["type"] for m in reader.messages] == ["objectives.changed"]
    assert other.messages == []
    await server.stop()


async def test_the_main_agents_change_reaches_apps_showing_the_host_without_a_lease(tmp_path, monkeypatch):
    # A remote goals panel reads the main agent through profiles.rpc and holds
    # no lease on its events; the change must reach it all the same, once.
    import flowly.profile as profiles
    from flowly.profile_host import ProfileHost

    default = tmp_path / ".flowly"
    monkeypatch.setattr(profiles, "_DEFAULT_HOME", default)
    monkeypatch.setattr(profiles, "_PROFILES_ROOT", default / "profiles")
    monkeypatch.setenv("FLOWLY_HOME", str(default))
    (default / "workspace").mkdir(parents=True)
    host = ProfileHost()
    seen: list[dict] = []

    async def collect(envelope):
        seen.append(envelope)

    host.subscribe_events(collect)
    await host.announce_primary_objectives({"at": 1})
    host.retain_default_events("gateway:desktop")
    await host.handle_primary_frame({"type": "event", "event": "objectives.changed", "data": {"at": 1}})

    assert [(e["profile"], e["type"], e["data"]) for e in seen] == [("default", "objectives.changed", {"at": 1})]
