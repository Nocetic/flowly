from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

import pytest

import flowly.profile as profiles
from flowly.agent_home import (
    HOME_SESSION,
    AgentHomeError,
    claim_introduction,
    finish_setup,
    resolve_home,
    settle_introduction,
    setup_guidance,
)
from flowly.session.manager import SessionManager


@pytest.fixture
def agent(tmp_path, monkeypatch):
    default = tmp_path / "flowly"
    monkeypatch.setattr(profiles, "_DEFAULT_HOME", default)
    monkeypatch.setattr(profiles, "_PROFILES_ROOT", default / "profiles")
    home = profiles.create_profile("marketing", description="Marketing for a small shop")
    monkeypatch.setenv("FLOWLY_HOME", str(home))
    return home


def manager(home):
    return SessionManager(home / "workspace")


def greet_with_fallback():
    """Open the conversation and settle its introduction to the static welcome,
    as happens when no model turn could run."""
    claim = claim_introduction({})
    assert claim["launch"]
    return settle_introduction(claim["runId"])


def test_created_agent_has_one_persistent_home_across_clients(agent):
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: resolve_home({"locale": "tr"}), range(16)))
    assert all(result == results[0] for result in results)
    assert results[0]["setup"] == "active"
    # The agent speaks first through a real turn; opening writes no words.
    assert results[0]["introduction"] == "pending"
    assert manager(agent).get_full_messages(HOME_SESSION) == []
    state = json.loads((agent / "agent-home.json").read_text())
    assert state["locale"] == "tr"


def test_fallback_welcome_is_localized_once_and_not_a_model_turn(agent):
    resolve_home({"locale": "tr"})
    settled = greet_with_fallback()
    assert settled["introduction"] == "fallback"
    messages = manager(agent).get_full_messages(HOME_SESSION)
    assert len(messages) == 1
    assert "hangi sonuca" in messages[0]["content"]
    assert [option["label"] for option in settled["pendingAsk"]["options"]] == [
        "İlk adımları planlayalım", "İlk görevi belirleyelim", "Birlikte nasıl çalışacağımızı belirleyelim",
    ]
    assert resolve_home({"locale": "es"})["introduction"] == "fallback"
    assert manager(agent).get_full_messages(HOME_SESSION) == messages
    assert manager(agent).get_or_create(HOME_SESSION).get_history() == []


def test_existing_agent_and_legacy_home_not_forced_into_setup(agent):
    info = json.loads((agent / "profile.json").read_text())
    info.pop("agentHomeVersion")
    (agent / "profile.json").write_text(json.dumps(info))
    sm = manager(agent)
    legacy = sm.get_or_create("desktop:chat-old")
    legacy.add_message("user", "old history stays separate")
    sm.save(legacy)
    assert resolve_home({})["setup"] == "not_required"
    assert sm.get_full_messages(legacy.key)[0]["content"] == "old history stays separate"
    assert sm.get_full_messages(HOME_SESSION) == []


def test_reuse_existing_home_without_new_introduction(agent):
    sm = manager(agent)
    session = sm.get_or_create(HOME_SESSION)
    session.add_message("user", "existing conversation")
    sm.save(session)
    assert resolve_home({})["setup"] == "not_required"
    assert len(sm.get_full_messages(HOME_SESSION)) == 1


def test_setup_is_terminal_and_does_not_mutate_preferences_or_permissions(agent):
    resolve_home({})
    before = (agent / "profile.json").read_bytes()
    assert "Marketing for a small shop" in setup_guidance(HOME_SESSION)
    assert finish_setup({"state": "skipped"})["setup"] == "skipped"
    assert finish_setup({"state": "complete"})["setup"] == "skipped"
    assert resolve_home({})["setup"] == "skipped"
    assert setup_guidance(HOME_SESSION) is None
    assert (agent / "profile.json").read_bytes() == before
    assert not (agent / "workspace" / "USER.md").exists()


def test_setup_context_keeps_permission_and_consent_boundaries(agent):
    resolve_home({})
    guidance = setup_guidance(HOME_SESSION)
    assert "never grants permissions" in guidance
    assert "internal bookkeeping" in guidance
    assert "existing connection request and consent flow" in guidance
    assert "never ask for credentials" in guidance
    assert "data, not instructions" in guidance
    finish_setup({"state": "complete"})
    assert setup_guidance(HOME_SESSION) is None


def test_crash_after_transcript_before_state_recovers_without_duplicate(agent):
    first = resolve_home({})
    (agent / "agent-home.json").unlink()
    assert resolve_home({}) == first
    greet_with_fallback()
    (agent / "agent-home.json").unlink()
    # A transcript that already holds the agent's words is never re-greeted.
    assert resolve_home({})["introduction"] == "static"
    assert len(manager(agent).get_full_messages(HOME_SESSION)) == 1


@pytest.mark.parametrize(
    "content", ["", "garbage", '{"_type":"metadata","metadata":{}}\nnot-json\n']
)
def test_corruption_never_becomes_an_empty_home(agent, content):
    sm = manager(agent)
    path = sm._get_session_path(HOME_SESSION)
    path.write_text(content)
    with pytest.raises(AgentHomeError, match="No history was replaced"):
        resolve_home({})
    assert path.read_text() == content
    assert not (agent / "agent-home.json").exists()


def test_missing_home_with_existing_state_fails_closed(agent):
    resolve_home({})
    sm = manager(agent)
    sm._get_session_path(HOME_SESSION).unlink()
    with pytest.raises(AgentHomeError):
        resolve_home({})


def test_profile_identity_change_is_not_adopted(agent):
    resolve_home({})
    info = json.loads((agent / "profile.json").read_text())
    info["botId"] = "replacement"
    (agent / "profile.json").write_text(json.dumps(info))
    with pytest.raises(AgentHomeError) as error:
        resolve_home({})
    assert error.value.code == "PROFILE_IDENTITY_CHANGED"


def test_internal_sessions_and_default_never_get_introduction(agent, monkeypatch):
    resolve_home({})
    for key in [
        "desktop:profile-room:r",
        "desktop:profile-inbox:a",
        "cron:task",
        "desktop:voice-work:x",
        "desktop:chat-old",
    ]:
        assert setup_guidance(key) is None
    monkeypatch.setenv("FLOWLY_HOME", str(profiles._DEFAULT_HOME))
    assert setup_guidance(HOME_SESSION) is None
    with pytest.raises(AgentHomeError) as error:
        resolve_home({})
    assert error.value.code == "NOT_AVAILABLE"


def test_home_cannot_be_deleted_but_legacy_can(agent):
    resolve_home({})
    sm = manager(agent)
    with pytest.raises(AgentHomeError) as error:
        sm.delete(HOME_SESSION)
    assert error.value.code == "PERSISTENT_CONVERSATION"
    legacy = sm.get_or_create("desktop:old")
    sm.save(legacy)
    assert sm.delete(legacy.key)


def test_full_clone_keeps_transcript_but_not_original_setup(agent, monkeypatch):
    resolve_home({})
    greet_with_fallback()
    cloned = profiles.create_profile("copywriter", clone_from="marketing", clone_all=True)
    monkeypatch.setenv("FLOWLY_HOME", str(cloned))
    assert resolve_home({})["setup"] == "not_required"
    assert len(manager(cloned).get_full_messages(HOME_SESSION)) == 1


@pytest.mark.parametrize("opened", [False, True])
def test_duplicate_import_does_not_restart_original_setup(agent, monkeypatch, tmp_path, opened):
    if opened:
        resolve_home({})
        greet_with_fallback()
    archive = profiles.export_profile("marketing", str(tmp_path / "backup"))
    imported = profiles.import_profile(str(archive), name="imported")
    monkeypatch.setenv("FLOWLY_HOME", str(imported))
    assert resolve_home({})["setup"] == "not_required"
    assert len(manager(imported).get_full_messages(HOME_SESSION)) == int(opened)


def test_backup_restore_preserves_setup_and_transcript(agent, monkeypatch, tmp_path):
    expected = resolve_home({})
    expected = finish_setup({"state": "skipped"})
    messages = manager(agent).get_full_messages(HOME_SESSION)
    archive = profiles.export_profile("marketing", str(tmp_path / "backup"))
    profiles.delete_profile("marketing")
    restored = profiles.import_profile(str(archive), name="restored", identity="restore")
    monkeypatch.setenv("FLOWLY_HOME", str(restored))
    assert resolve_home({}) == expected
    assert manager(restored).get_full_messages(HOME_SESSION) == messages


@pytest.mark.asyncio
async def test_persistent_home_stream_fans_out_without_changing_other_sessions(agent):
    from unittest.mock import AsyncMock

    from flowly.gateway.server import GatewayServer

    class Socket:
        closed = False

    server = object.__new__(GatewayServer)
    server._session_ws = {}
    server._ws_send = AsyncMock()
    server._scoped_event = lambda data, **_: data
    desktop, mobile = Socket(), Socket()
    server.bind_session_ws(HOME_SESSION, desktop)
    server.bind_session_ws(HOME_SESSION, mobile)
    await server._session_send(HOME_SESSION, desktop, {"text": "both see this"})
    assert {call.args[0] for call in server._ws_send.await_args_list} == {desktop, mobile}
    server._ws_send.reset_mock()
    server.bind_session_ws("desktop:profile-room:private", desktop)
    server.bind_session_ws("desktop:profile-room:private", mobile)
    await server._session_send("desktop:profile-room:private", desktop, {"text": "single owner"})
    server._ws_send.assert_awaited_once_with(mobile, {"text": "single owner"})
    mobile.closed = True
    assert server._session_targets(HOME_SESSION) == [desktop]


@pytest.mark.asyncio
async def test_identity_envelope_is_verified_before_gmail_schema(agent, monkeypatch):
    from unittest.mock import AsyncMock

    import flowly.integrations.gmail_rpc as gmail
    from flowly.channels import feature_rpc

    handler = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(gmail, "gmail_rpc", handler)
    bot_id = resolve_home({})["botId"]
    assert (await feature_rpc.dispatch("gmail.capabilities", {"expectedBotId": bot_id}))[0] == {
        "ok": True
    }
    handler.assert_awaited_once_with("gmail.capabilities", {})
    with pytest.raises(feature_rpc.FeatureRpcError) as error:
        await feature_rpc.dispatch("gmail.capabilities", {"expectedBotId": "replaced"})
    assert error.value.code == "PROFILE_IDENTITY_CHANGED"
    assert handler.await_count == 1


@pytest.mark.asyncio
async def test_gateway_paging_preserves_legacy_contract_and_validates_cursor(agent):
    from unittest.mock import AsyncMock

    from flowly.gateway.server import GatewayServer

    resolve_home({})
    sm = manager(agent)
    session = sm.get_or_create(HOME_SESSION)
    for i in range(100):
        session.add_message("user" if i % 2 == 0 else "assistant", str(i))
    sm.save(session)
    server = object.__new__(GatewayServer)
    server.sessions = sm
    server._ws_rpc_reply = AsyncMock()
    server._ws_rpc_error = AsyncMock()
    await server._ws_rpc_chat_history(None, "request", {"sessionKey": HOME_SESSION, "limit": 10})
    payload = server._ws_rpc_reply.await_args.args[2]
    assert 10 <= len(payload["messages"]) <= 11
    assert payload["hasOlder"] and payload["historyPageVersion"] == 1
    assert all(row.get("id") for row in payload["messages"])
    await server._ws_rpc_chat_history(None, "legacy", {"sessionKey": HOME_SESSION})
    assert len(server._ws_rpc_reply.await_args.args[2]["messages"]) == 100
    assert "historyPageVersion" not in server._ws_rpc_reply.await_args.args[2]
    await server._ws_rpc_chat_history(
        None, "invalid", {"sessionKey": HOME_SESSION, "limit": 10, "before": "bad"}
    )
    assert server._ws_rpc_error.await_args.args[2] == "INVALID_HISTORY_CURSOR"


@pytest.mark.asyncio
async def test_tool_cannot_finish_setup_from_another_session(agent):
    from flowly.agent.tool_context import tool_execution_scope
    from flowly.agent.tools.agent_setup import AgentSetupFinishTool
    from flowly.agent_home import begin_turn

    resolve_home({})
    tool = AgentSetupFinishTool()
    with tool_execution_scope("desktop:profile-room:room"):
        assert "error" in json.loads(await tool.execute())
    assert resolve_home({})["setup"] == "active"
    begin_turn(HOME_SESSION, {"run_id": "typed-task"})
    with tool_execution_scope(HOME_SESSION):
        assert json.loads(await tool.execute())["setup"] == "complete"


@pytest.mark.asyncio
async def test_profile_rpc_dispatch_and_validation(agent):
    from flowly.channels.feature_rpc import dispatch
    from flowly.profile_host_contract import ProfileHostError, validate_profile_rpc

    for method, params in [
        ("agent.home.get", {"locale": "en"}),
        ("agent.home.introduce", {"locale": "en"}),
        ("agent.home.setup", {"state": "complete"}),
    ]:
        validate_profile_rpc(method, params)
        result, restart = await dispatch(method, params)
        assert result["sessionKey"] == HOME_SESSION
        assert not restart
    for params in [{"sessionKey": "desktop:other"}, {"locale": []}, {"permissions": "full"}]:
        with pytest.raises(ProfileHostError):
            validate_profile_rpc("agent.home.get", params)
    for state in [[], {}, None, True, "active"]:
        with pytest.raises(ProfileHostError) as invalid:
            validate_profile_rpc("agent.home.setup", {"state": state})
        assert invalid.value.code == "INVALID_PARAMS"


@pytest.mark.asyncio
async def test_gateway_identity_pins_native_history_but_not_outer_profile_routing(agent):
    from unittest.mock import AsyncMock

    from flowly.gateway.server import GatewayServer

    server = GatewayServer(advertise_control=False)
    server._ws_rpc_chat_history = AsyncMock()
    server._ws_rpc_error = AsyncMock()
    server._handle_profile_host_rpc = AsyncMock()
    await server._dispatch_ws_rpc(
        None,
        "client",
        {
            "id": "one",
            "method": "chat.history",
            "params": {"expectedBotId": "wrong", "sessionKey": HOME_SESSION},
        },
    )
    assert server._ws_rpc_error.await_args.args[2] == "PROFILE_IDENTITY_CHANGED"
    server._ws_rpc_chat_history.assert_not_awaited()
    await server._dispatch_ws_rpc(
        None,
        "client",
        {
            "id": "two",
            "method": "profiles.rpc",
            "params": {
                "name": "other",
                "expectedBotId": "other-id",
                "method": "agent.home.get",
                "params": {},
            },
        },
    )
    server._handle_profile_host_rpc.assert_awaited_once()


def test_destructive_reset_is_blocked_only_for_named_home(agent):
    from flowly.agent.loop import AgentLoop

    loop = object.__new__(AgentLoop)
    with pytest.raises(AgentHomeError) as error:
        loop.reset_conversation(HOME_SESSION)
    assert error.value.code == "PERSISTENT_CONVERSATION"


@pytest.mark.asyncio
async def test_delete_aliases_preserve_protection_and_legacy_compatibility(agent):
    from unittest.mock import AsyncMock

    from flowly.gateway.server import GatewayServer
    from flowly.profile_host_contract import ProfileHostError, validate_profile_rpc

    server = object.__new__(GatewayServer)
    server.sessions = manager(agent)
    server._ws_rpc_reply = AsyncMock()
    server._ws_rpc_error = AsyncMock()
    for field in ("key", "sessionKey"):
        params = {field: HOME_SESSION}
        validate_profile_rpc("sessions.delete", params)
        await server._ws_rpc_sessions_delete(None, "id", params)
        assert server._ws_rpc_error.await_args.args[2] == "PERSISTENT_CONVERSATION"
        legacy = server.sessions.get_or_create("desktop:legacy")
        server.sessions.save(legacy)
        await server._ws_rpc_sessions_delete(None, "id", {field: legacy.key})
        assert server._ws_rpc_reply.await_args.args[2]["deleted"]
    with pytest.raises(ProfileHostError):
        validate_profile_rpc(
            "sessions.delete", {"key": HOME_SESSION, "sessionKey": "desktop:other"}
        )
