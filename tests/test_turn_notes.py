"""The app's notes for a turn (flowly/agent/turn_notes.py): read by the model,
never stored, echoed or shown as the person's words.

2026-10-27: the Flowly server glued the person's reactions in front of their
next message ("[The user reacted ❤️ to your message: …]"). Core stored that
text as what the person wrote, and Rocket Scientist's chat showed it as a
purple bubble of the user's own.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from flowly.agent import turn_notes


def test_notes_are_strings_within_bounds_or_refused_whole():
    assert turn_notes.turn_notes({}) == []
    assert turn_notes.turn_notes({"notes": [" The user reacted 👍. "]}) == ["The user reacted 👍."]
    for bad in ("one", [""], ["   "], [3], ["x" * (turn_notes.MAX_NOTE_CHARS + 1)],
                ["n"] * (turn_notes.MAX_NOTES + 1)):
        with pytest.raises(ValueError):
            turn_notes.turn_notes({"notes": bad})


def test_a_turn_of_notes_only_is_told_apart_from_one_with_words_or_files():
    notes = {"notes": ["The user reacted 🤔."]}
    assert turn_notes.inbound_metadata({}, "hi", False) == {}
    assert turn_notes.inbound_metadata(notes, "hi", False) == {turn_notes.TURN_NOTES: ["The user reacted 🤔."]}
    assert turn_notes.is_notes_only(turn_notes.inbound_metadata(notes, "  ", False))
    assert not turn_notes.is_notes_only(turn_notes.inbound_metadata(notes, "", True))


def test_the_model_reads_notes_as_the_users_own_actions_never_their_words():
    block = turn_notes.notes_block(["The user reacted ❤️ to your message: \"Done.\""])
    assert block.startswith("<app_notes>") and block.endswith("</app_notes>")
    # Their own actions (a "not from the user" made Tsuki disown the reaction, 2026-10-28), not typed, unseen.
    assert "the user's own actions" in block and "not words they typed" in block
    assert "not from the user" not in block and "Never quote or repeat" in block
    assert "- The user reacted ❤️" in block


def _agent(tmp_path, monkeypatch):
    from flowly.agent.loop import AgentLoop
    from flowly.bus.queue import MessageBus
    from flowly.config.schema import Config
    from flowly.providers.base import LLMProvider, LLMResponse

    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path / "home"))

    class Provider(LLMProvider):
        def __init__(self):
            super().__init__(api_key="test")
            self.calls: list[list[dict]] = []

        def get_default_model(self):
            return "test/model"

        async def chat(self, **kwargs):
            if kwargs.get("messages"):
                self.calls.append(kwargs["messages"].copy())
            return LLMResponse(content="Sure.", finish_reason="stop")

        async def chat_stream(self, **kwargs):
            self.calls.append(kwargs["messages"].copy())
            yield LLMResponse(content="Sure.", finish_reason="stop")

    provider = Provider()
    agent = AgentLoop(bus=MessageBus(), provider=provider, workspace=tmp_path, main_config=Config(),
                      max_iterations=2, soft_warn_at_iteration=0)
    return agent, provider


def _text(message: dict) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    return "".join(part.get("text", "") for part in content or [] if isinstance(part, dict))


async def _turn(agent, content: str, session_key: str, metadata: dict):
    return await agent.process_direct(
        content, session_key=session_key, run_id="r1", skip_memory=True, skip_context_files=True,
        tools_allowed=False, extra_metadata=metadata,
    )


@pytest.mark.asyncio
async def test_notes_ride_on_the_models_message_and_the_stored_row_is_the_persons_words(tmp_path, monkeypatch):
    agent, provider = _agent(tmp_path, monkeypatch)
    note = 'The user reacted ❤️ to your message: "Starship flies in November."'
    await _turn(agent, "What about the orbit?", "web:notes",
                turn_notes.inbound_metadata({"notes": [note]}, "What about the orbit?", False))
    sent = _text(provider.calls[0][-1])
    assert note in sent and "<app_notes>" in sent and sent.rstrip().endswith("What about the orbit?")
    users = [m for m in agent.sessions.get_or_create("web:notes").messages if m.get("role") == "user"]
    assert [m["content"] for m in users] == ["What about the orbit?"]
    assert not users[0].get("_display_hidden")


@pytest.mark.asyncio
async def test_a_turn_of_notes_only_is_the_apps_prompt_kept_hidden(tmp_path, monkeypatch):
    agent, provider = _agent(tmp_path, monkeypatch)
    note = 'The user reacted 🤔 to your message: "NET 8 November."'
    await _turn(agent, "", "web:wake", turn_notes.inbound_metadata({"notes": [note]}, "", False))
    assert note in _text(provider.calls[0][-1])
    session = agent.sessions.get_or_create("web:wake")
    users = [m for m in session.messages if m.get("role") == "user"]
    # Kept for the agent's later turns, never shown as something the person said.
    assert len(users) == 1 and users[0].get("_display_hidden") is True and note in users[0]["content"]
    shown = [m for m in agent.sessions.display_messages("web:wake") if m.get("role") == "user"] \
        if hasattr(agent.sessions, "display_messages") else []
    assert all(note not in _text(m) for m in shown)
    # Nothing a person typed: no title from it.
    assert not (session.metadata.get("title") or "").startswith("<app_notes>")


class _Ws:
    def __init__(self):
        self.sent: list[dict] = []

    async def send(self, payload: str) -> None:
        self.sent.append(json.loads(payload))


def _channel(monkeypatch, tmp_path):
    from flowly.bus.queue import MessageBus
    from flowly.channels.web import WebChannel
    from flowly.config.schema import WebChannelConfig

    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path / "home"))
    channel = WebChannel(config=WebChannelConfig(enabled=True), bus=MessageBus())
    seen: list[dict] = []

    async def process(session_id, session_key, content, run_id, stream_callback=None, media=None,
                      voice_mode=False, render_capabilities=(), extra_metadata=None):
        seen.append({"content": content, "metadata": extra_metadata or {}})

    channel._process_message = process  # type: ignore[method-assign]
    return channel, seen


async def _send(channel, params: dict) -> _Ws:
    ws = _Ws()
    await channel._dispatch_rpc(ws, {"type": "rpc", "id": "1", "sessionId": "s1", "method": "chat.send", "params": params})
    await asyncio.sleep(0)
    return ws


@pytest.mark.asyncio
async def test_the_channel_passes_notes_apart_and_restores_no_words_for_a_turn_of_notes(tmp_path, monkeypatch):
    from flowly.agent import inflight

    channel, seen = _channel(monkeypatch, tmp_path)
    channel.supports_turn_start = False
    ws = await _send(channel, {"sessionKey": "web:c1", "idempotencyKey": "reaction-1", "message": "",
                               "notes": ["The user reacted 🤔 to your message."]})
    assert ws.sent[-1].get("result", {}).get("runId") == "reaction-1"
    assert seen[-1]["content"] == ""
    assert seen[-1]["metadata"][turn_notes.NOTES_ONLY_TURN] is True
    # A re-entering app restores the run: no words of the person's to show.
    assert (inflight.get("web:c1") or {}).get("user", "") == ""
    inflight.finish("web:c1", "reaction-1")

    ws = await _send(channel, {"sessionKey": "web:c2", "idempotencyKey": "m2", "message": "Hi",
                               "notes": ["The user reacted 👍 to your message."]})
    assert seen[-1]["content"] == "Hi"
    assert seen[-1]["metadata"][turn_notes.TURN_NOTES] == ["The user reacted 👍 to your message."]
    assert turn_notes.NOTES_ONLY_TURN not in seen[-1]["metadata"]
    inflight.finish("web:c2", "m2")


@pytest.mark.asyncio
async def test_malformed_notes_refuse_the_message_and_a_message_with_notes_is_never_steered(tmp_path, monkeypatch):
    from flowly.channels import feature_rpc

    channel, seen = _channel(monkeypatch, tmp_path)
    ws = await _send(channel, {"sessionKey": "web:c3", "idempotencyKey": "m3", "message": "Hi", "notes": "nope"})
    assert ws.sent[-1]["error"]["code"] == "INVALID_REQUEST" and not seen

    def never(*_args, **_kwargs):
        raise AssertionError("a message with notes must keep its own turn")

    monkeypatch.setattr(feature_rpc, "steer_running_turn", never)
    await _send(channel, {"sessionKey": "web:c4", "idempotencyKey": "m4", "message": "Hi", "notes": ["n"]})
    assert seen[-1]["content"] == "Hi"


def test_core_says_it_takes_notes():
    from flowly.channels.feature_rpc import system_capabilities

    assert system_capabilities()["chatNotesVersion"] == turn_notes.CHAT_NOTES_VERSION


# A named agent's turn (the one in Rocket Scientist's chat) takes another road:
# the profile host checks the request, and the agent's own runtime answers
# chat.send through the gateway, not the web channel.


def test_the_profile_host_lets_a_turn_of_notes_through_and_refuses_malformed_notes():
    from flowly.profile_host_contract import ProfileHostError, validate_profile_rpc

    _method, safe = validate_profile_rpc("chat.send", {
        "sessionKey": "web:c1", "idempotencyKey": "reaction-1", "message": "",
        "notes": ["The user reacted 🤔 to your message."],
    })
    assert safe["notes"] == ["The user reacted 🤔 to your message."]
    for bad in ({"message": "", "notes": []}, {"message": "", "notes": "x"}, {"message": "hi", "notes": [3]}):
        with pytest.raises(ProfileHostError):
            validate_profile_rpc("chat.send", {"sessionKey": "web:c1", "idempotencyKey": "m1", **bad})


class _GatewayWs:
    def __init__(self) -> None:
        self.closed = False
        self.sent: list[dict] = []

    async def send_json(self, data: dict) -> None:
        self.sent.append(data)


async def _gateway_send(params: dict):
    from flowly.agent import inflight
    from flowly.gateway.server import GatewayServer

    seen: list[dict] = []
    users: list[str] = []
    done = asyncio.Event()

    async def on_chat_message(session_key, message, run_id, stream_callback, media, voice_mode,
                              iteration_callback, render_capabilities, extra_metadata=None):
        seen.append({"content": message, "metadata": extra_metadata or {}})
        users.append((inflight.get(session_key) or {}).get("user", ""))
        done.set()
        return "Sure.", {}

    server = GatewayServer(host="127.0.0.1", port=0, on_chat_message=on_chat_message)
    ws = _GatewayWs()
    await server._ws_rpc_chat_send(ws, "server", "rpc-1", params)  # type: ignore[arg-type]
    tasks = list(server._active_tasks.values())
    if tasks:
        await asyncio.wait_for(done.wait(), timeout=2)
        await asyncio.gather(*tasks)
    return ws, seen, users


@pytest.mark.asyncio
async def test_a_named_agents_runtime_takes_notes_apart_and_restores_no_words_for_a_turn_of_notes(tmp_path, monkeypatch):
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path / "home"))
    ws, seen, users = await _gateway_send({"sessionKey": "web:n1", "idempotencyKey": "reaction-2", "message": "",
                                           "notes": ["The user reacted 🤔 to your message."]})
    assert ws.sent[0].get("result", {}).get("runId") == "reaction-2", ws.sent
    assert seen[-1]["content"] == ""
    assert seen[-1]["metadata"][turn_notes.NOTES_ONLY_TURN] is True
    assert users == [""], "a re-entering app restores no words of the person's"

    ws, seen, users = await _gateway_send({"sessionKey": "web:n2", "idempotencyKey": "m5", "message": "Hi",
                                           "notes": ["The user reacted 👍 to your message."]})
    assert seen[-1]["content"] == "Hi"
    assert seen[-1]["metadata"][turn_notes.TURN_NOTES] == ["The user reacted 👍 to your message."]
    assert users == ["Hi"]


@pytest.mark.asyncio
async def test_a_named_agents_runtime_refuses_malformed_notes_and_never_steers_a_message_with_notes(tmp_path, monkeypatch):
    from flowly.channels import feature_rpc

    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path / "home"))
    ws, seen, _users = await _gateway_send({"sessionKey": "web:n3", "idempotencyKey": "m6", "message": "Hi", "notes": "nope"})
    assert ws.sent[-1]["error"]["code"] == "INVALID_REQUEST" and not seen

    def never(*_args, **_kwargs):
        raise AssertionError("a message with notes must keep its own turn")

    monkeypatch.setattr(feature_rpc, "steer_running_turn", never)
    _ws, seen, _users = await _gateway_send({"sessionKey": "web:n4", "idempotencyKey": "m7", "message": "Hi", "notes": ["n"]})
    assert seen[-1]["content"] == "Hi"


# A person's message in a named agent's one chat that did not come through the
# server (Desktop on the agent's own machine): the server, which keeps that
# chat's log, saw only the agent's answer (2026-10-28). Core tells it the message.


class _Host:
    def __init__(self):
        self.calls = []

    def retain_default_events(self, owner):
        pass

    def release_default_events(self, owner):
        pass

    async def dispatch(self, method, params):
        self.calls.append((method, params))
        return {"runId": params["params"].get("idempotencyKey") or "run", "status": "accepted"}


async def _desktop_send(report, inner, profile="tsuki"):
    from unittest.mock import AsyncMock

    from aiohttp import web
    from flowly.gateway.server import GatewayServer

    server = GatewayServer(advertise_control=False)
    server._profile_host = _Host()
    server._ws_rpc_reply = AsyncMock()
    server._ws_rpc_error = AsyncMock()
    server.on_profile_home_message = report
    await server._handle_profile_host_rpc(web.WebSocketResponse(), "desktop", "1", "profiles.rpc",
                                          {"name": profile, "method": "chat.send", "params": inner})
    return server


@pytest.mark.asyncio
async def test_a_message_in_a_named_agents_home_sent_here_is_told_to_the_server():
    from unittest.mock import AsyncMock

    report = AsyncMock()
    server = await _desktop_send(report, {"sessionKey": "desktop:profile-home", "message": "Hi again pal",
                                          "idempotencyKey": "d1", "attachments": [{"x": 1}]})
    report.assert_awaited_once_with("tsuki", "d1", "Hi again pal", 1)
    server._ws_rpc_reply.assert_awaited_once()

    for inner, profile in (({"sessionKey": "ios:earlier", "message": "x", "idempotencyKey": "d2"}, "tsuki"),
                           ({"sessionKey": "desktop:profile-home", "message": "x", "idempotencyKey": "d3"}, "default"),
                           ({"sessionKey": "desktop:profile-home", "message": "  ", "idempotencyKey": "d4"}, "tsuki")):
        quiet = AsyncMock()
        await _desktop_send(quiet, inner, profile)
        quiet.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_failed_report_never_fails_the_message():
    from unittest.mock import AsyncMock

    report = AsyncMock(side_effect=RuntimeError("socket gone"))
    server = await _desktop_send(report, {"sessionKey": "desktop:profile-home", "message": "hi", "idempotencyKey": "d5"})
    server._ws_rpc_reply.assert_awaited_once()
    server._ws_rpc_error.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_server_channel_tells_it_on_the_agents_delivery_session():
    from unittest.mock import AsyncMock

    from flowly.channels.flowly_server import FlowlyServerChannel

    channel = object.__new__(FlowlyServerChannel)
    channel._profile_sessions = lambda: {"tsuki": "srv-p:tsuki:c1"}
    channel._send_or_queue = AsyncMock()
    await channel.report_profile_home_message("tsuki", "d1", "Hi again pal", 0)
    sent = json.loads(channel._send_or_queue.await_args.args[0])
    assert sent == {"type": "event", "sessionId": "srv-p:tsuki:c1", "event": "chat", "profile": "tsuki",
                    "data": {"state": "user", "sessionKey": "desktop:profile-home", "messageId": "d1",
                             "message": {"role": "user", "text": "Hi again pal", "files": 0}}}
    # An agent the server never delivered to has no session yet: nothing to tell.
    channel._send_or_queue.reset_mock()
    await channel.report_profile_home_message("rocket", "d2", "x", 0)
    channel._send_or_queue.assert_not_awaited()
