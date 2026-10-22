"""D10: the agent's actions on its screen's whole desktop.

Each guard here is the screen's own: the supervisor refuses while the person
has control, whoever reaches its socket; nothing a model sends reaches a
shell; an action planned on a picture from before a takeover is refused; and
the Mac computer tool is unchanged.
"""

from __future__ import annotations

import json
from unittest.mock import Mock

import pytest

from flowly.desktop_runtime import desktop_act
from flowly.desktop_runtime.authority import ScreenAuthority, ScreenError
from flowly.desktop_runtime.browser_provider import HostBrowserProvider
from flowly.desktop_runtime.desktop_act import ActService, act_request, request_act
from flowly.desktop_runtime.execution import ScreenExecution


# ── validation: fixed argument lists, no shell ────────────────────────────


def test_actions_become_fixed_xdotool_argument_lists():
    assert act_request("click", {"x": 10, "y": 20, "button": "right", "count": 2}) == {
        "kind": "xdotool", "args": ["mousemove", "--sync", "10", "20", "click", "--repeat", "2", "3"]}
    assert act_request("drag", {"x": 1, "y": 2, "to_x": 3, "to_y": 4})["args"] == [
        "mousemove", "--sync", "1", "2", "mousedown", "1", "mousemove", "--sync", "3", "4", "mouseup", "1"]
    assert act_request("scroll", {"x": 5, "y": 6, "direction": "down"})["args"][-3:] == ["--repeat", "3", "5"]
    # Text is one argument after "--": a model cannot add xdotool options or commands.
    assert act_request("type", {"text": "--help; rm -rf / && çğış"})["args"] == [
        "type", "--clearmodifiers", "--delay", "12", "--", "--help; rm -rf / && çğış"]
    assert act_request("key", {"key": "ctrl+c"})["args"] == ["key", "--clearmodifiers", "ctrl+c"]
    assert act_request("open_app", {"name": "terminal"}) == {
        "kind": "launch", "argv": ["/usr/bin/xfce4-terminal", "--disable-server", "--command=/bin/bash"]}


@pytest.mark.parametrize("action, params", [
    ("click", {"x": "10", "y": 20}),
    ("click", {"x": True, "y": 20}),
    ("click", {"x": -1, "y": 20}),
    ("click", {"x": 1, "y": 2, "count": 4}),
    ("click", {"x": 1, "y": 2, "button": "4"}),
    ("key", {"key": "ctrl+c; reboot"}),
    ("key", {"key": "--window"}),
    ("type", {"text": ""}),
    ("type", {"text": "x" * 4001}),
    ("open_app", {"name": "/bin/sh"}),
    ("exec", {"command": "id"}),
    ("scroll", {"x": 1, "y": 2, "direction": "sideways"}),
])
def test_anything_outside_the_schema_is_refused(action, params):
    with pytest.raises(ScreenError) as exc:
        act_request(action, params)
    assert exc.value.code == "INVALID_PARAMS"


# ── the supervisor's service ───────────────────────────────────────────────


@pytest.fixture
def sockets():
    """A short directory: macOS caps Unix socket paths near 104 bytes."""
    import shutil
    import tempfile
    from pathlib import Path

    path = Path(tempfile.mkdtemp(prefix="act", dir="/tmp"))
    yield path
    shutil.rmtree(path, ignore_errors=True)


def _service(tmp_path, *, allowed=True, output=b"ok\n"):
    ran: list[dict] = []

    def run(request: str) -> bytes:
        ran.append(json.loads(request))
        return output

    return ActService(tmp_path, run, allowed=lambda: allowed), ran


def test_the_service_runs_one_action_and_the_gateway_gets_its_answer(sockets):
    service, ran = _service(sockets)
    try:
        request_act(sockets / "act.sock", "key", {"key": "Return"})
        assert ran == [{"kind": "xdotool", "args": ["key", "--clearmodifiers", "Return"]}]
    finally:
        service.close()


def test_the_service_itself_does_nothing_while_the_person_has_control(sockets):
    """The gateway checks too, but the socket must not be a way around it."""
    service, ran = _service(sockets, allowed=False)
    try:
        assert service.handle({"action": "click", "params": {"x": 1, "y": 1}}) == {
            "ok": False, "error": "HUMAN_HAS_CONTROL"}
        with pytest.raises(ScreenError) as exc:
            request_act(sockets / "act.sock", "click", {"x": 1, "y": 1})
        assert exc.value.code == "HUMAN_HAS_CONTROL"
        assert ran == []
    finally:
        service.close()


def test_a_failed_action_is_an_error_and_bad_input_never_runs(sockets):
    service, ran = _service(sockets, output=b"failed\n")
    try:
        assert service.handle({"action": "key", "params": {"key": "Return"}})["error"] == "SCREEN_ACT_FAILED"
        assert service.handle({"action": "key", "params": {"key": "a b"}})["error"] == "INVALID_PARAMS"
        assert len(ran) == 1
    finally:
        service.close()


def test_a_screen_started_before_the_service_cannot_act(tmp_path):
    with pytest.raises(ScreenError) as exc:
        request_act(tmp_path / "act.sock", "key", {"key": "Return"})
    assert exc.value.code == "SCREEN_ACT_UNAVAILABLE"


# ── the provider: one of the run's screen actions ─────────────────────────


@pytest.fixture
def provider(tmp_path, monkeypatch):
    authority = ScreenAuthority(tmp_path)
    authority.start("generation")
    runtime = Mock(authority=authority, socket_directory=tmp_path)
    runtime.status.return_value = {"phase": "running", "generation": "generation"}
    asked = []
    monkeypatch.setattr(desktop_act, "request_act", lambda path, action, params: asked.append((action, params)))
    return HostBrowserProvider(runtime, ScreenExecution(authority)), asked


async def test_the_agent_acts_on_the_desktop_as_one_of_its_actions(provider):
    instance, asked = provider
    epoch = instance.runtime.authority.snapshot()["epoch"]
    result = await instance.desktop_act("run", "a", "generation", "click", {"x": 3, "y": 4}, observed_epoch=epoch)
    assert result["success"] is True
    assert asked == [("click", {"x": 3, "y": 4})]


async def test_no_desktop_action_while_the_person_has_control(provider):
    instance, asked = provider
    instance.runtime.authority.acquire("human", "generation", 1)
    result = await instance.desktop_act("run", "a", "generation", "key", {"key": "Return"})
    assert result["error_code"] == "HUMAN_HAS_CONTROL"
    assert asked == []


async def test_an_action_planned_before_a_takeover_must_look_again(provider):
    instance, asked = provider
    authority = instance.runtime.authority
    before = authority.snapshot()["epoch"]
    authority.acquire("human", "generation", before)
    authority.release("human", "generation", authority.snapshot()["epoch"])
    result = await instance.desktop_act("run", "a", "generation", "click", {"x": 3, "y": 4}, observed_epoch=before)
    assert result["error_code"] == "STALE_OBSERVATION"
    assert asked == []


# ── the computer tool ──────────────────────────────────────────────────────


def test_the_cloud_screen_gets_the_desktop_schema_and_the_mac_keeps_its_own(monkeypatch):
    from flowly.agent.tools import computer

    tool = computer.ComputerTool()
    monkeypatch.setattr(computer, "_PLATFORM", "linux")
    monkeypatch.setenv("FLY_APP_NAME", "flowly-cloud-abc")
    assert tool.parameters is computer.SCREEN_PARAMETERS
    assert "xfce" in tool.description
    monkeypatch.setattr(computer, "_PLATFORM", "darwin")
    assert tool.parameters is not computer.SCREEN_PARAMETERS
    assert "AX" in tool.description


async def test_a_pointed_action_needs_a_picture_first(monkeypatch):
    from flowly.agent.tools import computer

    calls = []

    class Provider:
        async def desktop_act(self, *args, **kwargs):
            calls.append((args, kwargs))
            return {"success": True}

    class Run:
        def uses_screen(self):
            return True

        async def bind(self):
            return Mock(provider=Provider(), run_id="fresh-run", generation="g")

    monkeypatch.setattr("flowly.desktop_runtime.host_screen_run.current_screen_run", lambda: Run())
    tool = computer.ComputerTool()
    refused = json.loads(await tool.execute(action="click", x=1, y=2))
    assert refused["error_code"] == "OBSERVATION_REQUIRED"
    assert calls == []
    typed = json.loads(await tool.execute(action="key", key="Return"))
    assert typed == {"success": True}
    assert calls[0][0][3:] == ("key", {"key": "Return"})
