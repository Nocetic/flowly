"""Authenticated subscription ownership, ordering, cleanup and bounded delivery."""

import asyncio
import os
from threading import Event
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import flowly.profile as profiles
from flowly.desktop_runtime.authority import ScreenError
from flowly.gateway.display import _resolve
from flowly.gateway.display_watch import DisplaySubscriptions
from tests.test_screen_display_gateway import display_gateway as _display_gateway

display_gateway = _display_gateway
pytestmark = pytest.mark.skipif(os.name == "nt", reason="private Unix RFB transport is Linux-host only")


@pytest.fixture(autouse=True)
def fast_watch(monkeypatch):
    monkeypatch.setattr(DisplaySubscriptions, "INTERVAL", 0.01)


async def subscribe(fixture, profile="default"):
    status = (await fixture.rpc("display.status", {"profileId": profile}))["result"]
    result = await fixture.rpc("display.subscribe", {"profileId": profile,
        "expectedHostId": status["hostId"], "expectedBotId": status["botId"]})
    assert result["type"] == "rpc"  # The initial snapshot precedes all events.
    return result["result"]


async def test_subscription_requires_pinned_identity(display_gateway):
    fixture = display_gateway
    rejected = await fixture.rpc("display.subscribe", {"profileId": "default"})
    assert rejected["error"]["code"] == "PROFILE_IDENTITY_CHANGED"
    assert not fixture.gateway._display_subscriptions.entries


async def test_implicit_profile_is_captured_before_following_status(display_gateway, monkeypatch):
    fixture = display_gateway
    initial = (await fixture.rpc("display.status"))["result"]
    result = await fixture.rpc("display.subscribe", {"expectedHostId": initial["hostId"], "expectedBotId": initial["botId"]})
    assert result["result"]["status"]["profileId"] == "default"
    session = _resolve(fixture.gateway, {})
    running = session.runtime.status()
    session.runtime.status = lambda: {**running, "phase": "failed"}
    monkeypatch.setattr(profiles, "current_profile_name", lambda: "writer")
    event = await fixture.socket.receive_json(timeout=3)
    assert event["event"] == "display.status"
    assert event["data"]["profileId"] == "default"


async def test_restored_older_revision_closes_instead_of_sending_stale_heartbeats(display_gateway, monkeypatch):
    watch = await subscribe(display_gateway)
    session = _resolve(display_gateway.gateway, {})
    monkeypatch.setattr(session, "status", lambda: {**watch["status"], "statusRevision": 0})
    event = await display_gateway.socket.receive_json(timeout=3)
    assert event["event"] == "display.subscription.closed"
    assert event["data"]["code"] == "STATUS_STATE_INVALID"
    assert event["data"]["statusRevision"] == watch["status"]["statusRevision"]


async def test_phase_events_advance_without_control_change_and_unsubscribe_drains(display_gateway):
    fixture = display_gateway
    watch = await subscribe(fixture)
    session = _resolve(fixture.gateway, {})
    running = session.runtime.status()
    session.runtime.status = lambda: {**running, "phase": "failed", "error": "RUNTIME_FAILED"}
    status, failed = [await fixture.socket.receive_json(timeout=3) for _ in range(2)]
    assert [status["event"], failed["event"]] == ["display.status", "display.runtime.failed"]
    assert status["data"] == failed["data"]
    assert status["data"]["statusRevision"] > watch["status"]["statusRevision"]
    assert status["data"]["control"] == watch["status"]["control"]
    assert status["data"]["subscriptionId"] == watch["subscriptionId"]
    assert not {"socket", "cdpPort", "viewerToken", "ticket"} & status["data"].keys()
    unsubscribed = await fixture.rpc("display.unsubscribe", {"subscriptionId": watch["subscriptionId"]})
    assert unsubscribed["result"] == {"unsubscribed": True}
    assert not fixture.gateway._display_subscriptions.entries


async def test_unsubscribe_preserves_human_ownership(display_gateway):
    fixture = display_gateway
    watch = await subscribe(fixture)
    runtime = _resolve(fixture.gateway, {}).runtime
    human = runtime.authority.acquire("human", "generation-default", 1)
    events = [await fixture.socket.receive_json(timeout=3) for _ in range(2)]
    assert [event["event"] for event in events] == ["display.status", "display.control"]
    assert events[-1]["data"]["control"] == human
    await fixture.rpc("display.unsubscribe", {"subscriptionId": watch["subscriptionId"]})
    assert runtime.authority.snapshot() == human


async def test_profile_replacement_closes_subscription_with_original_identity(display_gateway, monkeypatch):
    fixture = display_gateway
    watch = await subscribe(fixture, "writer")
    original = profiles.ensure_profile_bot_id

    def replaced(name):
        profile = original(name)
        return SimpleNamespace(path=profile.path, bot_id="replacement")

    monkeypatch.setattr(profiles, "ensure_profile_bot_id", replaced)
    event = await fixture.socket.receive_json(timeout=3)
    assert event["event"] == "display.subscription.closed"
    assert event["data"]["botId"] == watch["status"]["botId"]
    assert event["data"]["code"] == "PROFILE_IDENTITY_CHANGED"
    async with asyncio.timeout(3):
        while fixture.gateway._display_subscriptions.entries:
            await asyncio.sleep(0.01)


async def test_subscription_cannot_be_removed_by_another_authenticated_connection(display_gateway):
    fixture = display_gateway
    watch = await subscribe(fixture)
    async with fixture.client.post(fixture.server.make_url("/api/auth/ws-ticket"),
                                  headers={"Authorization": "Bearer display-test-owner"}) as response:
        ticket = (await response.json())["ticket"]
    async with fixture.client.ws_connect(fixture.server.make_url("/ws?ticket=" + ticket)) as other:
        await other.send_json({"type": "rpc", "id": "other", "method": "display.unsubscribe",
                               "params": {"subscriptionId": watch["subscriptionId"]}})
        assert (await other.receive_json())["error"]["code"] == "UNAUTHORIZED"
    assert watch["subscriptionId"] in fixture.gateway._display_subscriptions.entries


async def test_subscription_capacity_and_disconnect_cleanup(display_gateway):
    fixture = display_gateway
    watches = [await subscribe(fixture) for _ in range(4)]
    status = watches[0]["status"]
    busy = await fixture.rpc("display.subscribe", {"profileId": "default",
        "expectedHostId": status["hostId"], "expectedBotId": status["botId"]})
    assert busy["error"]["code"] == "SCREEN_BUSY"
    await fixture.socket.close()
    async with asyncio.timeout(3):
        while fixture.gateway._display_subscriptions.entries:
            await asyncio.sleep(0.01)


async def test_unchanged_subscription_has_scoped_heartbeat(display_gateway, monkeypatch):
    monkeypatch.setattr(DisplaySubscriptions, "HEARTBEAT_INTERVAL", 0)
    watch = await subscribe(display_gateway)
    heartbeat = await display_gateway.socket.receive_json(timeout=3)
    assert heartbeat["event"] == "display.heartbeat"
    assert heartbeat["data"] == {"subscriptionId": watch["subscriptionId"], **{
        key: watch["status"][key] for key in ("hostId", "botId", "profileId", "screenId", "generation", "statusRevision")}}


async def test_shutdown_during_pending_snapshot_cannot_resurrect_subscription():
    started, release = Event(), Event()

    def sample():
        started.set()
        assert release.wait(3)
        return {"statusRevision": 1, "hostId": "host", "botId": "bot", "profileId": "default"}

    ws = SimpleNamespace(closed=False)
    gateway = SimpleNamespace(_ws_clients={"client": ws})
    gateway.display_client_authenticated = lambda client, socket: gateway._ws_clients.get(client) is socket and not socket.closed
    subscriptions = DisplaySubscriptions(gateway, lambda *_args, **_kwargs: SimpleNamespace(status=sample))
    pending = asyncio.create_task(subscriptions.prepare(ws, "client", {}))
    try:
        assert await asyncio.to_thread(started.wait, 3)
        await subscriptions.close()
    finally:
        release.set()
    with pytest.raises(ScreenError, match="UNAUTHORIZED"):
        await pending
    assert not subscriptions.entries


async def test_delivery_timeout_does_not_block_other_subscriptions(display_gateway, monkeypatch):
    fixture = display_gateway
    first, second = await subscribe(fixture), await subscribe(fixture, "writer")
    subscriptions = fixture.gateway._display_subscriptions
    original = subscriptions._send
    delayed = AsyncMock(side_effect=asyncio.TimeoutError)

    async def send(entry, event, data):
        if entry.identifier == first["subscriptionId"]:
            return await delayed(entry, event, data)
        return await original(entry, event, data)

    monkeypatch.setattr(subscriptions, "_send", send)
    for profile in ("default", "writer"):
        runtime = _resolve(fixture.gateway, {"profileId": profile}).runtime
        runtime.authority.acquire("human", f"generation-{profile}", 1)
    events = [await fixture.socket.receive_json(timeout=3) for _ in range(2)]
    assert all(event["data"]["subscriptionId"] == second["subscriptionId"] for event in events)
    async with asyncio.timeout(3):
        while first["subscriptionId"] in subscriptions.entries:
            await asyncio.sleep(0.01)
    assert delayed.await_count == 1
    assert first["subscriptionId"] not in subscriptions.entries
    assert _resolve(fixture.gateway, {}).runtime.authority.snapshot()["control"] == "human"


async def test_a_nonreading_consumer_has_a_real_send_deadline():
    async def never_complete(_event):
        await asyncio.Event().wait()

    ws = SimpleNamespace(closed=False, send_json=never_complete)
    gateway = SimpleNamespace(_ws_clients={"client": ws})
    gateway.display_client_authenticated = lambda client, socket: gateway._ws_clients.get(client) is socket and not socket.closed
    subscriptions = DisplaySubscriptions(gateway, lambda *_args, **_kwargs: SimpleNamespace(status=lambda: {
        "statusRevision": 1, "hostId": "host", "botId": "bot", "profileId": "default"}))
    entry = await subscriptions.prepare(ws, "client", {})
    sending = asyncio.create_task(subscriptions._send(entry, "display.status", {}))
    try:
        done, _ = await asyncio.wait({sending}, timeout=4)
        assert sending in done, "subscription delivery must not wait indefinitely"
        with pytest.raises(asyncio.TimeoutError):
            sending.result()
    finally:
        sending.cancel()
        await asyncio.gather(sending, return_exceptions=True)
        await subscriptions.close()
