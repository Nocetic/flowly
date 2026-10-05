"""An agent starts its installed screen through the same durable lifecycle as the panel."""

import asyncio
import sys
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from flowly.desktop_runtime import host_screen_run
from flowly.desktop_runtime.authority import ScreenError
from flowly.desktop_runtime.host_screen_run import start_profile_screen
from flowly.desktop_runtime.lifecycle import ScreenLifecycle
from flowly.gateway.display import IDLE_STOP_SECONDS


# The real durable lifecycle identifies its worker process through /proc.
pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux process identity (/proc)")

IDENTITY = {"hostId": "host", "profileId": "default", "botId": "bot"}


class Runtime:
    def __init__(self, directory, state=None):
        self.directory = directory
        self.state = state
        self.starts = []
        self.release = threading.Event()
        self.release.set()
        self.failure = None

    def status(self):
        return self.state

    def start(self, *, expected_generation, identity_guard):
        identity_guard()
        self.starts.append(expected_generation)
        self.release.wait(5)
        # Compare-and-start, as ScreenRuntime.start: a start observed against an
        # older generation never starts or replaces the current runtime.
        if expected_generation != (self.state["generation"] if self.state else None):
            raise ScreenError("STALE_GENERATION")
        if self.failure:
            raise ScreenError(self.failure)
        self.state = {"phase": "running", "generation": f"generation-{len(self.starts)}"}
        return self.state


@pytest.fixture
def host(tmp_path, monkeypatch):
    runtime = Runtime(tmp_path, {"phase": "stopped", "generation": "previous"})
    session = SimpleNamespace(runtime=runtime, identity={**IDENTITY, "screenId": "browser"})
    resolved = []

    def resolve(gateway, params, *, pinned=False):
        assert pinned and params == {"profileId": "default", "expectedHostId": "host", "expectedBotId": "bot"}
        resolved.append(params)
        return session

    monkeypatch.setattr("flowly.gateway.display._resolve", resolve)
    monkeypatch.setattr(host_screen_run, "_START_POLL_SECONDS", 0.01)
    gateway = Mock()
    gateway._host_screen_checkpoint_identity.return_value = dict(IDENTITY)
    return SimpleNamespace(gateway=gateway, runtime=runtime, lifecycle=ScreenLifecycle(runtime, IDENTITY, lambda: None))


async def test_the_agent_start_is_a_recorded_lifecycle_operation_from_the_observed_generation(host):
    await start_profile_screen(host.gateway)
    assert host.runtime.starts == ["previous"]
    assert host.runtime.state["phase"] == "running"


async def test_a_start_already_in_progress_is_awaited_not_duplicated(host):
    host.runtime.release.clear()
    host.lifecycle.submit("start", "previous", "panel-start")
    agent = asyncio.create_task(start_profile_screen(host.gateway))
    await asyncio.sleep(0.1)
    assert not agent.done()
    host.runtime.release.set()
    await asyncio.wait_for(agent, 5)
    # A late agent start against the observed generation may reach the runtime,
    # which refuses it; only the panel's start ran.
    assert host.runtime.state == {"phase": "running", "generation": "generation-1"}
    assert host.lifecycle.status("panel-start")["state"] == "succeeded"


async def test_an_agent_start_refused_as_stale_observes_again_instead_of_failing(host, monkeypatch):
    # The panel's start completes between the agent's observation and its submit.
    real = host.runtime.start

    def raced(**kwargs):
        if not host.runtime.starts:
            host.runtime.state = {"phase": "stopped", "generation": "panel-started"}
        return real(**kwargs)

    host.runtime.start = raced
    host.runtime.state = {"phase": "stopped", "generation": "previous"}
    await start_profile_screen(host.gateway)
    assert host.runtime.starts == ["previous", "panel-started"]
    assert host.runtime.state["phase"] == "running"


async def test_a_failed_start_reports_the_lifecycle_code(host):
    host.runtime.failure = "INSUFFICIENT_RESOURCES"
    with pytest.raises(ScreenError, match="INSUFFICIENT_RESOURCES"):
        await start_profile_screen(host.gateway)
    assert host.runtime.starts == ["previous"]


async def test_a_running_screen_is_not_started_again(host):
    host.runtime.state = {"phase": "running", "generation": "current"}
    await start_profile_screen(host.gateway)
    assert host.runtime.starts == []


async def test_a_start_that_never_finishes_times_out(host, monkeypatch):
    monkeypatch.setattr(host_screen_run, "START_TIMEOUT_SECONDS", 0.2)
    host.runtime.release.clear()
    with pytest.raises(ScreenError, match="RUNTIME_START_TIMEOUT"):
        await start_profile_screen(host.gateway)
    host.runtime.release.set()


def test_an_unused_screen_stops_after_twenty_minutes():
    assert IDLE_STOP_SECONDS == 20 * 60
