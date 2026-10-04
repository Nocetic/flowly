from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import flowly.profile as profiles
import flowly.profile_host as profile_host_module
from flowly.gateway.server import GatewayServer
from flowly.profile_host import ProfileHost, _profile_runtime_environment
from flowly.profile_host_contract import ProfileHostError


@pytest.fixture
def profile_roots(tmp_path, monkeypatch: pytest.MonkeyPatch):
    default = tmp_path / ".flowly"
    root = default / "profiles"
    monkeypatch.setattr(profiles, "_DEFAULT_HOME", default)
    monkeypatch.setattr(profiles, "_PROFILES_ROOT", root)
    monkeypatch.setenv("FLOWLY_HOME", str(default))
    default.mkdir(parents=True)
    (default / "workspace").mkdir()
    return default, root


def test_remote_gateway_requires_authentication() -> None:
    with pytest.raises(ValueError, match="requires gateway authentication"):
        GatewayServer(host="0.0.0.0", enable_profile_host=True)


@pytest.mark.asyncio
async def test_profile_rpc_pins_identity_before_read_or_mutation(profile_roots):
    profiles.create_profile('writer', local_runtime=True)
    bot_id = profiles.ensure_profile_bot_id('writer').bot_id
    host = ProfileHost()
    host._target_rpc = AsyncMock(return_value={'messages': []})
    params = {'name': 'writer', 'expectedHostId': host.host_id, 'expectedBotId': 'previous-agent', 'method': 'chat.history',
              'params': {'sessionKey': 'desktop:voice-work:task-1'}}
    with pytest.raises(ProfileHostError) as error:
        await host.dispatch('profiles.rpc', params)
    assert error.value.code == 'PROFILE_IDENTITY_CHANGED'
    host._target_rpc.assert_not_awaited()
    assert host.capabilities()['identityGuardVersion'] == 1
    await host.dispatch('profiles.rpc', {**params, 'expectedBotId': bot_id})
    assert host._target_rpc.call_args.args[2]['expectedBotId'] == bot_id


@pytest.mark.asyncio
async def test_profile_directory_and_statuses_are_public_and_stable(profile_roots) -> None:
    profiles.create_profile("writer", local_runtime=True, model="openai/gpt-5")
    host = ProfileHost()

    directory = await host.list()
    statuses = await host.statuses()

    assert uuid.UUID(directory["hostId"])
    assert {item["name"] for item in directory["profiles"]} == {"default", "writer"}
    assert all("path" not in item for item in directory["profiles"])
    assert all(uuid.UUID(item["botId"]) for item in directory["profiles"])
    writer = next(item for item in directory["profiles"] if item["name"] == "writer")
    primary = next(item for item in directory["profiles"] if item["name"] == "default")
    assert writer["model"] == "openai/gpt-5"
    assert writer["credentialPolicy"] == "isolated"
    assert primary["credentialPolicy"] == "primary"
    assert {item["profile"] for item in statuses["statuses"]} == {"default", "writer"}
    assert all(item["botId"] for item in statuses["statuses"])
    assert host.capabilities()["defaultProfileRpc"] == "direct"
    # Every agent may run at once: the only bound is how many can exist.
    assert host.capabilities()["maxConcurrentRuntimes"] == 15
    assert host.capabilities()["maxNamedProfiles"] == 15
    assert host.capabilities()["profileReadiness"] is True
    assert host.capabilities()["credentialPolicies"] == {
        "namedProfile": "isolated",
        "supported": ["isolated"],
        "sharedCredentialBroker": False,
    }
    assert host.capabilities()["runtimePolicy"] == {
        "strategy": "always-on",
        "queuesWhileBusy": True,
        "idleEviction": False,
        "autostart": False,
        "managerClaim": True,
        "stickyStop": True,
    }
    assert host.capabilities()["roomModes"] == ["panel", "council"]
    assert host.capabilities()["roomLimits"] == {
        "maxMembers": 6,
        "councilRounds": 3,
        "councilTurns": 10,
    }
    assert host.capabilities()["roomStorage"] == {
        "engine": "sqlite-wal",
        "legacyMigration": "verified-copy-preserve-source",
    }
    assert host.capabilities()["roomPrewarm"] is True
    assert "shared-board" in host.capabilities()["profileFeatures"]


@pytest.mark.asyncio
@pytest.mark.parametrize("prefix", ["ios", "android", "web", "desktop"])
async def test_remote_chat_proxy_assigns_identity_and_authority(profile_roots, prefix) -> None:
    profiles.create_profile("writer", local_runtime=True)
    profiles.create_profile("reviewer", local_runtime=True)
    host = ProfileHost()
    runtime = SimpleNamespace(last_used_at=0.0, active_runs=set())
    host._ensure_runtime = AsyncMock(return_value=runtime)  # type: ignore[method-assign]
    host._rpc = AsyncMock(return_value={"runId": "run-1"})  # type: ignore[method-assign]

    result = await host.rpc(
        "writer",
        "chat.send",
        {
            "sessionKey": f"{prefix}:thread-1",
            "message": "Ask @reviewer",
            "profileDirectory": ["spoofed"],
            "profileMentions": ["reviewer", "writer", "missing"],
            "profileMessageContext": {"sourceProfile": "spoofed", "hop": 3},
            "allowedTools": ["exec"],
            "disabledTools": ["message_profile"],
            "turnOrigin": "routine",
        },
    )

    assert result == {"runId": "run-1"}
    sent = host._rpc.await_args.args[2]
    assert sent["sessionKey"] == f"{prefix}:thread-1"
    assert sent["profileDirectory"] == ["default", "reviewer", "writer"]
    assert sent["profileMentions"] == ["reviewer"]
    assert sent["turnOrigin"] == "user"
    assert "profileMessageContext" not in sent
    assert "allowedTools" not in sent
    assert "disabledTools" not in sent
    assert runtime.active_runs == {"run-1"}


@pytest.mark.asyncio
async def test_remote_session_directory_hides_internal_collaboration(profile_roots) -> None:
    profiles.create_profile("writer", local_runtime=True)
    host = ProfileHost()
    runtime = SimpleNamespace(last_used_at=0.0, active_runs=set())
    host._ensure_runtime = AsyncMock(return_value=runtime)  # type: ignore[method-assign]
    host._rpc = AsyncMock(return_value={  # type: ignore[method-assign]
        "sessions": [
            {"key": "ios:visible"},
            {"key": "android:visible"},
            {"key": "desktop:profile-inbox:writer:source"},
            {"key": "desktop:profile-task:private"},
            {"key": "desktop:profile-room:00000000-0000-0000-0000-000000000001"},
            {"key": "cron:internal"},
            {"key": 123},
        ],
    })

    result = await host.rpc("writer", "sessions.list", {})

    assert result == {"sessions": [{"key": "ios:visible"}, {"key": "android:visible"}]}


@pytest.mark.asyncio
async def test_a_remote_session_page_is_filled_past_internal_rows(profile_roots) -> None:
    profiles.create_profile("writer", local_runtime=True)
    host = ProfileHost()
    runtime = SimpleNamespace(last_used_at=0.0, active_runs=set())
    host._ensure_runtime = AsyncMock(return_value=runtime)  # type: ignore[method-assign]
    pages = {
        None: {"sessions": [{"key": "ios:a"}, {"key": "cron:internal"}], "next": "v1:3:c"},
        "v1:3:c": {"sessions": [{"key": "desktop:profile-task:private"}, {"key": "cron:other"}], "next": "v1:2:b"},
        "v1:2:b": {"sessions": [{"key": "android:b"}, {"key": "ios:c"}], "next": "v1:1:a"},
    }
    asked = []

    async def page(_name, _method, params, *_args, **_kwargs):
        asked.append(params.get("before"))
        return pages[params.get("before")]

    host._rpc = page  # type: ignore[method-assign]

    result = await host.rpc("writer", "sessions.list", {"limit": 3})

    # Full page, the agent's own cursor after the last row read.
    assert result == {"sessions": [{"key": "ios:a"}, {"key": "android:b"}, {"key": "ios:c"}], "next": "v1:1:a"}
    assert asked == [None, "v1:3:c", "v1:2:b"]
    # The whole list, as every client asked before paging, reads once.
    asked.clear()
    host._rpc = AsyncMock(return_value=pages[None])  # type: ignore[method-assign]
    assert await host.rpc("writer", "sessions.list", {}) == {"sessions": [{"key": "ios:a"}], "next": "v1:3:c"}


@pytest.mark.asyncio
async def test_internal_board_task_is_scoped_and_hidden_from_public_contract(
    profile_roots,
) -> None:
    profiles.create_profile("writer", local_runtime=True)
    public_events = []

    async def on_event(event):
        public_events.append(event)

    host = ProfileHost(on_event=on_event)
    sent = {}
    started = []

    async def target_rpc(target, method, params, timeout):
        assert target == "writer"
        if method == "chat.send":
            sent.update(params)
            run_id = "task-run-1"
            await host._handle_profile_event("writer", "tool.start", {
                "toolCallId": "tool-1",
                "name": "read_file",
                "args": {"path": "/private/path"},
                "sessionKey": params["sessionKey"],
            })
            await host._handle_profile_event("writer", "tool.complete", {
                "toolCallId": "tool-1",
                "name": "read_file",
                "success": True,
                "durationMs": 12,
                "preview": "private result",
                "sessionKey": params["sessionKey"],
            })
            await host._handle_profile_event("writer", "chat", {
                "runId": run_id,
                "sessionKey": params["sessionKey"],
                "state": "final",
                "message": {"content": "Completed with evidence"},
            })
            return {"runId": run_id}
        raise AssertionError(f"unexpected method: {method}")

    host._target_rpc = target_rpc  # type: ignore[method-assign]
    result = await host.run_task(
        "writer",
        task_id="c_task-1",
        prompt="Prepare the report",
        idempotency_key="claim-1",
        on_started=started.append,
    )

    assert result == {"runId": "task-run-1", "response": "Completed with evidence"}
    assert sent["sessionKey"].startswith("desktop:profile-task:")
    assert sent["turnOrigin"] == "task"
    assert "message_profile" in sent["disabledTools"]
    assert "spawn" in sent["disabledTools"]
    assert "allowedTools" not in sent
    assert public_events == []
    assert started == ["task-run-1"]
    audit = host.task_audit("writer", "task-run-1")
    assert audit is not None
    assert audit["outcome"] == "ok"
    assert audit["toolTrace"] == [{
        "id": "tool-1",
        "tool": "read_file",
        "args_bytes": 24,
        "status": "ok",
        "duration_ms": 12,
    }]
    assert "preview" not in audit["toolTrace"][0]

    with pytest.raises(ProfileHostError) as public:
        await host.dispatch("profiles.task.run", {})
    assert public.value.code == "METHOD_NOT_ALLOWED"


@pytest.mark.parametrize("before_ack", [True, False], ids=["before-ack", "after-ack"])
@pytest.mark.parametrize("kind", ["task", "collaboration"])
@pytest.mark.parametrize(
    "state,aborted,failed,successful",
    [("final", False, False, True), ("final", True, False, False),
     ("error", False, False, False), ("aborted", False, False, False),
     ("final", False, True, False)],
)
@pytest.mark.asyncio
async def test_broker_terminal_result_does_not_depend_on_ack_timing(
    profile_roots, before_ack, kind, state, aborted, failed, successful,
) -> None:
    profiles.create_profile("writer", local_runtime=True)
    host = ProfileHost()
    accepted = asyncio.Event()
    terminal = {}

    async def deliver():
        await host._handle_profile_event("writer", "chat", terminal)

    async def target_rpc(target, method, params, timeout):
        assert method == "chat.send"
        terminal.update({
            "runId": "terminal-run", "sessionKey": params["sessionKey"],
            "state": state, "aborted": aborted, "failed": failed,
            # Interrupted and failed turns can contain useful partial text.
            "message": {"content": "Useful partial output"},
        })
        if before_ack:
            await deliver()
        accepted.set()
        return {"runId": "terminal-run"}

    host._target_rpc = target_rpc
    if kind == "task":
        call = host.run_task(
            "writer", task_id="terminal-task", prompt="Prepare a report",
            idempotency_key="terminal-command",
        )
    else:
        call = host._run_broker_turn(
            source_profile="default", target="writer",
            target_session="desktop:profile-inbox:writer:default",
            message="Prepare a report", correlation_id="terminal-command",
            request_id="terminal-request",
            hop=1, available={"default", "writer"},
        )
    task = asyncio.create_task(call)
    await asyncio.wait_for(accepted.wait(), 1)
    if not before_ack:
        assert ("writer", "terminal-run") in host._broker_waiters
        await deliver()

    if successful:
        assert (await asyncio.wait_for(task, 1))["response"] == "Useful partial output"
    else:
        with pytest.raises(ProfileHostError) as error:
            await asyncio.wait_for(task, 1)
        assert error.value.code == "PROFILE_COLLABORATION_FAILED"
    assert not host._broker_sessions
    assert not host._broker_waiters
    assert not host._terminal_events
    if kind == "task":
        audit = host.task_audit("writer", "terminal-run")
        assert audit["outcome"] == ("ok" if successful else "aborted" if aborted else "error" if failed else state)


@pytest.mark.asyncio
async def test_delete_requires_fresh_confirmation_bound_to_bot(profile_roots) -> None:
    profiles.create_profile("writer", local_runtime=True)
    host = ProfileHost()
    prepared = await host.prepare_delete("writer")

    with pytest.raises(ProfileHostError) as invalid:
        await host.commit_delete("writer", "not-the-confirmation")
    assert invalid.value.code == "DELETE_CONFIRMATION_INVALID"
    assert profiles.profile_exists("writer")

    result = await host.commit_delete("writer", prepared["confirmation"])
    assert result["ok"] is True
    assert result["botId"] == prepared["profile"]["botId"]
    assert not profiles.profile_exists("writer")


@pytest.mark.asyncio
async def test_profile_mutations_reject_ambiguous_parameter_types(profile_roots) -> None:
    host = ProfileHost()
    with pytest.raises(ProfileHostError) as bad_name:
        await host.create({"name": 123})
    assert bad_name.value.code == "INVALID_PARAMS"

    profiles.create_profile("writer", local_runtime=True)
    with pytest.raises(ProfileHostError, match="No bot settings"):
        await host.configure({"name": "writer"})
    with pytest.raises(ProfileHostError) as bad_model:
        await host.configure({"name": "writer", "model": ["not", "a", "string"]})
    assert bad_model.value.code == "INVALID_PARAMS"


@pytest.mark.asyncio
async def test_oauth_profile_creation_reports_isolated_login_requirement(
    profile_roots,
) -> None:
    host = ProfileHost()

    result = await host.create({
        "name": "grok-reviewer",
        "provider": "xai_oauth",
        "model": "xai/grok-4",
    })

    assert result["ok"] is True
    assert result["readiness"] == {
        "runnable": False,
        "authStatus": "login-required",
        "needsLogin": True,
        "missingCapabilities": ["provider-auth"],
        "credentialPolicy": "isolated",
    }
    assert result["profile"]["credentialPolicy"] == "isolated"
    assert (await host.settings("grok-reviewer"))["settings"]["credentialPolicy"] == "isolated"


@pytest.mark.asyncio
async def test_profile_creation_rejects_unsupported_shared_credentials(
    profile_roots,
) -> None:
    host = ProfileHost()

    with pytest.raises(ProfileHostError) as error:
        await host.create({
            "name": "shared-auth",
            "credentialPolicy": "shared",
        })

    assert error.value.code == "CREDENTIAL_POLICY_UNSUPPORTED"
    assert not profiles.profile_exists("shared-auth")


@pytest.mark.asyncio
async def test_profile_creation_limit_is_a_structured_remote_error(profile_roots) -> None:
    for index in range(1, profiles.MAX_NAMED_PROFILES + 1):
        profiles.create_profile(f"worker-{index}", local_runtime=True)
    host = ProfileHost()

    with pytest.raises(ProfileHostError) as error:
        await host.create({"name": "overflow"})

    assert error.value.code == "PROFILE_LIMIT"
    assert error.value.message == (
        "This installation can contain at most 15 bots. "
        "Delete one before creating another."
    )


def _running(name: str, *, busy: bool = False, last_used_at: float = 1.0) -> SimpleNamespace:
    return SimpleNamespace(
        profile=name,
        ws=SimpleNamespace(closed=False),
        process=None,
        owned=True,
        state="connected",
        active_runs={f"run-{name}"} if busy else set(),
        pending={},
        last_used_at=last_used_at,
        attention={},
    )


def _starts_into(host: ProfileHost, started: list[str] | None = None):
    async def start(name: str):
        if started is not None:
            started.append(name)
        runtime = _running(name, last_used_at=99.0)
        host._runtimes[name] = runtime  # type: ignore[assignment]
        return runtime

    return start


_SEVEN = ("one", "two", "three", "four", "five", "six", "seven")


@pytest.mark.asyncio
async def test_opening_another_agent_never_stops_an_idle_one(profile_roots) -> None:
    for name in _SEVEN:
        profiles.create_profile(name, local_runtime=True)
    host = ProfileHost()
    host._runtimes = {  # type: ignore[assignment]
        name: _running(name, last_used_at=float(index))
        for index, name in enumerate(_SEVEN[:6], start=1)
    }
    host._request_runtime_stop = AsyncMock()  # type: ignore[method-assign]
    host._close_runtime = AsyncMock()  # type: ignore[method-assign]
    host._start_runtime = _starts_into(host)  # type: ignore[method-assign]

    result = await host._ensure_runtime("seven")

    assert result.profile == "seven"
    assert set(host._runtimes) == set(_SEVEN)
    host._request_runtime_stop.assert_not_awaited()
    host._close_runtime.assert_not_awaited()


@pytest.mark.asyncio
async def test_busy_agents_do_not_hold_up_opening_another(profile_roots) -> None:
    for name in _SEVEN:
        profiles.create_profile(name, local_runtime=True)
    host = ProfileHost()
    host._runtimes = {  # type: ignore[assignment]
        name: _running(name, busy=True) for name in _SEVEN[:6]
    }
    host._start_runtime = _starts_into(host)  # type: ignore[method-assign]

    result = await asyncio.wait_for(host._ensure_runtime("seven"), 1)

    assert result.profile == "seven"
    assert set(host._runtimes) == set(_SEVEN)


@pytest.mark.asyncio
async def test_an_owner_stop_is_remembered_until_the_agent_is_started_again(profile_roots) -> None:
    profiles.create_profile("writer", local_runtime=True)
    host = ProfileHost()
    host._runtimes = {"writer": _running("writer")}  # type: ignore[assignment]
    host._request_runtime_stop = AsyncMock()  # type: ignore[method-assign]
    host._close_runtime = AsyncMock()  # type: ignore[method-assign]

    await host.dispatch("profiles.stop", {"name": "writer"})

    assert "writer" not in host._runtimes
    assert profiles.describe_profile("writer").stopped_by_user is True
    listed = await host.dispatch("profiles.list", {})
    writer = next(item for item in listed["profiles"] if item["name"] == "writer")
    assert writer["stoppedByUser"] is True

    host._start_runtime = _starts_into(host)  # type: ignore[method-assign]
    await host.dispatch("profiles.connect", {"name": "writer"})

    assert profiles.describe_profile("writer").stopped_by_user is False


@pytest.mark.asyncio
async def test_a_stop_flowly_makes_for_its_own_reasons_is_not_remembered(profile_roots) -> None:
    profiles.create_profile("writer", local_runtime=True)
    host = ProfileHost()
    host._runtimes = {"writer": _running("writer")}  # type: ignore[assignment]
    host._request_runtime_stop = AsyncMock()  # type: ignore[method-assign]
    host._close_runtime = AsyncMock()  # type: ignore[method-assign]

    await host.stop("writer")

    assert profiles.describe_profile("writer").stopped_by_user is False


@pytest.mark.asyncio
async def test_the_host_starts_every_agent_its_owner_has_not_stopped(profile_roots) -> None:
    for name in ("alpha", "beta", "gamma"):
        profiles.create_profile(name, local_runtime=True)
    profiles.set_profile_stopped_by_user("beta", True)
    host = ProfileHost(autostart=True)
    started: list[str] = []
    host._start_runtime = _starts_into(host, started)  # type: ignore[method-assign]

    await host._start_wanted_agents()
    # A second pass finds them running and starts nothing more.
    await host._start_wanted_agents()

    assert started == ["alpha", "gamma"]
    assert host.capabilities()["runtimePolicy"]["autostart"] is True


@pytest.mark.asyncio
async def test_the_keeper_starts_agents_after_the_grace_and_brings_back_a_crashed_one(
    profile_roots, monkeypatch: pytest.MonkeyPatch,
) -> None:
    profiles.create_profile("alpha", local_runtime=True)
    monkeypatch.setattr(profile_host_module, "_KEEPER_STARTUP_GRACE_SECONDS", 0)
    monkeypatch.setattr(profile_host_module, "_KEEPER_INTERVAL_SECONDS", 0.01)
    host = ProfileHost(autostart=True)
    started: list[str] = []
    host._start_runtime = _starts_into(host, started)  # type: ignore[method-assign]

    host.start_keeping_agents()
    host.start_keeping_agents()
    await asyncio.wait_for(_until(lambda: started == ["alpha"]), 1)
    # The agent's process goes away on its own.
    host._runtimes.pop("alpha")
    await asyncio.wait_for(_until(lambda: started == ["alpha", "alpha"]), 1)
    await host.shutdown()


@pytest.mark.asyncio
async def test_while_desktop_claims_the_agents_the_host_leaves_them_to_it(profile_roots) -> None:
    profiles.create_profile("alpha", local_runtime=True)
    host = ProfileHost(autostart=True)
    started: list[str] = []
    host._start_runtime = _starts_into(host, started)  # type: ignore[method-assign]

    assert await host.dispatch("profiles.manager.claim", {"ttlMs": 60_000}) == {"ok": True, "ttlMs": 60_000}
    await host._start_wanted_agents()
    assert started == []

    # The claim lapses (Desktop quit): the host keeps them itself.
    host._manager_claim_until = 0.0
    await host._start_wanted_agents()
    assert started == ["alpha"]


@pytest.mark.asyncio
async def test_a_claim_is_bounded_and_must_be_a_duration(profile_roots) -> None:
    host = ProfileHost(autostart=True)

    assert host.claim_management(1)["ttlMs"] == 10_000
    assert host.claim_management(10**9)["ttlMs"] == 300_000
    with pytest.raises(ProfileHostError) as raised:
        host.claim_management("soon")
    assert raised.value.code == "INVALID_PARAMS"
    assert "profiles.manager.claim" in host.capabilities()["methods"]


@pytest.mark.asyncio
async def test_an_agent_stopped_while_others_start_stays_stopped(profile_roots) -> None:
    for name in ("alpha", "beta"):
        profiles.create_profile(name, local_runtime=True)
    host = ProfileHost(autostart=True)
    started: list[str] = []
    record = _starts_into(host, started)

    async def start(name: str):
        runtime = await record(name)
        # The owner stops beta while alpha is still coming up.
        profiles.set_profile_stopped_by_user("beta", True)
        return runtime

    host._start_runtime = start  # type: ignore[method-assign]

    await host._start_wanted_agents()

    assert started == ["alpha"]
    assert profiles.describe_profile("beta").stopped_by_user is True


@pytest.mark.asyncio
async def test_an_agent_deleted_while_others_start_is_skipped_quietly(
    profile_roots, monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in ("alpha", "beta"):
        profiles.create_profile(name, local_runtime=True)
    host = ProfileHost(autostart=True)
    logged: list[str] = []
    monkeypatch.setattr(profile_host_module, "notable", lambda code, **params: logged.append(code))
    record = _starts_into(host)

    async def start(name: str):
        runtime = await record(name)
        profiles.delete_profile("beta")
        return runtime

    host._start_runtime = start  # type: ignore[method-assign]

    await host._start_wanted_agents()

    assert set(host._runtimes) == {"alpha"}
    assert logged == ["agents.started"]


@pytest.mark.asyncio
async def test_an_agent_that_cannot_start_is_reported_once_and_retried_less_often(
    profile_roots, monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in ("alpha", "beta"):
        profiles.create_profile(name, local_runtime=True)
    host = ProfileHost(autostart=True)
    logged: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        profile_host_module, "notable", lambda code, **params: logged.append((code, params)),
    )
    attempts: list[str] = []
    started = _starts_into(host)

    async def start(name: str):
        if name == "alpha":
            attempts.append(name)
            raise ProfileHostError("PROFILE_START_FAILED", "The model provider rejected the key.")
        return await started(name)

    host._start_runtime = start  # type: ignore[method-assign]

    await host._start_wanted_agents()
    assert set(host._runtimes) == {"beta"}
    assert logged == [
        ("agent.start_failed", {"name": "alpha", "reason": "The model provider rejected the key."}),
        ("agents.started", {"count": 1}),
    ]

    # Within its back-off it is not tried again.
    await host._start_wanted_agents()
    assert attempts == ["alpha"]
    # Once due, it is retried; the same problem is not written again.
    reason, _due, delay = host._keeper_failures["alpha"]
    host._keeper_failures["alpha"] = (reason, 0.0, delay)
    await host._start_wanted_agents()
    assert attempts == ["alpha", "alpha"]
    assert host._keeper_failures["alpha"][2] == delay * 2
    assert [code for code, _ in logged].count("agent.start_failed") == 1


@pytest.mark.asyncio
async def test_a_host_that_does_not_keep_agents_starts_none(profile_roots) -> None:
    profiles.create_profile("writer", local_runtime=True)
    host = ProfileHost()
    host._start_runtime = AsyncMock()  # type: ignore[method-assign]

    host.start_keeping_agents()

    assert host._keeper_task is None
    host._start_runtime.assert_not_called()


@pytest.mark.asyncio
async def test_nothing_but_the_owners_start_wakes_a_stopped_agent(profile_roots) -> None:
    profiles.create_profile("writer", local_runtime=True)
    profiles.set_profile_stopped_by_user("writer", True)
    host = ProfileHost()
    started: list[str] = []
    host._start_runtime = _starts_into(host, started)  # type: ignore[method-assign]

    # A read, a delegated task and a group message all find it stopped.
    for attempt in (
        host.dispatch("profiles.rpc", {"name": "writer", "method": "sessions.list", "params": {}}),
        host._ensure_runtime("writer"),
        host._prepare_room_member("writer"),
    ):
        with pytest.raises(ProfileHostError) as raised:
            await attempt
        assert raised.value.code == "PROFILE_STOPPED"
    assert started == []
    assert profiles.describe_profile("writer").stopped_by_user is True

    # The owner's own start runs it and ends the stop.
    await host.dispatch("profiles.connect", {"name": "writer"})
    assert started == ["writer"]
    assert profiles.describe_profile("writer").stopped_by_user is False


@pytest.mark.asyncio
async def test_opening_a_stopped_agents_chat_does_not_start_it(profile_roots) -> None:
    profiles.create_profile("writer", local_runtime=True)
    profiles.set_profile_stopped_by_user("writer", True)
    host = ProfileHost()
    started: list[str] = []
    host._start_runtime = _starts_into(host, started)  # type: ignore[method-assign]

    with pytest.raises(ProfileHostError) as raised:
        await host.dispatch("profiles.connect", {"name": "writer", "explicit": False})

    assert raised.value.code == "PROFILE_STOPPED"
    assert started == []
    assert profiles.describe_profile("writer").stopped_by_user is True


@pytest.mark.asyncio
async def test_opening_a_running_or_unstopped_agent_still_readies_it(profile_roots) -> None:
    profiles.create_profile("writer", local_runtime=True)
    host = ProfileHost()
    started: list[str] = []
    host._start_runtime = _starts_into(host, started)  # type: ignore[method-assign]

    await host.dispatch("profiles.connect", {"name": "writer", "explicit": False})

    assert started == ["writer"]


def test_the_gateway_keeps_its_agents_only_when_told(profile_roots) -> None:
    keeps = GatewayServer(enable_profile_host=True, autostart_profiles=True)
    leaves = GatewayServer(enable_profile_host=True)

    assert keeps.profile_host is not None and leaves.profile_host is not None
    assert keeps.profile_host.capabilities()["runtimePolicy"]["autostart"] is True
    assert leaves.profile_host.capabilities()["runtimePolicy"]["autostart"] is False


@pytest.mark.parametrize(
    ("peer", "local"),
    [
        (("127.0.0.1", 5000), True),
        (("::1", 5000, 0, 0), True),
        (("::ffff:127.0.0.1", 5000, 0, 0), True),
        (("192.168.1.22", 5000), False),
        (("10.0.0.5", 5000), False),
        (None, False),
    ],
)
def test_only_this_machine_can_claim_its_agents(peer, local: bool) -> None:
    from flowly.gateway.server import _loopback_peer

    request = SimpleNamespace(transport=SimpleNamespace(get_extra_info=lambda key: peer))

    assert _loopback_peer(request) is local
    assert _loopback_peer(None) is False


async def _until(condition) -> None:
    while not condition():
        await asyncio.sleep(0.005)


@pytest.mark.asyncio
async def test_external_runtime_is_visible_but_not_stopped(profile_roots, monkeypatch) -> None:
    profiles.create_profile("writer", local_runtime=True)
    monkeypatch.setattr(
        profile_host_module,
        "reconcile_runtime_lease",
        lambda *_args, **_kwargs: {"instanceId": "other-manager"},
    )
    host = ProfileHost()

    assert host.status("writer")["state"] == "external"
    assert host.status("writer")["owned"] is False
    with pytest.raises(ProfileHostError) as conflict:
        await host.stop("writer")
    assert conflict.value.code == "PROFILE_OWNERSHIP_CONFLICT"
    with pytest.raises(ProfileHostError) as connect_conflict:
        await host.connect("writer")
    assert connect_conflict.value.code == "PROFILE_OWNERSHIP_CONFLICT"


@pytest.mark.asyncio
async def test_attached_runtime_cooperatively_stops_exact_lease(
    profile_roots,
    monkeypatch,
) -> None:
    profiles.create_profile("writer", local_runtime=True)
    host = ProfileHost()
    runtime = SimpleNamespace(
        profile="writer",
        owned=False,
        active_runs=set(),
        capabilities=frozenset({"cooperative-stop-v1"}),
        instance_id="runtime-one",
    )
    host._runtimes["writer"] = runtime  # type: ignore[assignment]
    host._rpc = AsyncMock(return_value={
        "ok": True, "stopping": True, "instanceId": "runtime-one",
    })  # type: ignore[method-assign]
    host._close_runtime = AsyncMock()  # type: ignore[method-assign]
    monkeypatch.setattr(profile_host_module, "reconcile_runtime_lease", lambda *_a, **_k: None)

    result = await host.stop("writer")

    assert result["ok"] is True
    host._rpc.assert_awaited_once_with(
        runtime,
        "runtime.stop",
        {"instanceId": "runtime-one", "reason": "profile-mutation"},
        15,
    )
    host._close_runtime.assert_awaited_once_with(runtime)
    assert "writer" not in host._runtimes


@pytest.mark.asyncio
async def test_managed_runtime_stop_is_instance_bound_and_refuses_active_turns() -> None:
    stopped = asyncio.Event()
    server = GatewayServer(
        host="127.0.0.1",
        auth_token="s" * 48,
        require_loopback_auth=True,
    )
    server.set_managed_runtime_control("runtime-one", stopped.set)
    ws = SimpleNamespace(messages=[], closed=False)

    async def send_json(payload):
        ws.messages.append(payload)

    ws.send_json = send_json
    await server._ws_rpc_runtime_stop(ws, "stale", {"instanceId": "runtime-two"})
    assert ws.messages[-1]["error"]["code"] == "RUNTIME_INSTANCE_CHANGED"

    active = asyncio.create_task(asyncio.sleep(10))
    server._active_tasks["run-one"] = active
    await server._ws_rpc_runtime_stop(ws, "busy", {"instanceId": "runtime-one"})
    assert ws.messages[-1]["error"]["code"] == "RUNTIME_BUSY"
    active.cancel()
    await asyncio.gather(active, return_exceptions=True)
    server._active_tasks.clear()

    await server._ws_rpc_runtime_stop(ws, "stop", {
        "instanceId": "runtime-one", "reason": "delete",
    })
    await asyncio.sleep(0)
    assert ws.messages[-1]["result"] == {
        "ok": True, "stopping": True, "instanceId": "runtime-one",
    }
    assert stopped.is_set()


@pytest.mark.asyncio
async def test_named_bot_can_broker_to_default_through_primary_gateway(profile_roots) -> None:
    profiles.create_profile("writer", local_runtime=True)

    async def on_chat(
        _session_key,
        _message,
        _run_id,
        stream_callback,
        _media,
        _voice_mode,
        _iteration_callback,
        _render_capabilities,
        _metadata,
    ):
        await stream_callback("Primary answer")
        return "Primary answer", {}

    server = GatewayServer(
        host="127.0.0.1",
        on_chat_message=on_chat,
        enable_profile_host=True,
    )
    host = server._profile_host
    assert host is not None

    result = await host._broker("writer", {
        "sourceProfile": "writer",
        "sourceSessionKey": "ios:writer-thread",
        "targetProfile": "default",
        "message": "Please review this",
        "correlationId": "correlation-1",
        "hop": 1,
    })

    assert result == {
        "ok": True,
        "targetProfile": "default",
        "runId": result["runId"],
        "response": "Primary answer",
        "correlationId": "correlation-1",
        "hop": 1,
    }
    assert server._profile_host_client_id not in server._ws_clients
    await server.stop()


@pytest.mark.asyncio
async def test_profile_rpc_dispatch_preserves_structured_error() -> None:
    server = GatewayServer(host="127.0.0.1")
    fake_host = SimpleNamespace(
        dispatch=AsyncMock(side_effect=ProfileHostError(
            "PROFILE_CAPACITY",
            "Four bots are already running. Stop one and try again.",
            retryable=True,
        ))
    )
    server._profile_host = fake_host
    ws = SimpleNamespace(closed=False, messages=[])

    async def send_json(payload):
        ws.messages.append(payload)

    ws.send_json = send_json
    await server._handle_ws_rpc(ws, "ios-client", {
        "type": "rpc",
        "id": "rpc-1",
        "method": "profiles.rpc",
        "params": {"name": "writer", "method": "chat.history", "params": {}},
    })

    assert ws.messages == [{
        "type": "rpc",
        "id": "rpc-1",
        "error": {
            "code": "PROFILE_CAPACITY",
            "message": "Four bots are already running. Stop one and try again.",
            "retryable": True,
        },
    }]


@pytest.mark.asyncio
async def test_gateway_media_read_rpc_is_windowed_and_scoped(profile_roots) -> None:
    default, _root = profile_roots
    media_dir = default / "media"
    media_dir.mkdir()
    (media_dir / "generated.png").write_bytes(b"abcdefgh")
    server = GatewayServer(host="127.0.0.1")
    ws = SimpleNamespace(closed=False, messages=[])

    async def send_json(payload):
        ws.messages.append(payload)

    ws.send_json = send_json
    await server._handle_ws_rpc(ws, "ios-client", {
        "type": "rpc",
        "id": "media-1",
        "method": "media.read",
        "params": {"mediaId": "generated.png", "offset": 2, "length": 3},
    })

    assert ws.messages == [{
        "type": "rpc",
        "id": "media-1",
        "result": {
            "mediaId": "generated.png",
            "size": 8,
            "mimeType": "image/png",
            "offset": 2,
            "eof": False,
            "data": "Y2Rl",
        },
    }]


@pytest.mark.asyncio
async def test_wrapped_default_profile_rpc_uses_primary_gateway(profile_roots) -> None:
    primary = AsyncMock(return_value={"messages": []})
    host = ProfileHost(primary_rpc=primary)

    result = await host.dispatch("profiles.rpc", {
        "name": "default",
        "method": "chat.history",
        "params": {"sessionKey": "ios:default-thread"},
        "timeoutMs": 5_000,
    })

    assert result == {"messages": []}
    primary.assert_awaited_once_with(
        "chat.history", {"sessionKey": "ios:default-thread"}, 5.0
    )
    assert host.capabilities()["wrappedDefaultProfileRpc"] is True


@pytest.mark.asyncio
async def test_profile_host_forwards_shared_service_with_runtime_bound_source(
    profile_roots,
) -> None:
    primary = AsyncMock(return_value={"ok": True, "output": '{"ok":true}'})
    host = ProfileHost(primary_rpc=primary)

    class Socket:
        closed = False

        def __init__(self) -> None:
            self.frames: list[dict] = []

        async def send_json(self, frame: dict) -> None:
            self.frames.append(frame)

    socket = Socket()
    runtime = SimpleNamespace(profile="writer", ws=socket)
    params = {
        "service": "artifacts",
        "tool": "artifact",
        "arguments": {"action": "list"},
        "sourceProfile": "writer",
        "sourceSessionKey": "ios:writer:one",
        "turnOrigin": "user",
        "correlationId": "shared-1",
    }
    await host._handle_shared_service_request(  # type: ignore[arg-type]
        runtime,
        {"id": "shared-1", "params": params},
    )

    primary.assert_awaited_once_with("shared.invoke", params, 600)
    assert socket.frames == [{
        "type": "shared_service_result",
        "id": "shared-1",
        "result": {"ok": True, "output": '{"ok":true}'},
    }]


@pytest.mark.asyncio
async def test_profile_events_include_host_and_bot_identity(profile_roots) -> None:
    profiles.create_profile("writer", local_runtime=True)
    events: list[dict] = []

    async def on_event(event: dict) -> None:
        events.append(event)

    host = ProfileHost(on_event=on_event)
    await host._emit("writer", "connection", {"state": "starting"})

    assert events[0]["hostId"] == host.host_id
    assert uuid.UUID(events[0]["botId"])
    assert events[0]["profile"] == "writer"
    assert events[0]["type"] == "connection"


@pytest.mark.asyncio
async def test_internal_broker_serializes_turns_per_target(profile_roots) -> None:
    profiles.create_profile("writer", local_runtime=True)
    profiles.create_profile("reviewer", local_runtime=True)
    host = ProfileHost()
    active = 0
    max_active = 0

    async def run_turn(**_kwargs):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0)
        active -= 1
        return {"ok": True}

    host._run_broker_turn = run_turn  # type: ignore[method-assign]
    params = {
        "sourceProfile": "writer",
        "sourceSessionKey": "ios:one",
        "targetProfile": "reviewer",
        "message": "Review",
        "correlationId": "correlation-1",
        "hop": 1,
    }
    await asyncio.gather(host._broker("writer", params), host._broker("writer", params))

    assert max_active == 1


@pytest.mark.asyncio
async def test_internal_broker_grants_bounded_profile_follow_up(profile_roots) -> None:
    profiles.create_profile("writer", local_runtime=True)
    profiles.create_profile("reviewer", local_runtime=True)
    host = ProfileHost()
    captured: dict = {}

    async def target_rpc(profile: str, method: str, params: dict, _timeout: float):
        assert profile == "reviewer"
        assert method == "chat.send"
        captured.update(params)
        run_id = "review-run"
        async def finish() -> None:
            await asyncio.sleep(0)
            await host._handle_profile_event("reviewer", "chat", {
                "runId": run_id,
                "sessionKey": params["sessionKey"],
                "state": "final",
                "message": {"content": "Reviewed"},
            })

        asyncio.create_task(finish())
        return {"runId": run_id}

    host._target_rpc = target_rpc  # type: ignore[method-assign]
    result = await host._broker("writer", {
        "sourceProfile": "writer",
        "sourceSessionKey": "ios:writer-thread",
        "targetProfile": "reviewer",
        "message": "Please review this",
        "correlationId": "correlation-1",
        "hop": 1,
    })

    assert result["response"] == "Reviewed"
    assert "message_profile" in captured["allowedTools"]
    assert captured["profileMessageContext"]["hop"] == 1


def test_profile_runtime_environment_drops_owner_credentials(monkeypatch) -> None:
    inherited = {
        "PATH": "/usr/bin",
        "AWS_REGION": "eu-west-1",
        "GH_TOKEN": "user-owned",
        "OPENAI_API_KEY": "owner-provider-secret",
        "MOLTBOT_PROXY_JWT_SECRET": "owner-relay-secret",
        "FLOWLY_HOME": "/owner/home",
        "FLOWLY_CWD": "/owner/workspace",
        "FLOWLY_PROFILE": "owner",
        "FLOWLY_SERVER_ID": "owner-server",
    }
    for key, value in inherited.items():
        monkeypatch.setenv(key, value)

    child = _profile_runtime_environment()

    assert child["PATH"] == "/usr/bin"
    assert child["AWS_REGION"] == "eu-west-1"
    assert child["GH_TOKEN"] == "user-owned"
    for secret in (
        "OPENAI_API_KEY",
        "MOLTBOT_PROXY_JWT_SECRET",
        "FLOWLY_HOME",
        "FLOWLY_CWD",
        "FLOWLY_PROFILE",
        "FLOWLY_SERVER_ID",
    ):
        assert secret not in child


@pytest.mark.asyncio
async def test_profile_events_are_scoped_to_bound_conversation(profile_roots) -> None:
    profiles.create_profile("writer", local_runtime=True)
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

    writer_a = socket()
    writer_b = socket()
    directory = socket()
    server._ws_clients.update({
        "writer-a": writer_a,
        "writer-b": writer_b,
        "directory": directory,
    })
    server._bind_profile_client_request("writer-a", "profiles.rpc", {
        "name": "writer",
        "method": "chat.history",
        "params": {"sessionKey": "ios:a"},
    })
    server._bind_profile_client_request("writer-b", "profiles.rpc", {
        "name": "writer",
        "method": "chat.history",
        "params": {"sessionKey": "ios:b"},
    })
    server._bind_profile_client_request("directory", "profiles.list", {})

    await server._broadcast_profile_host_event({
        "hostId": "host",
        "profile": "writer",
        "botId": "bot",
        "type": "chat",
        "data": {
            "sessionKey": "ios:a",
            "runId": "run-a",
            "state": "final",
        },
    })
    assert len(writer_a.messages) == 1
    assert writer_b.messages == []
    assert directory.messages == []

    server._profile_run_subscriptions[("writer", "run-a")] = "ios:a"
    source = {"profile": "writer", "botId": "bot", "type": "agent",
              "data": {"runId": "run-a", "stream": "assistant", "delta": "Hello"}}
    await server._broadcast_profile_host_event(source)
    assert writer_a.messages[-1]["data"]["data"]["sessionKey"] == "ios:a"
    assert "sessionKey" not in source["data"]
    assert writer_b.messages == []

    await server._broadcast_profile_host_event({
        "hostId": "host",
        "profile": "writer",
        "botId": "bot",
        "type": "connection",
        "data": {"state": "connected"},
    })
    assert len(writer_a.messages) == 3
    assert len(writer_b.messages) == 1
    assert len(directory.messages) == 1

    await server.stop()


def test_default_profile_event_lease_tracks_direct_client(profile_roots) -> None:
    server = GatewayServer(host="127.0.0.1", enable_profile_host=True)
    assert server._profile_host_external_events is False

    server._bind_profile_client_request("ios", "profiles.rpc", {
        "name": "default",
        "method": "chat.history",
        "params": {"sessionKey": "ios:default"},
    })
    assert server._profile_host_external_events is True

    server._remove_profile_client_subscription("ios")
    assert server._profile_host_external_events is False


def test_rejected_internal_session_is_not_bound_to_direct_client(profile_roots) -> None:
    server = GatewayServer(host="127.0.0.1", enable_profile_host=True)

    server._bind_profile_client_request("ios", "profiles.rpc", {
        "name": "default",
        "method": "chat.history",
        "params": {"sessionKey": "desktop:profile-inbox:writer:source"},
    })

    assert server._profile_client_subscriptions["ios"].conversations == set()
    server._remove_profile_client_subscription("ios")


@pytest.mark.asyncio
async def test_broker_accepts_the_name_the_user_says(profile_roots) -> None:
    """The model is told to send an id, and may still send the name it heard.

    Asked to message Friday, a model holding only ids either says no such bot
    exists or forwards the word the user used. The second is recoverable, so it
    is recovered — as long as exactly one bot answers to that name.
    """
    profiles.create_profile("dqwdqwd", local_runtime=True, display_name="Friday")

    async def on_chat(
        _session_key, _message, _run_id, stream_callback, _media,
        _voice_mode, _iteration_callback, _render_capabilities, _metadata,
    ):
        await stream_callback("Hello from Friday")
        return "Hello from Friday", {}

    server = GatewayServer(host="127.0.0.1", on_chat_message=on_chat, enable_profile_host=True)
    host = server._profile_host
    assert host is not None

    captured: dict[str, str] = {}

    async def run_turn(**kwargs):
        captured["target"] = kwargs["target"]
        return {"ok": True, "targetProfile": kwargs["target"]}

    host._run_broker_turn = run_turn  # type: ignore[method-assign]

    result = await host._broker("default", {
        "sourceProfile": "default",
        "sourceSessionKey": "desktop:thread",
        "targetProfile": "Friday",
        "message": "Say hello",
        "correlationId": "c1",
        "hop": 1,
    })

    # Resolved to the id, not passed through as the label.
    assert captured["target"] == "dqwdqwd"
    assert result["targetProfile"] == "dqwdqwd"


@pytest.mark.asyncio
async def test_broker_still_refuses_a_name_that_identifies_nobody(profile_roots) -> None:
    profiles.create_profile("dqwdqwd", local_runtime=True, display_name="Friday")
    server = GatewayServer(host="127.0.0.1", enable_profile_host=True)
    host = server._profile_host
    assert host is not None

    with pytest.raises(ProfileHostError) as error:
        await host._broker("default", {
            "sourceProfile": "default",
            "sourceSessionKey": "desktop:thread",
            "targetProfile": "Gandalf",
            "message": "Say hello",
            "correlationId": "c1",
            "hop": 1,
        })
    assert error.value.code == "PROFILE_NOT_FOUND"


@pytest.mark.parametrize('profile', ['default', 'writer'])
@pytest.mark.asyncio
async def test_interactive_task_uses_visible_session_and_preserves_questions(profile_roots, profile):
    from flowly.session.manager import SessionManager

    profiles.create_profile('writer', local_runtime=True)
    events = []

    async def on_event(event):
        events.append(event)

    host = ProfileHost(on_event=on_event)
    sent = {}

    async def target_rpc(target, method, params, timeout):
        assert target == profile
        if method == 'runtime.voice.reserve':
            if profile == 'default':
                SessionManager(profiles.get_flowly_home() / 'workspace').reserve_voice_work(params['sessionKey'])
            return {'sessionKey': params['sessionKey'], 'reserved': True}
        if method == 'goal.get':
            return {'goal': None}
        if method == 'chat.command':
            return {'runId': params['runId'], 'status': 'completed', 'goalBinding': {'version': 1, 'state': 'none'}}
        # An internal headless task would auto-answer/deny these; interactive
        # work must leave them for the user in the visible work conversation.
        assert method == 'chat.send'
        sent.update(params)
        for event, data in [
            ('agent.clarify.requested', {'id': 'question-1', 'question': 'Which report?'}),
            ('exec.approval.requested', {'id': 'approval-1'}),
            ('chat', {'runId': 'voice-run', 'state': 'final', 'message': {'content': 'Report ready'}}),
        ]:
            payload = {**data, 'sessionKey': params['sessionKey']}
            if profile == 'default':
                await host.handle_primary_frame({'type': 'event', 'event': event, 'data': payload})
            else:
                await host._handle_profile_event(profile, event, payload)
        return {'runId': 'voice-run'}

    host._target_rpc = target_rpc
    result = await host.run_task(profile, task_id='c_voice', prompt='Prepare the report',
                                 idempotency_key='voice:c_voice:initial', interactive=True)
    await asyncio.sleep(0)
    assert sent['sessionKey'] == 'desktop:voice-work:c_voice'
    assert sent['turnOrigin'] == 'user'
    assert sent['disabledTools'] == []
    assert result['response'] == 'Report ready'
    assert len(events) == 3
    assert not host._interactive_task_sessions
    assert host.task_audit(profile, 'voice-run')['outcome'] == 'ok'


@pytest.mark.asyncio
async def test_interactive_task_replay_reads_verified_handoff(profile_roots):
    host = ProfileHost()

    async def target_rpc(target, method, params, timeout):
        if method == 'runtime.voice.reserve':
            return {'sessionKey': params['sessionKey'], 'reserved': True}
        if method == 'goal.get':
            return {'goal': None}
        if method == 'chat.send':
            return {'runId': 'voice-run', 'status': 'completed', 'replayed': True}
        if method == 'chat.command':
            return {'runId': params['runId'], 'status': 'completed', 'goalBinding': {'version': 1, 'state': 'none'}}
        assert method == 'chat.history'
        return {'messages': [
            {'role': 'assistant', 'runId': 'other-run', 'content': 'Unrelated reply'},
            {'role': 'assistant', 'runId': 'voice-run', 'content': 'Verified handoff'},
        ]}

    host._target_rpc = target_rpc
    result = await host.run_task('default', task_id='c_voice', prompt='Prepare the report',
                                 idempotency_key='voice:c_voice:initial', interactive=True)
    assert result['response'] == 'Verified handoff'


@pytest.mark.asyncio
async def test_interactive_task_uncertain_receipt_does_not_start_or_abort_again(profile_roots):
    host = ProfileHost()
    host._target_rpc = AsyncMock(side_effect=[
        {'sessionKey': 'desktop:voice-work:c_voice', 'reserved': True},
        {'runId': 'voice-run', 'status': 'status_unknown'},
    ])
    started = []
    with pytest.raises(ProfileHostError) as error:
        await host.run_task('default', task_id='c_voice', prompt='Prepare the report',
                            idempotency_key='voice:c_voice:initial', interactive=True, on_started=started.append)
    assert error.value.code == 'TASK_RESULT_UNKNOWN'
    assert [c.args[1] for c in host._target_rpc.await_args_list] == ['runtime.voice.reserve', 'chat.send']
    assert started == []


@pytest.mark.asyncio
async def test_interactive_task_rejects_recreated_target_before_sending(profile_roots):
    host = ProfileHost()
    host._target_rpc = AsyncMock()
    with pytest.raises(ProfileHostError) as error:
        await host.run_task('default', task_id='c_voice', prompt='Prepare the report',
                            idempotency_key='voice:c_voice:initial', interactive=True,
                            expected_bot_id='previous-agent-identity')
    assert error.value.code == 'TASK_TARGET_CHANGED'
    host._target_rpc.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('worker_status,active,expected', [
    ('aborted', False, 'stopped'), ('completed', False, 'stopped'),
    ('error', False, 'stopped'), ('running', False, 'stopping'),
    ('status_unknown', False, 'status_unknown'), ('not_found', False, 'status_unknown'),
    ('completed', True, 'stopped'),
])
async def test_task_stop_requires_worker_terminal_and_no_active_goal_turn(profile_roots, worker_status, active, expected):
    host = ProfileHost()
    calls = []

    async def rpc(profile, method, params, timeout):
        calls.append(method)
        assert params['sessionKey'] == 'desktop:voice-work:c_voice'
        assert params['expectedBotId'] == profiles.ensure_profile_bot_id('default').bot_id
        if method == 'goal.stop':
            return {'goal': {'status': 'cleared'}}
        if method == 'goal.get':
            return {'goal': None}
        if method == 'chat.command':
            return {'runId': 'worker-1', 'status': worker_status, 'goalBinding': {'version': 1, 'state': 'none'}}
        if method == 'chat.abort':
            return {'ok': True, 'cancelled': True}  # Accepted abort is not terminal proof.
        assert method == 'chat.inflight'
        return {'inflight': {'runId': 'goal-turn'} if active else None, 'goal': None}

    host._target_rpc = rpc
    result = await host.stop_task(profile='default', task_id='c_voice', run_id='worker-1',
                                  expected_bot_id=profiles.ensure_profile_bot_id('default').bot_id)
    assert result['status'] == expected
    assert calls[0] == 'chat.command'
    assert 'goal.stop' not in calls
    assert 'chat.send' not in calls
    assert ('chat.abort' in calls) == (worker_status == 'running')


@pytest.mark.parametrize('content', ['', '   ', []])
def test_empty_terminal_is_not_a_successful_board_audit(profile_roots, content):
    host = ProfileHost()
    audit = {}
    host._task_audit_sessions[('writer', 'desktop:voice-work:task-1')] = audit
    host._capture_task_audit_event('writer', 'desktop:voice-work:task-1', 'chat', {
        'state': 'final', 'message': {'content': content},
    })
    assert audit['outcome'] == 'error'


@pytest.mark.asyncio
@pytest.mark.parametrize("restarting", [False, True])
async def test_the_gateway_says_whether_it_is_stopping_or_restarting(
    profile_roots, monkeypatch: pytest.MonkeyPatch, restarting: bool,
) -> None:
    server = GatewayServer(enable_profile_host=True)
    sent: list[dict] = []

    async def broadcast(event: dict) -> None:
        sent.append(event)

    monkeypatch.setattr(server, "_broadcast_clients", broadcast)
    if restarting:
        monkeypatch.setattr(asyncio, "create_task", lambda coro: coro.close())
        server._schedule_feature_restart()

    await server.stop()

    assert sent[0] == {"type": "event", "event": "gateway.stopping", "data": {"restarting": restarting}}
