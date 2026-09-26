"""A profile child or manually launched gateway must not bounce another unit."""

import os
from unittest.mock import AsyncMock

import pytest

from flowly.integrations import service_ownership as ownership

LABEL = "ai.flowly.gateway"
GROUP = "/user.slice/user-1000.slice/user@1000.service/app.slice/ai.flowly.gateway.service"


@pytest.fixture
def observed(monkeypatch):
    pid = os.getpid()
    identity = {"pid": pid, "start": "123", "boot": "boot"}
    monkeypatch.setattr(ownership, "process_identity", lambda _: identity)
    monkeypatch.setattr(ownership, "_process_cgroups", lambda _: {GROUP})
    values = {"Id": LABEL + ".service", "LoadState": "loaded", "ActiveState": "active",
              "SubState": "running", "MainPID": str(pid), "ControlGroup": GROUP,
              "InvocationID": "a" * 32}
    show = AsyncMock(side_effect=lambda _: "\n".join(f"{k}={v}" for k, v in values.items()))
    monkeypatch.setattr(ownership, "_show", show)
    return values, show


async def test_exact_user_unit_main_process_is_owned(observed):
    _, show = observed
    assert await ownership.owns_current_gateway(LABEL, os.getpid())
    show.assert_awaited_once_with(LABEL + ".service")


@pytest.mark.parametrize("key,value", [
    ("MainPID", "0"), ("MainPID", "99999999"), ("Id", "other.service"),
    ("LoadState", "not-found"), ("ActiveState", "inactive"), ("SubState", "start"),
    ("ControlGroup", "/"), ("ControlGroup", GROUP + "/profile-child"),
    ("ControlGroup", "/another.service"), ("InvocationID", ""),
])
async def test_wrong_or_inactive_unit_is_not_owned(observed, key, value):
    values, _ = observed
    values[key] = value
    assert not await ownership.owns_current_gateway(LABEL, os.getpid())


@pytest.mark.parametrize("output", [None, "", "MainPID=1\nMainPID=1", "broken line"])
async def test_ambiguous_query_is_not_owned(observed, output):
    _, show = observed
    show.side_effect = None
    show.return_value = output
    assert not await ownership.owns_current_gateway(LABEL, os.getpid())


async def test_identity_changes_during_query_are_rejected(observed, monkeypatch):
    from unittest.mock import Mock
    monkeypatch.setattr(ownership, "process_identity", Mock(side_effect=[{"start": "1"}, {"start": "2"}]))
    assert not await ownership.owns_current_gateway(LABEL, os.getpid())


async def test_cgroup_changes_during_query_are_rejected(observed, monkeypatch):
    from unittest.mock import Mock
    monkeypatch.setattr(ownership, "_process_cgroups", Mock(side_effect=[{GROUP}, {GROUP + "/child"}]))
    assert not await ownership.owns_current_gateway(LABEL, os.getpid())


@pytest.mark.parametrize("label,pid", [("--all", None), ("a/b", None), (LABEL, True), (LABEL, 99999999)])
async def test_invalid_target_never_queries_service_manager(observed, label, pid):
    _, show = observed
    assert not await ownership.owns_current_gateway(label, os.getpid() if pid is None else pid)
    show.assert_not_called()


@pytest.mark.parametrize("error", [FileNotFoundError(), PermissionError(), TimeoutError()])
async def test_query_failures_require_manual_restart(observed, error):
    _, show = observed
    show.side_effect = error
    assert not await ownership.owns_current_gateway(LABEL, os.getpid())


@pytest.mark.parametrize("text,expected", [
    ("0::/user.slice/unit.service\n", {"/user.slice/unit.service"}),
    ("1:name=systemd:/user.slice/unit.service\n2:memory:/irrelevant\n", {"/user.slice/unit.service"}),
    ("2:memory:/irrelevant\n", set()),
    ("0::/../unit.service\n", set()),
])
def test_read_only_cgroup_parser(tmp_path, monkeypatch, text, expected):
    path = tmp_path / "cgroup"
    path.write_text(text)
    monkeypatch.setattr(ownership, "Path", lambda _: path)
    assert ownership._process_cgroups(123) == expected


async def test_timed_out_show_kills_and_reaps_its_query(monkeypatch):
    import asyncio
    from unittest.mock import Mock

    process = Mock(returncode=None)
    process.communicate = AsyncMock(side_effect=TimeoutError())
    process.wait = AsyncMock()
    spawn = AsyncMock(return_value=process)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    with pytest.raises(TimeoutError):
        await ownership._show(LABEL + ".service")
    process.kill.assert_called_once()
    process.wait.assert_awaited_once()
    assert "restart" not in spawn.call_args.args


async def test_linux_restart_denies_wrong_owner_without_mutation(monkeypatch):
    from flowly.integrations import service_control

    monkeypatch.setattr(service_control.platform, "system", lambda: "Linux")
    monkeypatch.setattr(service_control.shutil, "which", lambda _: "/usr/bin/systemctl")
    check = AsyncMock(return_value=False)
    mutate = AsyncMock()
    monkeypatch.setattr(ownership, "owns_current_gateway", check)
    monkeypatch.setattr(service_control, "_run", mutate)
    result = await service_control.restart_gateway(expected_pid=os.getpid())
    assert not result.ok and result.method == "no_service"
    assert "manually" in result.detail
    check.assert_awaited_once_with(LABEL, os.getpid())
    mutate.assert_not_called()


async def test_linux_restart_checks_owner_before_command(monkeypatch):
    from flowly.integrations import service_control

    monkeypatch.setattr(service_control.platform, "system", lambda: "Linux")
    monkeypatch.setattr(service_control.shutil, "which", lambda _: "/usr/bin/systemctl")
    events = []

    async def check(*args):
        events.append("check")
        return True

    async def run(command):
        events.append(command)
        return 0, "", ""

    monkeypatch.setattr(ownership, "owns_current_gateway", check)
    monkeypatch.setattr(service_control, "_run", run)
    monkeypatch.setattr(service_control, "_wait_for_port", AsyncMock(return_value=True))
    result = await service_control.restart_gateway(expected_pid=os.getpid())
    assert result.ok
    assert events == ["check", ["systemctl", "--user", "restart", LABEL]]


async def test_gateway_scheduled_restart_pins_its_own_pid(monkeypatch):
    import asyncio
    from types import SimpleNamespace

    from flowly.gateway import server
    from flowly.integrations import service_control

    gateway = object.__new__(server.GatewayServer)
    gateway.host, gateway.port = "127.0.0.1", 12345
    tasks = []

    def schedule(coroutine):
        task = asyncio.create_task(coroutine)
        tasks.append(task)
        return task

    monkeypatch.setattr(server, "asyncio", SimpleNamespace(create_task=schedule, sleep=AsyncMock()))
    restart = AsyncMock(return_value=service_control.RestartResult(False, "no_service", "manual restart"))
    monkeypatch.setattr(service_control, "restart_gateway", restart)
    gateway._schedule_feature_restart()
    await asyncio.gather(*tasks)
    restart.assert_awaited_once_with(
        health_check_host=gateway.host, health_check_port=gateway.port, expected_pid=os.getpid(),
    )
