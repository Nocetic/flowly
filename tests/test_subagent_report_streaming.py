"""A subagent's report turn streams to the client like the owner's turn.

The defect: when subagents finished, the turn the main agent took to report
their results came in over the bus with no run, no stream and no tool events.
The owner saw the whole reply appear at once (and on the direct gateway, a
desktop dropped it until it reloaded). Now a surface's own chat runner runs
the report turn — the direct gateway's ``_run_chat`` or the relay channel's
``chat.send`` machinery — so it has a run id, a command record, an in-flight
entry with a hidden prompt, live deltas, live tool steps, one final and Stop,
and the bus stays the fallback when nobody is watching.

Each test pins one part of that contract, plus the delivery rules that must
not move: exactly-once delivery (including Stop), ``notice_already_handled``,
the turn lock's ordering, and agenda turns staying as they were.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock

import pytest

from flowly.agent import inflight
from flowly.agent.loop import AgentLoop
from flowly.agent.report_turn import REPORT_TURN, REPORT_TURN_ORIGIN
from flowly.agent.subagent import SUBAGENT_REPORTS_KEY
from flowly.bus.events import ConversationInboundMessage
from flowly.bus.queue import MessageBus
from flowly.channels.web import WebChannel
from flowly.config.schema import Config, WebChannelConfig
from flowly.gateway.server import GatewayServer
from flowly.providers.base import LLMProvider, LLMResponse, ToolCallRequest

CHAT = "desktop:chat-1"
CONNECTION = "conn-1"


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path / "home"))


class _FakeWS:
    def __init__(self) -> None:
        self.closed = False
        self.sent: list[dict[str, Any]] = []

    async def send_json(self, payload: dict[str, Any]) -> None:
        self.sent.append(dict(payload))

    async def send_str(self, payload: str) -> None:
        self.sent.append(json.loads(payload))


class _Provider(LLMProvider):
    """Children answer at once; the main agent's report uses one tool, then speaks.

    ``hold`` parks the report's final answer until set (Stop tests);
    ``seen_inflight`` records what a re-entering client would get mid-turn.
    """

    def __init__(self, session_key: str = CHAT) -> None:
        super().__init__(api_key="unused")
        self.session_key = session_key
        self.main_calls = 0
        self.hold: asyncio.Event | None = None
        self.seen_inflight: list[dict | None] = []
        self.reports: list[str] = []

    def get_default_model(self) -> str:
        return "m"

    async def chat(self, **kwargs: Any) -> LLMResponse:
        messages = kwargs.get("messages") or []
        system = str(messages[0].get("content", "")) if messages else ""
        if "The main agent started you" in system:
            return LLMResponse(content="Anthropic was founded in 2021.")
        self.main_calls += 1
        last = str(messages[-1].get("content", "")) if messages else ""
        if "[Subagent" in last or "subagent" in last.lower():
            self.reports.append(last)
        self.seen_inflight.append(inflight.get(self.session_key))
        if not any(m.get("role") == "tool" for m in messages[-2:]):
            return LLMResponse(content="Checking. ", tool_calls=[ToolCallRequest(
                id=f"t{self.main_calls}", name="list_dir", arguments={"path": "."},
            )])
        if self.hold is not None:
            await self.hold.wait()
        return LLMResponse(content="It was 2021.")


def _agent(tmp_path, provider: LLMProvider, bus: MessageBus | None = None) -> AgentLoop:
    return AgentLoop(bus=bus or MessageBus(), provider=provider, workspace=tmp_path,
                     main_config=Config(), max_iterations=6, soft_warn_at_iteration=0)


def _gateway(agent: AgentLoop) -> GatewayServer:
    async def on_chat_message(session_key, message, run_id, stream_callback=None, media=None,
                              voice_mode=False, iteration_callback=None, render_capabilities=(),
                              extra_metadata=None):
        text, metadata = await agent.process_direct(
            content=message, session_key=session_key, stream_callback=stream_callback,
            media=media, voice_mode=voice_mode, render_capabilities=render_capabilities,
            return_metadata=True, on_iteration=iteration_callback,
            on_tool_progress=iteration_callback, run_id=run_id, defer_goal_delivery=True,
            extra_metadata=extra_metadata,
        )
        return text, (metadata or {})

    on_chat_message.supports_turn_start = True
    server = GatewayServer(host="127.0.0.1", port=0, on_chat_message=on_chat_message,
                           on_chat_abort=agent.mark_aborted)
    agent.set_gateway_server(server)
    agent.register_report_turn_submitter("direct", server.run_report_turn)
    return server


async def _spawn(agent: AgentLoop, name: str = "Anthropic", batch: str = "") -> str:
    started = json.loads(await agent.subagents.spawn(
        task=f"Find the year {name} was founded", origin_channel="desktop",
        origin_chat_id=CONNECTION, parent_session_key=CHAT, origin="agent",
        display_name=name, batch_id=batch,
    ))
    return started["run_id"]


async def _until(predicate, timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("timed out")
        await asyncio.sleep(0.01)


def _chat(ws: _FakeWS) -> list[dict[str, Any]]:
    return [e["data"] for e in ws.sent if e.get("event") == "chat"]


def _finals(ws: _FakeWS) -> list[dict[str, Any]]:
    return [d for d in _chat(ws) if d.get("state") in ("final", "error")]


# ── the direct gateway ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_report_turn_streams_on_the_direct_gateway_like_an_owner_turn(tmp_path):
    provider = _Provider()
    agent = _agent(tmp_path, provider)
    server = _gateway(agent)
    ws = _FakeWS()
    server._session_ws[CHAT] = ws
    server.push_session_message = AsyncMock()  # type: ignore[method-assign]
    try:
        child = await _spawn(agent)
        await _until(lambda: _finals(ws))

        chat = _chat(ws)
        started = chat[0]
        assert started["state"] == "run_started"
        run_id = started["runId"]
        assert started == {
            "state": "run_started", "runId": run_id, "sessionKey": CHAT,
            "turnOrigin": REPORT_TURN_ORIGIN, "hiddenPrompt": True, "steerable": False,
        }
        # Live text, under the same run, marked as the server's.
        deltas = [e["data"] for e in ws.sent if e.get("event") == "agent"]
        assert deltas and {d["runId"] for d in deltas} == {run_id}
        assert all(d["turnOrigin"] == REPORT_TURN_ORIGIN and d["sessionKey"] == CHAT for d in deltas)
        assert "".join(d["data"]["text"] for d in deltas).endswith("It was 2021.")
        # Live tool steps.
        steps = [d for d in chat if d.get("state") == "iteration_step"]
        assert steps and {d["runId"] for d in steps} == {run_id}
        assert any(d.get("tool_calls") for d in steps) and any(d.get("role") == "tool" for d in steps)
        assert all(d["turnOrigin"] == REPORT_TURN_ORIGIN and d["sessionKey"] == CHAT for d in steps)
        # One terminal, same run, nothing else on the wire for it.
        finals = _finals(ws)
        assert len(finals) == 1 and finals[0]["state"] == "final"
        assert finals[0]["runId"] == run_id and finals[0]["turnOrigin"] == REPORT_TURN_ORIGIN
        assert finals[0]["message"]["content"][0]["text"] == "It was 2021."
        assert "aborted" not in finals[0]
        # No user row, no proactive copy of the reply.
        assert not any(d.get("state") == "user" for d in chat)
        server.push_session_message.assert_not_called()

        # The command record says what ran, and it finished.
        assert server.chat_commands.lookup(CHAT, run_id)["status"] == "completed"
        # In flight, a re-entering client got the run without the notice.
        live = [entry for entry in provider.seen_inflight if entry]
        assert live and all(e["runId"] == run_id and e["user"] == "" for e in live)
        assert all(e["turnOrigin"] == REPORT_TURN_ORIGIN for e in live)
        assert inflight.get(CHAT) is None

        # Delivered once, in the same save as the turn; the notice is hidden.
        assert agent.subagents.registry.get(child).delivery_state == "delivered"
        session = agent.sessions.get_or_create(CHAT)
        assert child in session.metadata[SUBAGENT_REPORTS_KEY]
        notice_rows = [m for m in session.messages if "[System: subagent]" in str(m.get("content"))]
        assert notice_rows
        replies = [m for m in session.messages if m.get("role") == "assistant" and m.get("run_id") == run_id]
        assert replies and replies[-1]["content"] == "It was 2021."
    finally:
        agent.stop()


@pytest.mark.asyncio
async def test_without_a_watching_client_the_report_goes_over_the_bus_as_before(tmp_path):
    provider = _Provider()
    bus = MessageBus()
    agent = _agent(tmp_path, provider, bus)
    server = _gateway(agent)  # registered, but no socket watches CHAT
    try:
        assert await server.run_report_turn(CHAT, {"content": "x"}) is False
        child = await _spawn(agent)
        notice = await asyncio.wait_for(bus.consume_inbound(), 5)
        # Exactly the notice the bus always carried: no run, no callbacks.
        assert isinstance(notice, ConversationInboundMessage)
        assert (notice.channel, notice.sender_id, notice.session_key) == ("system", "subagent", CHAT)
        assert notice.chat_id == f"desktop:{CONNECTION}"
        assert notice.metadata == {}
        assert server.chat_commands.lookup(CHAT, "x") is None

        server.push_session_message = AsyncMock()  # type: ignore[method-assign]
        pushes: list[tuple] = []
        server._schedule_offline_chat_push = lambda *a, **k: pushes.append((a, k))  # type: ignore[method-assign]
        out = await agent._process_message(notice)
        assert out is not None and out.content == "It was 2021."
        assert agent.subagents.registry.get(child).delivery_state == "delivered"
        # Local clients still get the reply pushed, now under the
        # conversation's own key and with the run id the saved row carries,
        # so a client that showed it does not show the saved copy again.
        server.push_session_message.assert_awaited_once()
        key, text = server.push_session_message.await_args.args
        assert (key, text) == (CHAT, "It was 2021.")
        pushed_run = server.push_session_message.await_args.kwargs["metadata"]["stream_run_id"]
        session = agent.sessions.get_or_create(CHAT)
        assert session.messages[-1]["role"] == "assistant"
        assert session.messages[-1]["run_id"] == pushed_run
        # A phone registered for pushes hears about it (nobody was watching).
        assert pushes and pushes[0][0][:2] == (CHAT, "It was 2021.")
    finally:
        agent.stop()


@pytest.mark.asyncio
async def test_stopping_a_report_turn_ends_it_and_its_results_stay_delivered(tmp_path):
    """Rule: a stopped report is saved like any stopped turn — what streamed,
    the hidden notice, ``aborted`` — so its results are delivered and never
    announced again. The owner stopped the report; repeating it would ignore
    that. The results stay in the conversation for the agent's next turn."""
    provider = _Provider()
    provider.hold = asyncio.Event()  # the report's answer never comes on its own
    bus = MessageBus()
    agent = _agent(tmp_path, provider, bus)
    server = _gateway(agent)
    ws = _FakeWS()
    server._session_ws[CHAT] = ws
    try:
        child = await _spawn(agent)
        await _until(lambda: any(d.get("state") == "iteration_step" and d.get("role") == "tool"
                                 for d in _chat(ws)))
        run_id = _chat(ws)[0]["runId"]
        await _until(lambda: provider.main_calls >= 2)

        # Stop, through the same RPC a client uses.
        await server._ws_rpc_chat_abort(ws, "rpc-1", {"runId": run_id, "sessionKey": CHAT})
        await _until(lambda: _finals(ws))
        final = _finals(ws)[0]
        assert final["state"] == "final" and final["runId"] == run_id and final["aborted"] is True
        assert server.chat_commands.lookup(CHAT, run_id)["status"] == "aborted"

        assert agent.subagents.registry.get(child).delivery_state == "delivered"
        session = agent.sessions.get_or_create(CHAT)
        assert child in session.metadata[SUBAGENT_REPORTS_KEY]
        assert any(m.get("run_id") == run_id and m.get("aborted") for m in session.messages)
        # Never announced a second time.
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(bus.consume_inbound(), 0.5)
        assert len([d for d in _chat(ws) if d.get("state") == "run_started"]) == 1
    finally:
        provider.hold.set()
        agent.stop()


@pytest.mark.asyncio
async def test_a_notice_already_taken_by_agent_wait_never_opens_a_run(tmp_path):
    provider = _Provider()
    agent = _agent(tmp_path, provider)
    server = _gateway(agent)
    ws = _FakeWS()
    server._session_ws[CHAT] = ws
    agent.subagents.report_submitter = None  # deliver by hand below
    try:
        child = await _spawn(agent)
        await _until(lambda: agent.subagents.registry.get(child).delivery_state == "queued")
        agent.subagents.mark_delivered(CHAT, [child])
        content = f"[Subagent 'Anthropic' finished (run_id={child})]\n\nIt was 2021."
        assert agent.subagents.notice_already_handled(content)
        calls = provider.main_calls

        assert await server.run_report_turn(CHAT, {"content": content, "chat_id": f"desktop:{CONNECTION}"})
        await _until(lambda: not server._active_tasks)
        # Nothing announced, nothing said, nothing run; the record is closed.
        assert ws.sent == []
        assert provider.main_calls == calls
        records = server.chat_commands._db.execute(
            "SELECT status FROM chat_commands WHERE session_key = ?", (CHAT,)).fetchall()
        assert [r["status"] for r in records] == ["aborted"]
    finally:
        agent.stop()


@pytest.mark.asyncio
async def test_report_turns_of_one_conversation_keep_their_order(tmp_path):
    """Two notices submitted back to back run one after the other, in order:
    the second announces itself only after the first has ended."""
    provider = _Provider()
    agent = _agent(tmp_path, provider)
    server = _gateway(agent)
    ws = _FakeWS()
    server._session_ws[CHAT] = ws
    try:
        await agent._submit_report_turn(CHAT, "desktop", CONNECTION, "[System note] first")
        await agent._submit_report_turn(CHAT, "desktop", CONNECTION, "[System note] second")
        await _until(lambda: len(_finals(ws)) == 2)
        lifecycle = [(d["state"], d["runId"]) for d in _chat(ws)
                     if d.get("state") in ("run_started", "final")]
        first, second = lifecycle[0][1], lifecycle[2][1]
        assert lifecycle == [("run_started", first), ("final", first),
                             ("run_started", second), ("final", second)]
        hidden = [m["content"] for m in agent.sessions.get_or_create(CHAT).messages
                  if str(m.get("content", "")).startswith("[System: subagent]")]
        assert [h.split("] ", 2)[-1][:5] for h in hidden] == ["first", "secon"]
    finally:
        agent.stop()


@pytest.mark.asyncio
async def test_the_surface_that_serves_the_conversation_is_tried_first(tmp_path):
    agent = _agent(tmp_path, _Provider())
    calls: list[str] = []

    def submitter(name: str, accept: bool):
        async def submit(session_key: str, notice: dict) -> bool:
            calls.append(name)
            assert notice["sender_id"] == "subagent"
            return accept
        return submit

    try:
        assert await agent._submit_report_turn(CHAT, "desktop", "c", "n") is False  # none registered
        agent.register_report_turn_submitter("direct", submitter("direct", False))
        agent.register_report_turn_submitter("web", submitter("web", True))
        assert await agent._submit_report_turn("web:chat", "web", "relay", "n") is True
        assert calls == ["web"]
        calls.clear()
        assert await agent._submit_report_turn(CHAT, "desktop", "c", "n") is True
        assert calls == ["direct", "web"]
    finally:
        agent.stop()


# ── the relay ───────────────────────────────────────────────────────────────


def _relay(bus: MessageBus, server: GatewayServer) -> tuple[WebChannel, list[dict[str, Any]]]:
    channel = WebChannel(config=WebChannelConfig(enabled=True), bus=bus)
    payloads: list[dict[str, Any]] = []

    async def capture(payload: str) -> None:
        payloads.append(json.loads(payload))

    async def capture_local(_name: str, _data: dict[str, Any]) -> None:
        return None

    channel._send_or_queue = capture  # type: ignore[method-assign]
    channel._emit_local_event = capture_local  # type: ignore[method-assign]
    channel._session_key_to_relay_id["web:chat"] = "relay-2"
    channel.supports_turn_start = True
    channel.set_chat_commands(server.chat_commands)
    return channel, payloads


@pytest.mark.asyncio
async def test_a_report_turn_streams_on_the_relay_and_closes_by_its_stream_run(tmp_path):
    provider = _Provider(session_key="web:chat")
    bus = MessageBus()
    agent = _agent(tmp_path, provider, bus)
    server = _gateway(agent)
    channel, payloads = _relay(bus, server)
    agent.register_report_turn_submitter("web", channel.run_report_turn)
    try:
        notice_text = "[System note] the research is done"
        assert await agent._submit_report_turn("web:chat", "web", "relay-1", notice_text)
        msg = await asyncio.wait_for(bus.consume_inbound(), 5)
        # The agent queue gets the same system notice the bus always did,
        # addressed to the relay session the conversation has now.
        assert isinstance(msg, ConversationInboundMessage)
        assert (msg.channel, msg.sender_id, msg.session_key) == ("system", "subagent", "web:chat")
        assert msg.chat_id == "web:relay-2" and msg.content == notice_text
        assert msg.metadata[REPORT_TURN]["chat_id"] == "web:relay-2"
        run_id = msg.metadata["run_id"]

        await agent._process_scoped_turn(msg)
        out = await asyncio.wait_for(bus.consume_outbound(), 5)
        await channel.send(out)

        events = [p["data"] for p in payloads if p.get("event") == "chat"]
        assert events[0] == {
            "state": "run_started", "runId": run_id, "sessionKey": "web:chat",
            "turnOrigin": REPORT_TURN_ORIGIN, "hiddenPrompt": True, "steerable": False,
        }
        streaming = [e for e in events if e["state"] == "streaming"]
        assert streaming and all(e["runId"] == run_id and e["turnOrigin"] == REPORT_TURN_ORIGIN
                                 for e in streaming)
        final = events[-1]
        assert final["state"] == "final" and final["streamRunId"] == run_id
        assert final["sessionKey"] == "web:chat" and final["turnOrigin"] == REPORT_TURN_ORIGIN
        assert final["message"]["content"][0]["text"] == "It was 2021."
        assert final.get("toolMessages")  # the relay persists the tool steps
        assert not any(e["state"] == "user" for e in events)
        assert server.chat_commands.lookup("web:chat", run_id)["status"] == "completed"
        live = [entry for entry in provider.seen_inflight if entry]
        assert live and all(e["user"] == "" and e["turnOrigin"] == REPORT_TURN_ORIGIN for e in live)
        assert inflight.get("web:chat") is None
    finally:
        agent.stop()


@pytest.mark.asyncio
async def test_a_failed_report_turn_still_ends_its_relay_run(tmp_path):
    provider = _Provider(session_key="web:chat")
    bus = MessageBus()
    agent = _agent(tmp_path, provider, bus)
    server = _gateway(agent)
    channel, payloads = _relay(bus, server)
    agent.register_report_turn_submitter("web", channel.run_report_turn)
    try:
        assert await agent._submit_report_turn("web:chat", "web", "relay-1", "[System note] x")
        msg = await asyncio.wait_for(bus.consume_inbound(), 5)
        agent._process_system_message = AsyncMock(side_effect=RuntimeError("boom"))  # type: ignore[method-assign]
        await agent._process_scoped_turn(msg)
        out = await asyncio.wait_for(bus.consume_outbound(), 5)
        assert (out.channel, out.chat_id) == ("web", "relay-2")
        await channel.send(out)
        final = [p["data"] for p in payloads if p.get("event") == "chat"][-1]
        assert final["state"] == "final" and final["failed"] is True
        assert final["streamRunId"] == msg.metadata["run_id"]
        assert server.chat_commands.lookup("web:chat", msg.metadata["run_id"])["status"] == "error"
        assert inflight.get("web:chat") is None
    finally:
        agent.stop()


# ── what stays as it was ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_report_turns_get_the_compaction_checks_an_owner_turn_gets(tmp_path, monkeypatch):
    agent = _agent(tmp_path, _Provider())
    passes: list[tuple[str, str, bool]] = []

    async def fake_pass(msg, *, settle=True, should_cancel=None):
        passes.append((msg.session_key, msg.channel, settle))

    monkeypatch.setattr(agent, "_post_turn_compaction", fake_pass)
    try:
        await agent._process_message(ConversationInboundMessage(
            channel="system", sender_id="subagent", chat_id=f"desktop:{CONNECTION}",
            content="[System note] done", conversation_key=CHAT,
        ))
        await asyncio.sleep(0.05)
        # Before the turn, awaited (no settle); after it, in the background.
        assert passes == [(CHAT, "desktop", False), (CHAT, "desktop", True)]
    finally:
        agent.stop()


@pytest.mark.asyncio
async def test_an_agenda_wake_up_still_runs_silent_and_unstreamed(tmp_path, monkeypatch):
    agent = _agent(tmp_path, _Provider())
    passes: list[Any] = []

    async def fake_pass(msg, **_kw):
        passes.append(msg)

    monkeypatch.setattr(agent, "_post_turn_compaction", fake_pass)
    seen: dict[str, Any] = {}
    real = agent._run_llm_tool_loop

    async def spy(**kwargs):
        seen.update(kwargs)
        return await real(**kwargs)

    monkeypatch.setattr(agent, "_run_llm_tool_loop", spy)
    try:
        await agent._process_system_message(ConversationInboundMessage(
            channel="system", sender_id="agenda", chat_id=f"desktop:{CONNECTION}",
            content="wake", conversation_key=CHAT,
            metadata={"agenda_item": "a1", "stream_callback": AsyncMock(), "run_id": "r"},
        ))
        assert passes == []
        assert "stream_callback" not in seen and "on_iteration" not in seen
        assert seen["outbound_channel"] == ""
    finally:
        agent.stop()


@pytest.mark.asyncio
async def test_a_surface_that_cannot_announce_the_turn_leaves_it_to_the_bus(tmp_path):
    """Without the turn boundary a host would run the notice as an owner
    message (visible, steerable); such a surface declines."""

    async def old_host(*_args):
        return "", {}

    server = GatewayServer(host="127.0.0.1", port=0, on_chat_message=old_host)
    server._session_ws[CHAT] = _FakeWS()
    assert await server.run_report_turn(CHAT, {"content": "notice"}) is False
    assert server._active_tasks == {}

    channel = WebChannel(config=WebChannelConfig(enabled=True), bus=MessageBus())
    channel._session_key_to_relay_id["web:chat"] = "relay-2"
    assert await channel.run_report_turn("web:chat", {"content": "notice"}) is False


@pytest.mark.asyncio
async def test_a_named_agents_report_run_reaches_its_clients_through_the_profile_host():
    """A named agent runs in its own gateway process; the host forwards its
    events. Its report run's opening event, deltas and final pass through as
    they are, marked, so the chat for that agent adopts the run."""
    from flowly.profile_host import ProfileHost

    host = ProfileHost()
    heard: list[dict[str, Any]] = []

    async def listener(envelope: dict[str, Any]) -> None:
        heard.append(envelope)

    host.subscribe_events(listener)
    marked = {"runId": "r1", "sessionKey": "desktop:chat", "turnOrigin": REPORT_TURN_ORIGIN}
    await host._handle_profile_event("writer", "chat", {"state": "run_started", **marked, "hiddenPrompt": True})
    await host._handle_profile_event("writer", "agent", {**marked, "stream": "assistant", "data": {"text": "Hi"}})
    await host._handle_profile_event("writer", "chat", {"state": "final", **marked, "message": {"content": []}})
    chat_events = [e for e in heard if e["type"] in ("chat", "agent")]
    assert [(e["profile"], e["type"], e["data"].get("state")) for e in chat_events] == [
        ("writer", "chat", "run_started"), ("writer", "agent", None), ("writer", "chat", "final"),
    ]
    assert all(e["data"]["turnOrigin"] == REPORT_TURN_ORIGIN for e in chat_events)


@pytest.mark.asyncio
async def test_over_a_real_gateway_socket_a_client_that_opened_the_chat_sees_the_report_stream(tmp_path):
    """End to end on the wire: a client authenticates, opens the conversation
    (chat.inflight binds it), a subagent finishes, and the report run reaches
    that socket — run_started, live text, tool steps, one final."""
    import aiohttp
    from aiohttp.test_utils import TestServer

    provider = _Provider()
    agent = _agent(tmp_path, provider)
    server = _gateway(agent)
    server._require_auth = True
    server._auth_token = "fixture-only"
    try:
        async with TestServer(server._create_app()) as http, aiohttp.ClientSession() as session:
            async with session.post(http.make_url("/api/auth/ws-ticket"),
                                    headers={"Authorization": "Bearer fixture-only"}) as reply:
                assert reply.status == 200
                ticket = (await reply.json())["ticket"]
            ws = await session.ws_connect(http.make_url(f"/ws?ticket={ticket}&clientId=desk"))
            try:
                await ws.send_json({"type": "rpc", "id": "open", "method": "chat.inflight",
                                    "params": {"sessionKey": CHAT}})
                frames: list[dict[str, Any]] = []

                async def until(predicate):
                    async with asyncio.timeout(5):
                        while True:
                            frame = await ws.receive_json()
                            frames.append(frame)
                            if predicate(frame):
                                return frame

                await until(lambda f: f.get("id") == "open")
                await _spawn(agent)
                final = await until(lambda f: f.get("event") == "chat" and f["data"].get("state") == "final")
            finally:
                await ws.close()
        events = [f for f in frames if f.get("type") == "event" and f.get("event") in ("chat", "agent")]
        run_id = final["data"]["runId"]
        assert events[0]["data"]["state"] == "run_started" and events[0]["data"]["runId"] == run_id
        assert any(e["event"] == "agent" for e in events)
        assert any(e["data"].get("state") == "iteration_step" for e in events)
        assert all(e["data"]["runId"] == run_id and e["data"]["turnOrigin"] == REPORT_TURN_ORIGIN
                   for e in events)
        assert final["data"]["message"]["content"][0]["text"] == "It was 2021."
    finally:
        agent.stop()


@pytest.mark.asyncio
async def test_a_report_turn_that_fails_gives_back_what_it_took_and_ends_its_run(tmp_path, monkeypatch):
    """release_taken still runs for a live report: a turn that ends unsaved
    gives back the results it took, nothing is delivered, and the client
    gets the run's one terminal (an error) instead of a stuck stream."""
    provider = _Provider()
    agent = _agent(tmp_path, provider)
    server = _gateway(agent)
    ws = _FakeWS()
    server._session_ws[CHAT] = ws
    released: list[tuple] = []
    real_release = agent.subagents.release_taken
    monkeypatch.setattr(agent.subagents, "release_taken",
                        lambda *args: (released.append(args), real_release(*args)))

    async def broken(**_kwargs):
        raise RuntimeError("provider exploded")

    monkeypatch.setattr(agent, "_run_llm_tool_loop", broken)
    try:
        child = await _spawn(agent)
        await _until(lambda: _finals(ws))
        started, terminal = _chat(ws)[0], _finals(ws)[0]
        assert started["state"] == "run_started"
        assert terminal["state"] == "error" and terminal["runId"] == started["runId"]
        assert terminal["turnOrigin"] == REPORT_TURN_ORIGIN
        assert server.chat_commands.lookup(CHAT, started["runId"])["status"] == "error"
        assert released and released[0][0] == CHAT
        assert agent.subagents.registry.get(child).delivery_state == "queued"
        session = agent.sessions.get_or_create(CHAT)
        assert child not in (session.metadata.get(SUBAGENT_REPORTS_KEY) or [])
    finally:
        agent.stop()


@pytest.mark.asyncio
async def test_a_live_report_does_not_hold_up_another_conversation(tmp_path):
    provider = _Provider()
    provider.hold = asyncio.Event()
    agent = _agent(tmp_path, provider)
    server = _gateway(agent)
    ws = _FakeWS()
    server._session_ws[CHAT] = ws
    try:
        await _spawn(agent)
        await _until(lambda: provider.main_calls >= 2)  # the report is parked mid-turn
        other = _Provider(session_key="desktop:other")
        agent.provider = other
        reply = await asyncio.wait_for(agent.process_direct("hello", session_key="desktop:other"), 5)
        assert reply == "It was 2021."  # answered while the report still runs
        assert not _finals(ws)
    finally:
        provider.hold.set()
        agent.stop()
