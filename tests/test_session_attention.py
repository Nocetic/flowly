"""A conversation that waits on its owner says so, in every list.

Four registries can hold a turn until the owner acts — exec approvals,
clarify questions, plans awaiting approval and chat connection requests.
``flowly.session.attention`` reads them into one ``needsInput`` per
conversation; ``sessions.list`` carries it per row and the profile host
keeps each named bot's most urgent wait for ``profiles.status``.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import flowly.clarify.manager as clarify_module
import flowly.exec.approval_manager as approval_module
import flowly.plans.store as plan_store_module
import flowly.profile as profiles
import flowly.profile_host as profile_host_module
from flowly.clarify.manager import ClarifyManager
from flowly.clarify.types import ClarifyRequest
from flowly.exec.approval_manager import ApprovalManager
from flowly.exec.types import ExecRequest, PendingApproval
from flowly.plans.approval import PlanApprovalManager
from flowly.plans.manager import PlanManager
from flowly.plans.store import PlanStore
from flowly.profile_host import ProfileHost, _Runtime
from flowly.session.attention import most_urgent, pending_inputs


@pytest.fixture
def registries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Fresh, isolated registries in place of the process-wide singletons."""
    approvals = ApprovalManager()
    questions = ClarifyManager()
    plans = PlanManager(store=PlanStore(root=tmp_path / "plans", hydrate=False),
                        approvals=PlanApprovalManager())
    monkeypatch.setattr(approval_module, "_manager", approvals)
    monkeypatch.setattr(clarify_module, "_manager", questions)
    monkeypatch.setattr(plan_store_module, "_singleton", plans.store)
    return SimpleNamespace(approvals=approvals, questions=questions, plans=plans)


def _approval(session_key: str, approval_id: str = "a1", created_at: float | None = None) -> PendingApproval:
    now = time.time()
    return PendingApproval(id=approval_id, request=ExecRequest(command="rm -rf build"),
                           created_at=now if created_at is None else created_at,
                           expires_at=now + 30, session_key=session_key, kind="exec")


def _question(session_key: str, question_id: str = "q1", created_at: float | None = None) -> ClarifyRequest:
    now = time.time()
    return ClarifyRequest(id=question_id, question="Which one?", choices=["A", "B"],
                          session_key=session_key,
                          created_at=now if created_at is None else created_at,
                          expires_at=now + 30)


async def _until(predicate, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition was never met")
        await asyncio.sleep(0.005)


# ── pending_inputs ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_waiting_approval_marks_only_its_conversation_until_answered(registries):
    wait = asyncio.create_task(registries.approvals.request_and_wait(_approval("desktop:one")))
    await _until(lambda: registries.approvals.list_pending())

    waiting = pending_inputs()
    assert set(waiting) == {"desktop:one"}
    assert waiting["desktop:one"]["kind"] == "approval"
    assert waiting["desktop:one"]["count"] == 1

    assert registries.approvals.resolve("a1", "deny")
    await wait
    assert pending_inputs() == {}


@pytest.mark.asyncio
async def test_a_question_is_reported_as_a_question(registries):
    wait = asyncio.create_task(registries.questions.request_and_wait(_question("ios:two")))
    await _until(lambda: registries.questions.list_pending())

    assert pending_inputs()["ios:two"]["kind"] == "question"

    registries.questions.resolve("q1", "A")
    await wait
    assert pending_inputs() == {}


@pytest.mark.asyncio
async def test_a_plan_waiting_for_approval_is_reported_until_decided(registries):
    mgr = registries.plans
    proposal = asyncio.create_task(mgr.propose(
        "desktop:plan", "ship it", mgr.build_steps([{"id": 1, "content": "A"}]), timeout_s=5,
    ))
    await _until(lambda: "desktop:plan" in pending_inputs())
    assert pending_inputs()["desktop:plan"]["kind"] == "plan"

    current = mgr.get_current("desktop:plan")
    await mgr.resolve_approval(current.id, "reject", expected_revision=current.approval.revision,
                               decision_id="d1")
    await proposal
    assert pending_inputs() == {}


def test_a_chat_connection_request_is_reported():
    waiting = pending_inputs([{"sessionKey": "desktop:mcp", "createdAt": 12.5}])
    assert waiting == {"desktop:mcp": {"kind": "connection", "since": 12500, "count": 1}}


def test_a_connection_names_the_service_it_is_for():
    waiting = pending_inputs([{"sessionKey": "desktop:mcp", "createdAt": 1, "name": "higgsfield"}])
    assert waiting["desktop:mcp"]["subject"] == "higgsfield"
    assert most_urgent(waiting)["subject"] == "higgsfield"


@pytest.mark.asyncio
async def test_a_tool_action_is_named_but_a_shell_command_never_is(registries):
    action = _approval("desktop:mail", "a1")
    action.kind = "action"
    action.request = ExecRequest(command="Send   email\n to team@example.com")
    command = _approval("desktop:shell", "a2")
    waits = [asyncio.create_task(registries.approvals.request_and_wait(item)) for item in (action, command)]
    await _until(lambda: len(registries.approvals.list_pending()) == 2)

    waiting = pending_inputs()
    assert waiting["desktop:mail"]["subject"] == "Send email to team@example.com"
    assert "subject" not in waiting["desktop:shell"]

    registries.approvals.resolve("a1", "deny")
    registries.approvals.resolve("a2", "deny")
    await asyncio.gather(*waits)


def test_a_long_subject_is_cut_to_one_short_line():
    waiting = pending_inputs([{"sessionKey": "desktop:mcp", "createdAt": 1, "name": "x" * 200}])
    subject = waiting["desktop:mcp"]["subject"]
    assert len(subject) == 80 and subject.endswith("…")


@pytest.mark.asyncio
async def test_the_subject_comes_from_the_wait_that_is_shown(registries):
    wait = asyncio.create_task(registries.questions.request_and_wait(_question("desktop:both", created_at=5.0)))
    await _until(lambda: registries.questions.list_pending())

    # The question outranks the connection, so the connection's name is not shown.
    waiting = pending_inputs([{"sessionKey": "desktop:both", "createdAt": 1, "name": "higgsfield"}])
    assert waiting["desktop:both"]["kind"] == "question"
    assert "subject" not in waiting["desktop:both"]

    registries.questions.resolve("q1", "A")
    await wait


@pytest.mark.asyncio
async def test_several_waits_show_the_most_urgent_kind_and_the_oldest_start(registries):
    first = asyncio.create_task(registries.questions.request_and_wait(
        _question("desktop:busy", created_at=100.0)))
    second = asyncio.create_task(registries.approvals.request_and_wait(
        _approval("desktop:busy", created_at=200.0)))
    await _until(lambda: registries.questions.list_pending() and registries.approvals.list_pending())

    waiting = pending_inputs([{"sessionKey": "desktop:busy", "createdAt": 300.0}])
    # A blocked command outranks a question; the wait began with the question.
    assert waiting["desktop:busy"] == {"kind": "approval", "since": 100_000, "count": 3}

    registries.approvals.resolve("a1", "deny")
    registries.questions.resolve("q1", "B")
    await asyncio.gather(first, second)


def test_one_broken_registry_does_not_hide_the_others(monkeypatch):
    def broken():
        raise RuntimeError("registry offline")

    monkeypatch.setattr(approval_module, "get_approval_manager", broken)
    waiting = pending_inputs([{"sessionKey": "desktop:mcp", "createdAt": 1}])
    assert set(waiting) == {"desktop:mcp"}


def test_most_urgent_picks_kind_first_then_the_longest_wait():
    waits = {
        "desktop:old-plan": {"kind": "plan", "since": 1, "count": 1},
        "desktop:new-question": {"kind": "question", "since": 50, "count": 2},
        "desktop:old-question": {"kind": "question", "since": 10, "count": 1},
    }
    assert most_urgent(waits) == {"sessionKey": "desktop:old-question", "kind": "question",
                                  "since": 10, "count": 4}
    assert most_urgent({}) is None


# ── sessions.list / sessions.attention ─────────────────────────────────────


@pytest.fixture
def temp_flowly_home(tmp_path, monkeypatch):
    home = tmp_path / "flowly-home"
    home.mkdir()
    monkeypatch.setenv("FLOWLY_HOME", str(home))
    if hasattr(profiles, "_cached_home"):
        profiles._cached_home = None
    return home


@pytest.mark.asyncio
async def test_sessions_list_marks_the_waiting_row_and_no_other(temp_flowly_home, registries):
    from flowly.channels.feature_rpc import _DISPATCH, sessions_list
    from flowly.session.manager import SessionManager

    manager = SessionManager(workspace=temp_flowly_home)
    for key in ("desktop:waiting", "desktop:quiet"):
        session = manager.get_or_create(key)
        session.add_message("user", "hello")
        manager.save(session)

    wait = asyncio.create_task(registries.approvals.request_and_wait(_approval("desktop:waiting")))
    await _until(lambda: registries.approvals.list_pending())

    rows = {row["key"]: row for row in sessions_list()["sessions"]}
    assert rows["desktop:waiting"]["needsInput"]["kind"] == "approval"
    assert "needsInput" not in rows["desktop:quiet"]

    handler, takes_params, needs_restart = _DISPATCH["sessions.attention"]
    assert (takes_params, needs_restart) == (False, False)
    assert set(handler()["sessions"]) == {"desktop:waiting"}

    registries.approvals.resolve("a1", "deny")
    await wait
    rows = {row["key"]: row for row in sessions_list()["sessions"]}
    assert "needsInput" not in rows["desktop:waiting"]


def test_an_account_sees_only_the_waits_it_may_open(temp_flowly_home, monkeypatch):
    import flowly.channels.feature_rpc as feature_rpc
    from flowly.live_voice.authority import RequestOwner, request_owner_scope

    sessions = temp_flowly_home / "sessions"
    sessions.mkdir()
    (sessions / "desktop_voice_mine.jsonl").write_text(json.dumps(
        {"_type": "metadata", "metadata": {"voiceOwner": {"kind": "account", "uid": "a"}}}) + "\n")
    (sessions / "desktop_voice_theirs.jsonl").write_text(json.dumps(
        {"_type": "metadata", "metadata": {"voiceOwner": {"kind": "account", "uid": "b"}}}) + "\n")
    wait = {"kind": "plan", "since": 1, "count": 1}
    monkeypatch.setattr(feature_rpc, "_pending_inputs", lambda: {
        "desktop:voice:mine": wait, "desktop:voice:theirs": wait, "desktop:plain": wait,
    })

    with request_owner_scope(RequestOwner(uid="a")):
        assert set(feature_rpc.sessions_attention()["sessions"]) == {"desktop:voice:mine", "desktop:plain"}
    # In-process callers (the profile host's own runtime link) see everything.
    assert len(feature_rpc.sessions_attention()["sessions"]) == 3


# ── profile host ────────────────────────────────────────────────────────────


@pytest.fixture
def profile_roots(tmp_path, monkeypatch: pytest.MonkeyPatch):
    default = tmp_path / ".flowly"
    root = default / "profiles"
    monkeypatch.setattr(profiles, "_DEFAULT_HOME", default)
    monkeypatch.setattr(profiles, "_PROFILES_ROOT", root)
    monkeypatch.setenv("FLOWLY_HOME", str(default))
    default.mkdir(parents=True)
    (default / "workspace").mkdir()
    monkeypatch.setattr(profile_host_module, "_ATTENTION_DEBOUNCE_SECONDS", 0)
    return default, root


def _host_with_runtime(*, capable: bool = True, answer=None):
    profiles.create_profile("writer", local_runtime=True)
    events: list[dict] = []

    async def on_event(event: dict) -> None:
        events.append(event)

    host = ProfileHost(on_event=on_event)
    runtime = _Runtime(
        "writer", None, SimpleNamespace(), SimpleNamespace(closed=False), "instance",
        capabilities=frozenset({"session-attention-v1"}) if capable else frozenset(),
    )
    host._runtimes["writer"] = runtime
    calls: list[str] = []

    async def rpc(target, method, params, timeout):
        assert target is runtime
        calls.append(method)
        return answer() if callable(answer) else answer

    async def must_not_start(*_args, **_kwargs):
        raise AssertionError("an attention read must never start a runtime")

    host._rpc = rpc  # type: ignore[method-assign]
    host._target_rpc = must_not_start  # type: ignore[method-assign]
    host._ensure_runtime = must_not_start  # type: ignore[method-assign]
    return host, runtime, calls, events


@pytest.mark.asyncio
async def test_a_bot_status_shows_what_its_conversations_wait_on(profile_roots):
    state = {"sessions": {"desktop:home": {"kind": "question", "since": 5, "count": 1}}}
    host, runtime, calls, events = _host_with_runtime(answer=lambda: state)

    await host._handle_profile_event("writer", "agent.clarify.requested",
                                     {"id": "q1", "sessionKey": "desktop:home"}, runtime=runtime)
    await _until(lambda: runtime.attention)

    assert calls == ["sessions.attention"]
    assert host.status("writer")["needsInput"] == {
        "sessionKey": "desktop:home", "kind": "question", "since": 5, "count": 1,
    }
    changed = [event for event in events if event["type"] == "needsInput"]
    assert changed[-1]["data"] == {"needsInput": host.status("writer")["needsInput"]}

    # The runtime is the truth: once it reports nothing, the badge goes.
    state = {"sessions": {}}
    await host._handle_profile_event("writer", "agent.clarify.closed",
                                     {"id": "q1", "sessionKey": "desktop:home"}, runtime=runtime)
    await _until(lambda: not runtime.attention)
    assert "needsInput" not in host.status("writer")
    assert [event for event in events if event["type"] == "needsInput"][-1]["data"] == {
        "needsInput": None,
    }


@pytest.mark.asyncio
async def test_a_turn_ending_rereads_attention_for_waits_without_events(profile_roots):
    host, runtime, calls, _events = _host_with_runtime(answer={"sessions": {}})

    await host._handle_profile_event("writer", "chat",
                                     {"runId": "r1", "sessionKey": "desktop:home", "state": "final"},
                                     runtime=runtime)
    await _until(lambda: calls)
    assert calls == ["sessions.attention"]


@pytest.mark.asyncio
async def test_an_older_runtime_is_never_asked(profile_roots):
    host, runtime, calls, _events = _host_with_runtime(capable=False, answer={"sessions": {}})

    await host._handle_profile_event("writer", "exec.approval.requested",
                                     {"id": "a1", "sessionKey": "desktop:home"}, runtime=runtime)
    await asyncio.sleep(0.05)
    assert calls == []
    assert "needsInput" not in host.status("writer")


@pytest.mark.asyncio
async def test_a_replaced_or_closed_runtime_is_not_read(profile_roots):
    host, runtime, calls, _events = _host_with_runtime(answer={"sessions": {}})
    host._runtimes["writer"] = object()  # type: ignore[assignment]

    host._schedule_attention_refresh(runtime)
    await asyncio.sleep(0.05)
    assert calls == []

    host._runtimes["writer"] = runtime
    runtime.ws.closed = True
    host._schedule_attention_refresh(runtime)
    await asyncio.sleep(0.05)
    assert calls == []


@pytest.mark.asyncio
async def test_hidden_and_malformed_waits_are_dropped(profile_roots):
    answer = {"sessions": {
        "desktop:profile-inbox:reviewer:s1": {"kind": "approval", "since": 1, "count": 1},
        "telegram:123": {"kind": "approval", "since": 1, "count": 1},
        "desktop:odd": {"kind": "shout", "since": 1, "count": 1},
        "desktop:home": {"kind": "plan", "since": -4, "count": True, "subject": 7},
        "ios:mcp": {"kind": "connection", "since": 1, "count": 1, "subject": "  higgs\nfield "},
    }}
    host, runtime, _calls, _events = _host_with_runtime(answer=answer)

    host._schedule_attention_refresh(runtime)
    await _until(lambda: runtime.attention)
    assert runtime.attention == {
        "desktop:home": {"kind": "plan", "since": 0, "count": 1},
        "ios:mcp": {"kind": "connection", "since": 1, "count": 1, "subject": "higgs field"},
    }


@pytest.mark.asyncio
async def test_a_bot_status_names_what_its_most_urgent_wait_is_for(profile_roots):
    answer = {"sessions": {"ios:mcp": {"kind": "connection", "since": 1, "count": 1, "subject": "higgsfield"}}}
    host, runtime, _calls, _events = _host_with_runtime(answer=answer)

    host._schedule_attention_refresh(runtime)
    await _until(lambda: runtime.attention)
    assert host.status("writer")["needsInput"]["subject"] == "higgsfield"


@pytest.mark.asyncio
async def test_a_burst_of_events_is_one_read(profile_roots, monkeypatch):
    monkeypatch.setattr(profile_host_module, "_ATTENTION_DEBOUNCE_SECONDS", 0.05)
    host, runtime, calls, _events = _host_with_runtime(answer={"sessions": {}})

    for _ in range(5):
        host._schedule_attention_refresh(runtime)
    await runtime.attention_task
    assert calls == ["sessions.attention"]


@pytest.mark.asyncio
async def test_a_status_read_does_not_keep_an_idle_bot_alive(profile_roots):
    host, runtime, _calls, _events = _host_with_runtime(answer={"sessions": {}})
    runtime.last_used_at = 1000.0

    async def rpc(target, method, params, timeout):
        target.last_used_at = time.time()
        return {"sessions": {}}

    host._rpc = rpc  # type: ignore[method-assign]
    host._schedule_attention_refresh(runtime)
    await runtime.attention_task
    assert runtime.last_used_at == 1000.0


def test_the_local_runtime_advertises_attention():
    from flowly.cli.gateway_cmd import _LOCAL_RUNTIME_CAPABILITIES

    assert profile_host_module._ATTENTION_CAPABILITY in _LOCAL_RUNTIME_CAPABILITIES


# ── delivery of the bot badge ───────────────────────────────────────────────


_BADGE = {"hostId": "host", "profile": "writer", "botId": "bot", "type": "needsInput",
          "data": {"needsInput": {"sessionKey": "ios:a", "kind": "approval", "since": 1, "count": 1}}}


@pytest.mark.asyncio
async def test_gateway_sends_the_bot_badge_to_the_directory_and_the_bots_readers(profile_roots):
    from flowly.gateway.server import GatewayServer

    profiles.create_profile("writer", local_runtime=True)
    profiles.create_profile("reviewer", local_runtime=True)
    server = GatewayServer(host="127.0.0.1", enable_profile_host=True)

    def socket():
        ws = SimpleNamespace(closed=False, messages=[])

        async def send_json(payload):
            ws.messages.append(payload)

        async def close():
            ws.closed = True

        ws.send_json = send_json
        ws.close = close
        return ws

    reader, other, directory = socket(), socket(), socket()
    server._ws_clients.update({"reader": reader, "other": other, "directory": directory})
    server._bind_profile_client_request("reader", "profiles.rpc", {
        "name": "writer", "method": "chat.history", "params": {"sessionKey": "ios:b"}})
    server._bind_profile_client_request("other", "profiles.rpc", {
        "name": "reviewer", "method": "chat.history", "params": {"sessionKey": "ios:c"}})
    server._bind_profile_client_request("directory", "profiles.list", {})

    await server._broadcast_profile_host_event(_BADGE)

    assert [m["data"]["type"] for m in directory.messages] == ["needsInput"]
    assert [m["data"]["type"] for m in reader.messages] == ["needsInput"]
    assert other.messages == []
    await server.stop()


@pytest.mark.asyncio
async def test_relay_sends_the_bot_badge_to_the_directory_and_the_bots_readers():
    from unittest.mock import AsyncMock

    from flowly.bus.queue import MessageBus
    from flowly.channels.web import WebChannel
    from flowly.config.schema import WebChannelConfig

    channel = WebChannel(WebChannelConfig(), MessageBus())
    sent: list[dict] = []
    channel._send_or_queue = AsyncMock(side_effect=lambda frame: sent.append(json.loads(frame)))
    now = time.monotonic()
    channel._profile_directory_sessions = {"directory": now}
    channel._profile_conversation_sessions = {
        ("writer", "ios:b"): {"reader": now},
        ("reviewer", "ios:c"): {"other": now},
    }

    await channel._forward_profile_event(_BADGE)

    assert sorted(frame["sessionId"] for frame in sent) == ["directory", "reader"]
