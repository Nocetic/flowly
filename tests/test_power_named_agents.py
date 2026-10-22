"""A named agent's turn keeps a cloud machine awake (PROTOCOL.md B4).

Named agents run in their own processes under the profile host. Their turns
never reach the main agent loop's counters, so the gateway's power probes ask
the profile host, which tracks every run its runtimes report.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import flowly.profile as profiles
from flowly.power.monitor import JUST_WOKE_SECONDS, PowerMonitor, add_gateway_probes, named_agent_turns
from flowly.profile_host import ProfileHost


@pytest.fixture
def host(tmp_path, monkeypatch: pytest.MonkeyPatch) -> ProfileHost:
    default = tmp_path / ".flowly"
    monkeypatch.setattr(profiles, "_DEFAULT_HOME", default)
    monkeypatch.setattr(profiles, "_PROFILES_ROOT", default / "profiles")
    monkeypatch.setenv("FLOWLY_HOME", str(default))
    (default / "workspace").mkdir(parents=True)
    profiles.create_profile("writer", local_runtime=True)
    return ProfileHost()


def _runtime(name: str = "writer") -> SimpleNamespace:
    return SimpleNamespace(profile=name, ws=SimpleNamespace(closed=False), process=None, owned=True,
                           state="connected", active_runs=set(), pending={}, last_used_at=0.0, attention={},
                           capabilities=frozenset())


class Clock:
    def __init__(self):
        self.now = 1_000_000.0

    def wall(self) -> float:
        return self.now

    def mono(self) -> float:
        return self.now


def _monitor(gateway) -> PowerMonitor:
    clock = Clock()
    monitor = PowerMonitor(wall=clock.wall, monotonic=clock.mono)
    clock.now += JUST_WOKE_SECONDS + 1
    add_gateway_probes(monitor, gateway)
    return monitor


async def test_a_named_agents_turn_is_busy_until_it_ends(host):
    runtime = _runtime()
    host._runtimes["writer"] = runtime  # type: ignore[assignment]
    gateway = SimpleNamespace(profile_host=host, _display_sessions={})
    monitor = _monitor(gateway)
    assert monitor.status()["busy"] == []

    await host._handle_profile_event("writer", "chat", {"runId": "run-1", "sessionKey": "web:c1",
                                                          "state": "run_started"}, runtime=runtime)
    assert host.has_active_turns() is True
    status = monitor.status()
    assert status["idle"] is False and status["busy"] == ["turn"]
    assert await monitor.sleep() == {"ok": False, "busy": ["turn"]}
    assert monitor.sleeping is False

    await host._handle_profile_event("writer", "chat", {"runId": "run-1", "sessionKey": "web:c1",
                                                          "state": "final", "content": "done"}, runtime=runtime)
    assert host.has_active_turns() is False
    assert monitor.status()["busy"] == []
    assert (await monitor.sleep())["ok"] is True


async def test_an_internal_turn_between_agents_is_busy(host):
    host._broker_sessions[("writer", "desktop:profile-inbox:writer:default")] = "correlation-1"
    assert named_agent_turns(SimpleNamespace(profile_host=host)) is True
    host._broker_sessions.clear()
    assert named_agent_turns(SimpleNamespace(profile_host=host)) is False


def test_without_a_profile_host_nothing_is_busy():
    assert named_agent_turns(SimpleNamespace(profile_host=None)) is False
    assert named_agent_turns(SimpleNamespace()) is False


def test_the_gateway_probes_cover_screen_named_agents_and_maintenance(monkeypatch):
    import flowly.power.monitor as monitor_module

    monkeypatch.setattr(monitor_module, "environment_maintenance", lambda: True)
    gateway = SimpleNamespace(
        profile_host=SimpleNamespace(has_active_turns=lambda: True),
        _display_sessions={"s": SimpleNamespace(streams={object()})},
    )
    assert _monitor(gateway).status()["busy"] == ["turn", "screen", "maintenance"]
