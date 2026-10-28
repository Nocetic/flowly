"""A gateway restarts itself in place, whoever runs it.

The restart a settings change asks for used to go to the machine's service
manager: a named agent bounced its host's service instead of itself, a
gateway with no service restarted nothing while the app showed
"restarting", and the port answering (the old process) counted as success.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import textwrap
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import flowly.profile as profiles
from flowly import self_restart
from flowly.profile_host import ProfileHost


@pytest.fixture(autouse=True)
def _clean_self_restart(monkeypatch: pytest.MonkeyPatch):
    from flowly.integrations import service_control

    monkeypatch.setattr(service_control, "_restart_requested", False)
    monkeypatch.setattr(self_restart, "_stop", None)
    monkeypatch.setattr(self_restart, "_requested", False)
    monkeypatch.setattr(self_restart, "_drop_flags", frozenset())


def test_the_restart_replays_the_command_line_without_one_shot_flags(monkeypatch) -> None:
    monkeypatch.setattr(sys, "orig_argv", [sys.executable, "-m", "flowly.cli.entry", "gateway", "--rotate-token", "--port", "1"])
    self_restart.install(lambda: None, drop_flags=("--rotate-token",))

    assert self_restart.command() == [sys.executable, "-m", "flowly.cli.entry", "gateway", "--port", "1"]


@pytest.mark.skipif(os.name != "posix", reason="restart in place is POSIX-only")
def test_a_restarted_process_keeps_its_pid(tmp_path) -> None:
    """The real thing: stop, exec the same command line, same process id."""
    script = tmp_path / "gateway.py"
    script.write_text(textwrap.dedent(
        """
        import os, sys
        from flowly import self_restart

        if os.environ.get("FIRST_PID"):
            print(f"{os.environ['FIRST_PID']} {os.getpid()} {' '.join(sys.argv[1:])}")
            sys.exit(0)
        os.environ["FIRST_PID"] = str(os.getpid())
        self_restart.install(lambda: None, drop_flags=("--rotate-token",))
        assert self_restart.request()
        self_restart.reexec()
        """
    ), encoding="utf-8")

    done = subprocess.run(
        [sys.executable, str(script), "--rotate-token", "--port", "1"],
        capture_output=True, text=True, timeout=60, check=True,
        env={**os.environ, "FIRST_PID": ""},
    )

    first, second, args = done.stdout.strip().split(" ", 2)
    assert first == second
    assert args == "--port 1"


@pytest.mark.asyncio
async def test_a_settings_restart_goes_in_place_not_to_the_service(monkeypatch) -> None:
    from flowly.gateway import server
    from flowly.integrations import service_control

    gateway = object.__new__(server.GatewayServer)
    gateway.host, gateway.port = "127.0.0.1", 12345
    tasks = []

    def schedule(coroutine):
        task = asyncio.create_task(coroutine)
        tasks.append(task)
        return task

    stopped = []
    self_restart.install(lambda: stopped.append(True))
    monkeypatch.setattr(server, "asyncio", SimpleNamespace(create_task=schedule, sleep=AsyncMock()))
    service = AsyncMock()
    monkeypatch.setattr(service_control, "restart_gateway", service)

    gateway._schedule_feature_restart()
    await asyncio.gather(*tasks)

    assert stopped == [True]
    assert self_restart.requested()
    service.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_failed_service_restart_leaves_the_goodbye_a_stop(monkeypatch) -> None:
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
    monkeypatch.setattr(
        service_control, "restart_gateway",
        AsyncMock(return_value=service_control.RestartResult(False, "no_service", "manual restart")),
    )

    gateway._schedule_feature_restart()
    await asyncio.gather(*tasks)

    assert gateway._restarting is False


@pytest.mark.asyncio
async def test_a_hand_started_gateway_can_be_restarted_from_an_app(monkeypatch) -> None:
    from flowly.channels import feature_rpc
    from flowly.integrations import service_control

    monkeypatch.setattr(service_control, "service_runs_this_process", AsyncMock(return_value=False))
    monkeypatch.setattr(self_restart.os, "name", "posix")
    self_restart.install(lambda: None)

    result, needs_restart = await feature_rpc.dispatch("gateway.restart", {})

    assert result == {"ok": True, "willRestart": True}
    assert needs_restart is True


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "posix", reason="managed process-group lifecycle is POSIX-specific")
async def test_a_named_agent_restarts_itself_and_its_host_takes_it_back(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A real agent under a real host: same process, new connection, still answering."""
    home = tmp_path / "home"
    default = home / ".flowly"
    root = default / "profiles"
    (default / "workspace").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("OPENAI_API_KEY", "test-restart-key")
    monkeypatch.setattr(profiles, "_DEFAULT_HOME", default)
    monkeypatch.setattr(profiles, "_PROFILES_ROOT", root)
    created = profiles.create_profile(
        "writer", local_runtime=True, provider="openai", model="openai/gpt-4o-mini",
    )
    config_path = created / "config.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config.setdefault("providers", {}).setdefault("openai", {})["apiKey"] = "test-restart-key"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    host = ProfileHost()

    try:
        await host.connect("writer")
        before = host._runtimes["writer"]
        assert before.process is not None
        pid = before.process.pid

        # A settings change that needs a restart, as an app sends it.
        reply = await host._rpc(before, "config.set", {"patch": {"agents": {}}, "restart": True}, 15)
        assert reply == {"ok": True, "willRestart": True}

        deadline = time.monotonic() + 90
        while True:
            current = host._runtimes.get("writer")
            if current is not None and current is not before and host._runtime_is_open(current):
                break
            assert time.monotonic() < deadline, "the agent did not come back"
            assert before.process.returncode is None, "the host stopped the restarting agent"
            await asyncio.sleep(0.1)

        assert current.process is before.process and current.process.pid == pid
        assert current.owned is True
        assert current.instance_id != before.instance_id
        assert await host.rpc("writer", "sessions.list", {}) == {"sessions": []}
    finally:
        await host.shutdown()

    assert before.process.returncode is not None


def _launchd(pids: list[int | None], kicked: list[list[str]]):
    """A launchctl that reports each pid in turn (the last one sticks)."""
    async def run(cmd: list[str]) -> tuple[int, str, str]:
        if cmd[:2] == ["launchctl", "kickstart"]:
            kicked.append(cmd)
            return 0, "", ""
        pid = pids.pop(0) if len(pids) > 1 else pids[0]
        if pid is None:
            return 0, '{\n\t"LastExitStatus" = 0;\n};', ""
        return 0, f'{{\n\t"PID" = {pid};\n\t"Label" = "ai.flowly.gateway";\n}};', ""

    return run


@pytest.mark.asyncio
async def test_a_named_agent_never_kicks_its_hosts_service(monkeypatch) -> None:
    from flowly.integrations import service_control

    kicked: list[list[str]] = []
    monkeypatch.setattr(service_control.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(service_control, "_run", _launchd([os.getpid() + 1], kicked))

    result = await service_control.restart_gateway(expected_pid=os.getpid())

    assert result.ok is False and result.method == "no_service"
    assert kicked == []


@pytest.mark.asyncio
async def test_the_old_process_answering_the_port_is_not_a_restart(monkeypatch) -> None:
    from flowly.integrations import service_control

    kicked: list[list[str]] = []
    monkeypatch.setattr(service_control.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(service_control, "_run", _launchd([4242], kicked))
    monkeypatch.setattr(service_control, "_wait_for_port", AsyncMock(return_value=True))

    result = await service_control.restart_gateway(health_check_timeout=0.6)

    assert len(kicked) == 1
    assert result.ok is False


@pytest.mark.asyncio
async def test_a_new_service_process_on_the_port_is_a_restart(monkeypatch) -> None:
    from flowly.integrations import service_control

    kicked: list[list[str]] = []
    monkeypatch.setattr(service_control.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(service_control, "_run", _launchd([4242, 4242, None, 5151], kicked))
    monkeypatch.setattr(service_control, "_wait_for_port", AsyncMock(return_value=True))

    result = await service_control.restart_gateway(health_check_timeout=5)

    assert result.ok is True
