"""A running profile screen is its runs' browser; nothing selects it per message."""

import json
from unittest.mock import AsyncMock, Mock

import pytest

from flowly.agent.tools.browser_tab import BrowserTabTool
from flowly.desktop_runtime.authority import ScreenAuthority, ScreenError
from flowly.desktop_runtime.browser_provider import HostBrowserProvider
from flowly.desktop_runtime.handoff import RunHandoffs
from flowly.desktop_runtime.host_screen_run import (
    HOST_SCREEN_RUN,
    HostScreenRun,
    bound_screen_run,
    current_screen_run,
    host_screen_run_scope,
)
from flowly.gateway.server import _BROWSER_RUN_BINDING, GatewayServer


@pytest.fixture(autouse=True)
def _profile_screens_run_on_linux(monkeypatch):
    """Routing is Linux-only; the screen here is a fake, so pin the platform."""
    monkeypatch.setattr("flowly.desktop_runtime.host_screen_run.sys.platform", "linux")


class Socket:
    closed = False

    def __init__(self):
        self.messages = []

    async def send_json(self, value):
        self.messages.append(value)


@pytest.fixture
def screen_gateway(tmp_path, monkeypatch):
    owner = {"pid": 42, "start": "1", "boot": "test"}
    monkeypatch.setattr("flowly.desktop_runtime.handoff.process_identity", lambda _: owner)
    monkeypatch.setattr("flowly.desktop_runtime.handoff.is_alive", lambda _: True)
    authority = ScreenAuthority(tmp_path)
    authority.start("generation")
    runtime = Mock(directory=tmp_path, authority=authority, status=lambda: {"phase": "running", "generation": "generation"})
    provider = HostBrowserProvider(runtime)
    provider._connect = AsyncMock()
    provider._dispatch = AsyncMock(return_value={"success": True, "text": "remote page"})
    gateway = GatewayServer(host="0.0.0.0", port=0, auth_token="test-owner-token")
    socket = Socket()
    gateway._ws_clients["viewer"] = socket
    gateway._host_screen_checkpoint_identity = lambda: {"hostId": "host", "profileId": "default", "botId": "bot"}
    gateway._host_screen_browser = provider
    gateway._host_screen_identity = {"hostId": "host", "profileId": "default", "screenId": "browser"}
    return gateway, socket, provider


def desktop_browser(gateway, socket):
    """The Desktop's own browser, offered with the message as before."""
    registered = gateway._register_browser_provider("viewer", {"providerId": "local-browser", "type": "embedded"})
    binding, error = gateway._browser_binding_for_chat("viewer", socket, {
        "providerId": "local-browser", "registrationId": registered["provider"]["registrationId"]})
    assert error is None
    return binding


def test_client_cannot_register_as_host_screen_provider(screen_gateway):
    gateway, *_ = screen_gateway
    with pytest.raises(ValueError, match="reserved"):
        gateway._register_browser_provider("viewer", {"providerId": "host-desktop-browser"})


@pytest.mark.parametrize("access", [
    {"providerId": "host-desktop-browser", "hostId": "host", "botId": "bot", "profileId": "default",
     "screenId": "browser", "generation": "generation"},
    {"providerId": "host-desktop-browser", "registrationId": "anything"},
])
def test_a_client_cannot_ask_for_the_screen_by_message(screen_gateway, access):
    gateway, socket, provider = screen_gateway
    binding, error = gateway._browser_binding_for_chat("viewer", socket, access)
    assert error["code"] in {"INVALID_REQUEST", "BROWSER_PROVIDER_UNAVAILABLE"}
    assert binding.provider_id is None
    assert provider.runtime.authority.snapshot()["runId"] is None


async def test_the_running_screen_wins_over_the_desktop_browser_and_claims_on_first_use(screen_gateway):
    gateway, socket, provider = screen_gateway
    token = _BROWSER_RUN_BINDING.set(desktop_browser(gateway, socket))
    try:
        async with host_screen_run_scope(gateway, "origin-session", "origin-run") as run:
            # Entering a run claims nothing; runs that never browse never contend.
            assert provider.runtime.authority.snapshot()["runId"] is None
            assert gateway.has_browser_provider()
            assert not gateway.has_browser_provider("local-browser")
            result = await gateway.send_browser_tool_request("read", "get_page_text", {})
            assert result["text"] == "remote page"
            assert provider.runtime.authority.snapshot()["runId"] == "origin-run"
            foreign = await gateway.send_browser_tool_request("foreign", "click", {}, "local-browser")
            assert foreign["error_code"] == "BROWSER_PROVIDER_NOT_OWNED"
            run.succeeded = True
    finally:
        _BROWSER_RUN_BINDING.reset(token)
    assert not any(message.get("type") == "tool_request" for message in socket.messages)
    assert provider.runtime.authority.snapshot()["runId"] is None
    assert RunHandoffs(provider.runtime.directory, provider.runtime.authority).status()["state"] == "finished"
    assert HOST_SCREEN_RUN.get() is None


def installed(monkeypatch, supported: bool):
    monkeypatch.setattr("flowly.desktop_runtime.launcher.ScreenRuntime.capabilities",
                        staticmethod(lambda: {"supported": supported, "reason": None if supported else "DEPENDENCIES_MISSING",
                                              "missing": []}))


async def test_a_host_without_screen_components_keeps_the_desktop_browser_for_the_whole_run(screen_gateway, monkeypatch):
    gateway, socket, provider = screen_gateway
    installed(monkeypatch, False)
    provider.runtime.status = lambda: None
    started = AsyncMock()
    monkeypatch.setattr("flowly.desktop_runtime.host_screen_run.start_profile_screen", started)
    token = _BROWSER_RUN_BINDING.set(desktop_browser(gateway, socket))
    try:
        async with host_screen_run_scope(gateway, "session", "run") as run:
            assert not run.uses_screen()
            assert gateway.has_browser_provider("local-browser")
            assert await run.bind() is None
            # Components installed mid-task do not switch this run's browser.
            installed(monkeypatch, True)
            assert not run.uses_screen()
            assert gateway.has_browser_provider("local-browser")
    finally:
        _BROWSER_RUN_BINDING.reset(token)
    started.assert_not_awaited()
    provider._dispatch.assert_not_awaited()


async def test_a_stopped_installed_screen_is_started_for_the_first_browser_action(screen_gateway, monkeypatch):
    gateway, socket, provider = screen_gateway
    installed(monkeypatch, True)
    provider.runtime.status = lambda: {"phase": "stopped", "generation": "previous"}

    async def start(target):
        assert target is gateway
        provider.runtime.status = lambda: {"phase": "running", "generation": "generation"}

    started = AsyncMock(side_effect=start)
    monkeypatch.setattr("flowly.desktop_runtime.host_screen_run.start_profile_screen", started)
    token = _BROWSER_RUN_BINDING.set(desktop_browser(gateway, socket))
    try:
        async with host_screen_run_scope(gateway, "session", "run") as run:
            # An installed screen is this bot's browser even while stopped,
            # and entering the run starts nothing.
            assert run.uses_screen() and gateway.has_browser_provider()
            assert not gateway.has_browser_provider("local-browser")
            started.assert_not_awaited()
            assert (await gateway.send_browser_tool_request("read", "get_page_text", {}))["text"] == "remote page"
            assert provider.runtime.authority.snapshot()["runId"] == "run"
            run.succeeded = True
    finally:
        _BROWSER_RUN_BINDING.reset(token)
    started.assert_awaited_once()
    assert not any(message.get("type") == "tool_request" for message in socket.messages)


async def test_a_failed_start_is_reported_and_the_next_action_tries_again(screen_gateway, monkeypatch):
    gateway, _, provider = screen_gateway
    installed(monkeypatch, True)
    provider.runtime.status = lambda: None
    attempts = []

    async def start(_):
        attempts.append(1)
        if len(attempts) == 1:
            raise ScreenError("INSUFFICIENT_RESOURCES")
        provider.runtime.status = lambda: {"phase": "running", "generation": "generation"}

    monkeypatch.setattr("flowly.desktop_runtime.host_screen_run.start_profile_screen", start)
    async with host_screen_run_scope(gateway, "session", "run") as run:
        refused = await gateway.send_browser_tool_request("read", "get_page_text", {})
        assert refused["error_code"] == "INSUFFICIENT_RESOURCES"
        assert "enough free memory" in refused["error"]
        assert run.uses_screen() and bound_screen_run() is None
        assert (await gateway.send_browser_tool_request("again", "get_page_text", {}))["text"] == "remote page"
        run.succeeded = True
    assert len(attempts) == 2


async def test_parallel_runs_contend_only_when_both_use_the_browser(screen_gateway):
    gateway, _, provider = screen_gateway

    async def browse(run_id):
        async with host_screen_run_scope(gateway, f"session-{run_id}", run_id) as run:
            result = await gateway.send_browser_tool_request(f"{run_id}-read", "get_page_text", {})
            run.succeeded = True
            return result

    async with host_screen_run_scope(gateway, "chatting", "chat-run"):
        # A parallel conversation that never touches the browser does not block it.
        assert (await browse("browsing-run"))["text"] == "remote page"
        async with host_screen_run_scope(gateway, "first", "first-run"):
            assert (await gateway.send_browser_tool_request("first-read", "get_page_text", {}))["text"] == "remote page"
            busy = await browse("second-run")
            assert busy["error_code"] == "SCREEN_BUSY"
            assert "Another task" in busy["error"]
            assert provider.runtime.authority.snapshot()["runId"] == "first-run"
    assert provider.runtime.authority.snapshot()["runId"] is None


async def test_a_person_holding_control_is_named_and_the_next_action_claims_the_screen(screen_gateway):
    gateway, _, provider = screen_gateway
    authority = provider.runtime.authority
    async with host_screen_run_scope(gateway, "session", "run") as run:
        human = authority.acquire("human", "generation", authority.snapshot()["epoch"])
        refused = await gateway.send_browser_tool_request("read", "get_page_text", {})
        assert refused["error_code"] == "HUMAN_HAS_CONTROL"
        assert "give control back" in refused["error"]
        assert bound_screen_run() is None and run.uses_screen()
        authority.release("human", "generation", human["epoch"])
        assert (await gateway.send_browser_tool_request("again", "get_page_text", {}))["text"] == "remote page"
        run.succeeded = True
    assert authority.snapshot()["runId"] is None


async def test_screen_bound_capture_cannot_fall_back_to_physical_display(screen_gateway, monkeypatch):
    from flowly.agent.tools.computer import ComputerTool
    from flowly.agent.tools.screenshot import ScreenshotTool

    gateway, _, provider = screen_gateway
    screenshot = ScreenshotTool()
    # Existing native/Electron capture implementations must not run.
    monkeypatch.setattr(screenshot, "_capture_via_electron_sync", Mock(side_effect=AssertionError("physical capture")))
    for name in ("_capture_linux", "_capture_macos", "_capture_windows"):
        monkeypatch.setattr(screenshot, name, AsyncMock(side_effect=AssertionError("physical capture")))
    async with host_screen_run_scope(gateway, "origin-session", "run"):
        assert (await gateway.send_browser_tool_request("read", "get_page_text", {}))["success"]
        human = provider.runtime.authority.acquire("human", "generation", 1)
        capture = json.loads(await screenshot.execute())
        computer = json.loads(await ComputerTool().execute("click", x=10, y=10))
        provider.runtime.authority.release("human", "generation", human["epoch"])
    assert capture["error_code"] == "SCREEN_HANDOFF_REQUIRED"
    assert computer["error_code"] == "BROWSER_SCOPE_ONLY"
    provider._dispatch.assert_awaited_once_with("get_page_text", {})


async def test_browser_tool_discards_stale_pixels_before_saving_or_rendering(monkeypatch):
    gateway = Mock(has_browser_provider=lambda: True, validate_browser_tool_result=lambda result: False)
    gateway.send_browser_tool_request = AsyncMock(return_value={
        "screenshot": "data:image/jpeg;base64,cHJpdmF0ZQ==",
        "_screenObservation": {"generation": "generation", "epoch": 1},
    })
    tool = BrowserTabTool(gateway)
    monkeypatch.setattr(tool, "_save_screenshot_to_file", Mock(side_effect=AssertionError("must not save")))
    result = json.loads(await tool.execute("screenshot"))
    assert result["error_code"] == "STALE_OBSERVATION"
    assert "screenshot" not in result and "image_data_url" not in result


async def test_host_screenshot_preserves_reply_attachment_contract(tmp_path, monkeypatch):
    from flowly.agent.reply_media import extract_reply_media
    from flowly.agent.tools.screenshot import ScreenshotTool
    from flowly.desktop_runtime.context import ScreenRunBinding

    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path / "home"))
    saved = tmp_path / "screen.jpg"
    saved.write_bytes(b"generated test image")
    monkeypatch.setattr(BrowserTabTool, "_save_screenshot_to_file", lambda result: {"path": str(saved)})
    provider = Mock(request=AsyncMock(return_value={"screenshot": "data:image/jpeg;base64,dGVzdA=="}),
                    desktop_screenshot=AsyncMock(return_value={"screenshot": "data:image/png;base64,dGVzdA=="}),
                    observation_valid=lambda result: True)
    run = HostScreenRun(Mock(), "session", "run")
    run.binding, run.decided = ScreenRunBinding(provider, "generation", "run"), True
    token = HOST_SCREEN_RUN.set(run)
    try:
        result = await ScreenshotTool().execute()
        paths, summary = extract_reply_media(result)
        assert paths == [str(saved)]
        assert "attached" in summary and "whole desktop" in summary
        assert "data:image" not in result
        # The whole desktop, not the browser tab.
        provider.desktop_screenshot.assert_awaited_once()
        provider.request.assert_not_awaited()
    finally:
        HOST_SCREEN_RUN.reset(token)


async def test_a_screen_without_the_desktop_picture_gives_its_browser_tab(tmp_path, monkeypatch):
    from flowly.agent.reply_media import extract_reply_media
    from flowly.agent.tools.screenshot import ScreenshotTool
    from flowly.desktop_runtime.context import ScreenRunBinding

    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path / "home"))
    saved = tmp_path / "screen.jpg"
    saved.write_bytes(b"generated test image")
    monkeypatch.setattr(BrowserTabTool, "_save_screenshot_to_file", lambda result: {"path": str(saved)})
    provider = Mock(request=AsyncMock(return_value={"screenshot": "data:image/jpeg;base64,dGVzdA=="}),
                    desktop_screenshot=AsyncMock(return_value={"error": "x", "error_code": "SCREEN_STILL_UNAVAILABLE"}),
                    observation_valid=lambda result: True)
    run = HostScreenRun(Mock(), "desktop:chat", "run")
    run.binding, run.decided = ScreenRunBinding(provider, "generation", "run", owner="desktop:chat"), True
    token = HOST_SCREEN_RUN.set(run)
    try:
        paths, summary = extract_reply_media(await ScreenshotTool().execute())
    finally:
        HOST_SCREEN_RUN.reset(token)
    assert paths == [str(saved)] and "Browser screenshot" in summary
    assert provider.request.await_args.kwargs["owner"] == "desktop:chat"


async def test_no_desktop_picture_while_the_person_has_control(tmp_path, monkeypatch):
    from flowly.agent.tools.screenshot import ScreenshotTool
    from flowly.desktop_runtime.context import ScreenRunBinding

    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path / "home"))
    provider = Mock(request=AsyncMock(), observation_valid=lambda result: True,
                    desktop_screenshot=AsyncMock(return_value={"error": "x", "error_code": "HUMAN_HAS_CONTROL"}))
    run = HostScreenRun(Mock(), "desktop:chat", "run")
    run.binding, run.decided = ScreenRunBinding(provider, "generation", "run"), True
    token = HOST_SCREEN_RUN.set(run)
    try:
        result = json.loads(await ScreenshotTool().execute())
    finally:
        HOST_SCREEN_RUN.reset(token)
    assert result["error_code"] == "HUMAN_HAS_CONTROL"
    provider.request.assert_not_awaited()


async def test_a_replaced_profile_identity_claims_nothing(screen_gateway):
    gateway, _, provider = screen_gateway

    def replaced():
        raise ScreenError("PROFILE_IDENTITY_CHANGED")

    gateway._host_screen_checkpoint_identity = replaced
    async with host_screen_run_scope(gateway, "session", "run"):
        result = await gateway.send_browser_tool_request("read", "get_page_text", {})
    assert result["error_code"] == "PROFILE_IDENTITY_CHANGED"
    assert provider.runtime.authority.snapshot()["runId"] is None
    provider._dispatch.assert_not_awaited()


async def test_hosts_without_profile_screens_keep_their_own_browser(screen_gateway, monkeypatch):
    gateway, *_ = screen_gateway
    monkeypatch.setattr("flowly.desktop_runtime.host_screen_run.sys.platform", "darwin")
    async with host_screen_run_scope(gateway, "session", "run") as run:
        assert run is None and current_screen_run() is None
        assert not gateway.has_browser_provider("host-desktop-browser")


async def test_the_browser_tool_waits_as_long_as_the_screen_bounds_an_action(screen_gateway, monkeypatch):
    """The tool's own 15 s screenshot limit fired before the screen's 20 s bound:
    the action stayed admitted behind the tool's back and every next call was busy."""
    import asyncio

    from flowly.agent.tools import browser_tab as module

    gateway, _, _ = screen_gateway
    waits = []
    real = asyncio.wait_for

    async def record(awaitable, timeout):
        waits.append(timeout)
        return await real(awaitable, timeout)

    monkeypatch.setattr(module.asyncio, "wait_for", record)
    async with host_screen_run_scope(gateway, "desktop:chat", "run"):
        await BrowserTabTool(gateway).execute("screenshot")
    # The tool's own wait (first) covers the screen's 20 s bound (second).
    assert len(waits) >= 2 and waits[0] >= 25 and waits[0] > waits[1]
