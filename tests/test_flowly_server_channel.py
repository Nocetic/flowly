"""Core connects to the Flowly server instead of the relay (PROTOCOL.md B).

The frames are the relay's; only the authentication (an agent secret and the
agent host id as headers, no shared JWT) and the power RPCs (B4) are new.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager

import pytest

from flowly.bus.queue import MessageBus
from flowly.channels import web as web_module
from flowly.channels.flowly_server import FlowlyServerChannel
from flowly.channels.web import WebChannel
from flowly.config.schema import ChannelsConfig, Config, FlowlyServerChannelConfig, WebChannelConfig
from flowly.power.monitor import JUST_WOKE_SECONDS, PowerMonitor

URL = "wss://cloud.example.test/v1/agent"


def _config(**overrides) -> FlowlyServerChannelConfig:
    values = {"enabled": True, "server_url": URL, "agent_secret": "s3cret", "agent_host_id": "host-1"}
    values.update(overrides)
    return FlowlyServerChannelConfig(**values)


class FakeSocket:
    def __init__(self, frames: list[dict] | None = None):
        self.sent: list[dict] = []
        self._frames = [json.dumps(frame) for frame in frames or []]

    async def send(self, payload: str) -> None:
        self.sent.append(json.loads(payload))

    async def close(self) -> None:
        return None

    def __aiter__(self):
        return self

    async def __anext__(self):
        await asyncio.sleep(0)
        if not self._frames:
            raise StopAsyncIteration
        return self._frames.pop(0)

    def rpc(self, rpc_id: str) -> dict:
        return next(frame for frame in self.sent if frame.get("type") == "rpc" and frame.get("id") == rpc_id)


class Clock:
    def __init__(self):
        self.now = 2_000_000.0

    def wall(self) -> float:
        return self.now

    def mono(self) -> float:
        return self.now


class Agent:
    def __init__(self):
        self.turns = 0
        self.dream_owed = False
        self.dreamed = 0

    def power_snapshot(self) -> dict:
        return {"turns": self.turns, "dreamOwed": self.dream_owed, "dreamRunning": False,
                "nextDreamAt": None, "lastUserActivityAt": None}

    async def dream_before_sleep(self) -> None:
        self.dreamed += 1
        self.dream_owed = False


class Heartbeat:
    running = False

    def __init__(self):
        self.triggered = 0
        self.next_at = None

    def next_due_at(self):
        return self.next_at

    def set_schedule_listener(self, callback):
        self.listener = callback

    async def trigger(self) -> str:
        self.triggered += 1
        return "ok"


def _channel(*, agent=None, heartbeat=None):
    clock = Clock()
    channel = FlowlyServerChannel(_config(), MessageBus())
    monitor = PowerMonitor(agent=agent or Agent(), heartbeat=heartbeat, flush=lambda: None,
                           wall=clock.wall, monotonic=clock.mono)
    clock.now += JUST_WOKE_SECONDS + 1
    channel.set_power_monitor(monitor)
    socket = FakeSocket()
    channel._ws = socket
    channel._running = True
    return channel, socket, monitor, clock


# ── configuration ──────────────────────────────────────────────────────────


def test_config_reads_camel_case_keys_and_defaults_to_the_cloud_url():
    from flowly.config.loader import convert_keys

    config = Config(**convert_keys({"channels": {"flowlyServer": {
        "enabled": True, "agentSecret": "abc", "agentHostId": "srv1",
    }}}))
    server = config.channels.flowly_server
    assert server.enabled is True
    assert server.agent_secret == "abc"
    assert server.agent_host_id == "srv1"
    assert server.server_url == "wss://cloud.useflowlyapp.com/v1/agent"
    # The relay config is untouched.
    assert config.channels.web.enabled is False


def test_manager_uses_the_server_channel_under_the_web_name(monkeypatch):
    from flowly.channels.manager import ChannelManager

    config = Config(channels=ChannelsConfig(
        web=WebChannelConfig(enabled=True, relay_url="wss://relay.example.test/relay", server_id="x", auth_token="y"),
        flowly_server=_config(),
    ))
    manager = ChannelManager(config, MessageBus())
    assert isinstance(manager.channels["web"], FlowlyServerChannel)


def test_manager_keeps_the_relay_when_the_server_channel_is_off():
    from flowly.channels.manager import ChannelManager

    config = Config(channels=ChannelsConfig(web=WebChannelConfig(enabled=True)))
    manager = ChannelManager(config, MessageBus())
    channel = manager.channels["web"]
    assert isinstance(channel, WebChannel) and not isinstance(channel, FlowlyServerChannel)


def test_env_fallbacks_fill_empty_fields(monkeypatch):
    monkeypatch.setenv("FLOWLY_SERVER_URL", "wss://env.example.test/v1/agent")
    monkeypatch.setenv("FLOWLY_AGENT_SECRET", "env-secret")
    monkeypatch.setenv("FLOWLY_AGENT_HOST_ID", "env-host")
    channel = FlowlyServerChannel(FlowlyServerChannelConfig(enabled=True, server_url=""), MessageBus())
    assert channel.connection_settings() == ("wss://env.example.test/v1/agent", "env-secret", "env-host")
    configured = FlowlyServerChannel(_config(), MessageBus())
    assert configured.connection_settings() == (URL, "s3cret", "host-1")


# ── connection ────────────────────────────────────────────────────────────


async def test_connects_with_agent_secret_headers_and_no_jwt(monkeypatch):
    monkeypatch.setenv("MOLTBOT_PROXY_JWT_SECRET", "shared-relay-secret")
    calls: list[tuple[str, dict]] = []
    socket = FakeSocket([{"type": "ready", "protocol": 1, "cronSessionId": "cron-1", "capabilities": []}])

    @asynccontextmanager
    async def fake_connect(url, **kwargs):
        calls.append((url, kwargs))
        yield socket

    monkeypatch.setattr(web_module.websockets, "connect", fake_connect)
    channel = FlowlyServerChannel(_config(), MessageBus())
    clock = Clock()
    monitor = PowerMonitor(flush=lambda: None, wall=clock.wall, monotonic=clock.mono)
    channel.set_power_monitor(monitor)
    channel._running = True
    await channel._connect_and_run()

    assert len(calls) == 1
    url, kwargs = calls[0]
    assert url == URL
    assert "token=" not in url
    assert kwargs["additional_headers"] == {
        "Authorization": "Bearer s3cret",
        "X-Flowly-Agent-Host": "host-1",
    }
    assert channel.cron_session_id == "cron-1"
    # After ready, Core announces when it is next due.
    assert {"type": "power.next_due", "nextDueAt": None} in socket.sent


async def test_start_refuses_without_a_secret(monkeypatch):
    for name in ("FLOWLY_SERVER_URL", "FLOWLY_AGENT_SECRET", "FLOWLY_AGENT_HOST_ID"):
        monkeypatch.delenv(name, raising=False)
    channel = FlowlyServerChannel(_config(agent_secret=""), MessageBus())

    def must_not_connect():
        raise AssertionError("must not connect")

    channel._connect_and_run = must_not_connect  # type: ignore[method-assign]
    await channel.start()
    assert channel.is_running is False


async def test_the_relay_still_signs_a_jwt(monkeypatch):
    calls: list[str] = []

    @asynccontextmanager
    async def fake_connect(url, **kwargs):
        calls.append(url)
        assert "additional_headers" not in kwargs
        yield FakeSocket()

    monkeypatch.setattr(web_module.websockets, "connect", fake_connect)
    channel = WebChannel(WebChannelConfig(enabled=True, relay_url="ws://localhost:9/relay", server_id="srv",
                                          auth_token="tok", jwt_secret="relay-signing-secret-of-32-bytes!"), MessageBus())
    channel._running = True
    await channel._connect_and_run()
    assert calls and calls[0].startswith("ws://localhost:9/relay?token=")


# ── power RPCs ────────────────────────────────────────────────────────────


async def test_power_status_answers_without_a_session_id():
    channel, socket, monitor, clock = _channel()
    await channel._handle_relay_message(socket, {"type": "rpc", "id": "p1", "method": "power.status", "params": {}})
    assert socket.rpc("p1") == {"type": "rpc", "id": "p1", "result": {
        "idle": True, "busy": [], "nextDueAt": None, "lastActivityAt": None,
    }}


async def test_power_status_reports_a_running_turn():
    agent = Agent()
    agent.turns = 2
    channel, socket, *_ = _channel(agent=agent)
    await channel._handle_relay_message(socket, {"type": "rpc", "id": "p1", "method": "power.status",
                                                 "params": {"idleMinutes": 10}})
    assert socket.rpc("p1")["result"]["busy"] == ["turn"]


async def test_power_sleep_then_new_work_is_refused_until_wake():
    channel, socket, monitor, clock = _channel()
    await channel._handle_relay_message(socket, {"type": "rpc", "id": "p2", "method": "power.sleep",
                                                 "params": {"idleMinutes": 10}})
    assert socket.rpc("p2")["result"] == {"ok": True, "nextDueAt": None}

    await channel._handle_relay_message(socket, {
        "type": "rpc", "id": "c1", "sessionId": "srv-1", "method": "chat.send",
        "params": {"message": "hi", "sessionKey": "web:conv", "idempotencyKey": "m1"},
    })
    reply = socket.rpc("c1")
    assert reply["sessionId"] == "srv-1"
    assert reply["error"]["code"] == "AGENT_SLEEPING"
    assert reply["error"]["retryable"] is True
    # Nothing was accepted, so the server's redelivery starts the turn.
    assert channel.chat_commands.accept("web:conv", "m1", {"message": "hi", "sessionKey": "web:conv"})[0] is True

    await channel._handle_relay_message(socket, {"type": "rpc", "id": "p3", "method": "power.wake", "params": {}})
    assert socket.rpc("p3")["result"] == {"ok": True}
    assert monitor.sleeping is False


async def test_power_sleep_refuses_while_busy():
    agent = Agent()
    agent.turns = 1
    channel, socket, monitor, _ = _channel(agent=agent)
    await channel._handle_relay_message(socket, {"type": "rpc", "id": "p2", "method": "power.sleep",
                                                 "params": {"idleMinutes": 10}})
    assert socket.rpc("p2")["result"] == {"ok": False, "busy": ["turn"]}
    assert monitor.sleeping is False


async def test_power_sleep_dreams_first():
    agent = Agent()
    agent.dream_owed = True
    channel, socket, monitor, _ = _channel(agent=agent)
    await channel._handle_relay_message(socket, {"type": "rpc", "id": "p2", "method": "power.sleep",
                                                 "params": {"idleMinutes": 10}})
    assert socket.rpc("p2")["result"] == {"ok": False, "busy": ["dream"]}
    await monitor.wait_for_dream()
    assert agent.dreamed == 1
    await channel._handle_relay_message(socket, {"type": "rpc", "id": "p4", "method": "power.sleep",
                                                 "params": {"idleMinutes": 10}})
    assert socket.rpc("p4")["result"]["ok"] is True


async def test_power_sleep_rejects_bad_params():
    channel, socket, *_ = _channel()
    await channel._handle_relay_message(socket, {"type": "rpc", "id": "p2", "method": "power.sleep",
                                                 "params": {"idleMinutes": "ten"}})
    assert socket.rpc("p2")["error"]["code"] == "INVALID_REQUEST"


async def test_heartbeat_trigger_runs_and_answers():
    heartbeat = Heartbeat()
    channel, socket, *_ = _channel(heartbeat=heartbeat)
    await channel._handle_relay_message(socket, {"type": "rpc", "id": "p3", "method": "heartbeat.trigger",
                                                 "params": {}})
    await channel.wait_for_power_tasks()
    assert heartbeat.triggered == 1
    assert socket.rpc("p3")["result"] == {"ok": True, "outcome": "ok", "nextDueAt": None}


async def test_client_chat_is_activity_but_polls_are_not():
    channel, socket, monitor, clock = _channel()
    await channel._handle_relay_message(socket, {"type": "rpc", "id": "r1", "sessionId": "c", "method": "chat.inflight",
                                                 "params": {"sessionKey": "web:x"}})
    assert monitor.status()["lastActivityAt"] is None
    await channel._handle_relay_message(socket, {"type": "rpc", "id": "r2", "sessionId": "c", "method": "commands.list",
                                                 "params": {}})
    assert monitor.status()["lastActivityAt"] is None
    await channel._handle_relay_message(socket, {"type": "rpc", "id": "r3", "sessionId": "c", "method": "chat.abort",
                                                 "params": {"runId": "nope"}})
    assert monitor.status()["lastActivityAt"] == int(clock.now * 1000)


async def test_power_rpcs_from_a_client_session_are_refused():
    channel, socket, monitor, _ = _channel()
    await channel._handle_relay_message(socket, {"type": "rpc", "id": "p2", "sessionId": "client-1",
                                                 "method": "power.sleep", "params": {"idleMinutes": 1}})
    assert socket.rpc("p2")["error"]["code"] == "FORBIDDEN"
    assert monitor.sleeping is False


async def test_screen_ticket_is_answered_not_dropped():
    channel, socket, *_ = _channel()
    await channel._handle_relay_message(socket, {"type": "rpc", "id": "s1", "sessionId": "c", "method": "screen.ticket",
                                                 "params": {"profile": "default"}})
    assert socket.rpc("s1")["error"]["code"] == "SCREEN_TICKET_UNAVAILABLE"


async def test_next_due_changes_are_pushed_to_the_server():
    heartbeat = Heartbeat()
    channel, socket, monitor, clock = _channel(heartbeat=heartbeat)
    await monitor.check_next_due(force=True)
    heartbeat.next_at = clock.now + 600
    heartbeat.listener()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    frames = [frame for frame in socket.sent if frame.get("type") == "power.next_due"]
    assert frames[-1] == {"type": "power.next_due", "nextDueAt": int((clock.now + 600) * 1000)}


async def test_reconnect_ends_the_sleeping_state():
    channel, socket, monitor, _ = _channel()
    assert (await monitor.sleep(idle_minutes=1))["ok"] is True
    channel._on_connected(socket)
    assert monitor.sleeping is False


async def test_prompts_keep_their_session_key_for_server_routing():
    """The server routes events to subscribed connections by data.sessionKey."""
    from flowly.clarify.types import ClarifyRequest

    channel, socket, *_ = _channel()
    sent: list[dict] = []

    async def capture(payload: str) -> None:
        sent.append(json.loads(payload))

    channel._send_or_queue = capture  # type: ignore[method-assign]
    channel._session_key_to_relay_id["web:conv"] = "srv-1"
    await channel.send_clarify_event("web:conv", ClarifyRequest(
        id="q1", question="Which one?", choices=None, session_key="web:conv", created_at=0.0, expires_at=9.0,
    ))
    await channel.send_clarify_closed("web:conv", "q1", "answered")
    assert [frame["sessionId"] for frame in sent] == ["srv-1", "srv-1"]
    assert all(frame["data"]["sessionKey"] == "web:conv" for frame in sent)


class _SuspendingClock:
    """time.time / time.monotonic where a suspend advances only the wall clock."""

    def __init__(self):
        self.wall = 1_000_000.0
        self.mono = 50.0
        self.ticks = 0

    def time(self) -> float:
        return self.wall

    def monotonic(self) -> float:
        return self.mono

    async def sleep(self, seconds: float) -> None:
        self.ticks += 1
        if self.ticks == 3:
            self.wall += 1800  # a 30 minute suspend: monotonic stands still
        self.wall += seconds
        self.mono += seconds


def _clocked(monkeypatch, clock: _SuspendingClock) -> None:
    import flowly.channels.flowly_server as server_module

    monkeypatch.setattr(server_module.time, "time", clock.time)
    monkeypatch.setattr(server_module.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(server_module.asyncio, "sleep", clock.sleep)


async def test_backoff_ends_at_once_when_the_machine_resumes(monkeypatch):
    """The suspend froze the backoff; the server is waiting with a message (wake took 11 s)."""
    channel, socket, monitor, _ = _channel()
    assert (await monitor.sleep(idle_minutes=1))["ok"] is True
    clock = _SuspendingClock()
    _clocked(monkeypatch, clock)
    assert await channel._reconnect_wait(10) is True
    assert clock.ticks == 3  # 1.5 s of backoff before the suspend, nothing after it
    assert monitor.sleeping is False


async def test_backoff_without_a_resume_waits_it_out(monkeypatch):
    channel, *_ = _channel()
    clock = _SuspendingClock()
    clock.ticks = -1000  # no suspend during this wait
    _clocked(monkeypatch, clock)
    assert await channel._reconnect_wait(3) is False
    assert clock.mono == pytest.approx(53.0)


async def test_a_resume_resets_the_reconnect_backoff(monkeypatch):
    channel, *_ = _channel()
    delays: list[float] = []
    attempts = 0

    async def fail_then_stop():
        nonlocal attempts
        attempts += 1
        if attempts == 3:
            channel._running = False
        raise ConnectionError("dropped")

    async def wait(delay: float) -> bool:
        delays.append(delay)
        return len(delays) == 1  # the first wait ends in a resume

    monkeypatch.setattr(channel, "_connect_and_run", fail_then_stop)
    monkeypatch.setattr(channel, "_reconnect_wait", wait)
    monkeypatch.setattr(channel, "_ready_to_connect", lambda: True)
    await WebChannel.start(channel)
    assert delays == [5, 5]


async def test_screen_status_is_answered_and_is_not_activity():
    """A client polls screen.status while the Computer tab is open; it must not keep the machine up."""
    channel, socket, monitor, _ = _channel()
    before = monitor._last_activity
    await channel._handle_relay_message(socket, {"type": "rpc", "id": "s2", "sessionId": "c", "method": "screen.status",
                                                 "params": {"profile": "default"}})
    assert socket.rpc("s2")["error"]["code"] == "SCREEN_TICKET_UNAVAILABLE"
    assert monitor._last_activity == before


def test_server_screen_status_reports_the_phase_without_a_ticket(monkeypatch):
    from flowly.gateway import display

    class Runtime:
        def status(self):
            return {"phase": "running", "generation": "g7"}

    class Session:
        runtime = Runtime()

        def observe(self, *_args):  # pragma: no cover - must not be called
            raise AssertionError("status must not issue a ticket")

    monkeypatch.setattr(display, "_resolve", lambda gateway, params: Session())
    assert display.server_screen_status(object(), "default") == {"phase": "running", "generation": "g7"}

    class Stopped(Runtime):
        def status(self):
            return None

    Session.runtime = Stopped()
    assert display.server_screen_status(object(), "default") == {"phase": "stopped", "generation": None}
