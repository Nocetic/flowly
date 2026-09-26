"""Background shell lifetime remains busy after its creating agent turn ends."""

import asyncio
import os
import shlex
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from flowly.desktop_runtime import installation_gate as gate
from flowly.desktop_runtime.authority import ScreenError
from flowly.exec.process_registry import ProcessRegistry
from tests.test_screen_install_jobs import fixture as job_fixture

installation_job = job_fixture
pytestmark = pytest.mark.skipif(os.name != "posix", reason="real POSIX process leases")


@pytest.fixture(autouse=True)
def linux_gate(monkeypatch, installation_job):
    monkeypatch.setattr(gate, "sys", SimpleNamespace(platform="linux", prefix=sys.prefix))


def assert_busy():
    with pytest.raises(ScreenError, match="INSTALL_BUSY"):
        with gate.environment_activity(exclusive=True):
            pytest.fail("maintenance admitted while a command was running")


async def stop(session):
    if session.process.returncode is None:
        os.killpg(session.pid, 9)
    await asyncio.wait_for(asyncio.shield(session._reader_task), 5)


async def test_running_command_blocks_installation_until_exit():
    registry = ProcessRegistry()
    session = await registry.spawn("sleep 30", notify_on_complete=False)
    try:
        assert_busy()
        assert await registry.get(session.id) is session
    finally:
        await stop(session)
    assert session.exited
    assert session._environment_activity is None
    with gate.environment_activity(exclusive=True):
        pass


async def test_maintenance_rejects_before_any_subprocess(monkeypatch):
    spawn = AsyncMock()
    monkeypatch.setattr(asyncio, "create_subprocess_shell", spawn)
    with gate.environment_activity(exclusive=True):
        with pytest.raises(ScreenError, match="INSTALL_BUSY"):
            await ProcessRegistry().spawn("never execute")
    spawn.assert_not_called()


async def test_failed_creation_releases_admission(monkeypatch):
    monkeypatch.setattr(asyncio, "create_subprocess_shell", AsyncMock(side_effect=OSError("test")))
    registry = ProcessRegistry()
    with pytest.raises(OSError, match="test"):
        await registry.spawn("never execute")
    assert not registry._pending_spawns
    with gate.environment_activity(exclusive=True):
        pass


async def test_cancelled_caller_still_registers_and_tracks_its_child(monkeypatch):
    original = asyncio.create_subprocess_shell
    entered, release = asyncio.Event(), asyncio.Event()

    async def delayed(*args, **kwargs):
        entered.set()
        await release.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_shell", delayed)
    registry = ProcessRegistry()
    caller = asyncio.create_task(registry.spawn("sleep 30", notify_on_complete=False))
    await asyncio.wait_for(entered.wait(), 2)
    caller.cancel()
    await asyncio.gather(caller, return_exceptions=True)
    assert_busy()
    pending = list(registry._pending_spawns)
    assert len(pending) == 1
    release.set()
    session, = await asyncio.wait_for(asyncio.gather(*pending), 5)
    try:
        assert await registry.get(session.id) is session
        assert_busy()
    finally:
        await stop(session)
    with gate.environment_activity(exclusive=True):
        pass


async def test_child_inherits_kernel_lease_even_without_parent_copy():
    registry = ProcessRegistry()
    command = "exec " + shlex.quote(sys.executable) + " -c 'import time; time.sleep(30)'"
    session = await registry.spawn(command, notify_on_complete=False)
    try:
        # Model loss of the gateway-owned descriptor after successful exec.
        # This is a real subprocess and flock, not a mocked alive PID.
        session._environment_activity.close()
        session._environment_activity = None
        assert_busy()
    finally:
        await stop(session)
    with gate.environment_activity(exclusive=True):
        pass


async def test_non_linux_spawn_does_not_pass_posix_descriptors(monkeypatch):
    monkeypatch.setattr(gate, "sys", SimpleNamespace(platform="win32", prefix=sys.prefix))
    original = asyncio.create_subprocess_shell
    seen = {}

    async def capture(*args, **kwargs):
        seen.update(kwargs)
        return await original(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_shell", capture)
    session = await ProcessRegistry().spawn("true", notify_on_complete=False)
    await asyncio.wait_for(session._reader_task, 5)
    assert "pass_fds" not in seen


@pytest.mark.skipif(sys.platform != "linux", reason="actual Linux gateway crash")
async def test_gateway_crash_leaves_running_child_lease(tmp_path):
    import json
    import subprocess

    ready = tmp_path / "ready"
    info = tmp_path / "child.json"
    child_source = "from pathlib import Path; import time; Path(" + repr(str(ready)) + ").touch(); time.sleep(30)"
    command = "exec " + shlex.quote(sys.executable) + " -c " + shlex.quote(child_source)
    source = '''
import asyncio, json, os, sys
from pathlib import Path
from flowly.desktop_runtime import install_jobs
from flowly.exec.process_registry import ProcessRegistry
install_jobs.state_root = lambda: Path(sys.argv[4])
async def main():
    session = await ProcessRegistry().spawn(sys.argv[1], notify_on_complete=False)
    Path(sys.argv[3]).write_text(json.dumps({"pid": session.pid}))
    for _ in range(300):
        if Path(sys.argv[2]).exists():
            os._exit(0)
        await asyncio.sleep(.01)
    os._exit(2)
asyncio.run(main())
'''
    pid = None
    try:
        result = await asyncio.to_thread(
            subprocess.run, [sys.executable, "-c", source, command, str(ready), str(info), str(tmp_path / "state")],
            capture_output=True, timeout=8,
        )
        if info.exists():
            pid = json.loads(info.read_text())["pid"]
        assert result.returncode == 0, result.stderr.decode()
        assert pid and ready.exists()
        assert_busy()
    finally:
        if pid:
            try:
                os.killpg(pid, 9)
            except ProcessLookupError:
                pass
    # The orphan must close its own kernel descriptor before another writer
    # enters. Waiting for that release does not depend on gateway cleanup.
    for _ in range(200):
        try:
            with gate.environment_activity(exclusive=True):
                break
        except ScreenError:
            await asyncio.sleep(.01)
    else:
        pytest.fail("child exit did not release inherited activity")
