"""A host-screen grant cannot drift into a local or another profile browser."""

import json
from unittest.mock import AsyncMock, Mock

import pytest

from flowly.agent.tools.browser_tab import BrowserTabTool
from flowly.desktop_runtime.authority import ScreenAuthority, ScreenError
from flowly.desktop_runtime.browser_provider import HostBrowserProvider
from flowly.desktop_runtime.context import SCREEN_RUN_BINDING
from flowly.gateway.server import GatewayServer


class Socket:
    closed = False

    def __init__(self):
        self.messages = []

    async def send_json(self, value):
        self.messages.append(value)


@pytest.fixture
def screen_gateway(tmp_path):
    authority = ScreenAuthority(tmp_path)
    authority.start("generation")
    runtime = Mock(authority=authority, status=lambda: {"phase": "running", "generation": "generation"})
    provider = HostBrowserProvider(runtime)
    provider._connect = AsyncMock()
    provider._dispatch = AsyncMock(return_value={"success": True, "text": "remote page"})
    gateway = GatewayServer(host="0.0.0.0", port=0, auth_token="test-owner-token")
    socket = Socket()
    gateway._ws_clients["viewer"] = socket
    gateway._host_screen_browser = provider
    gateway._host_screen_identity = {"hostId": "host", "profileId": "default", "screenId": "browser"}
    access = {**gateway._host_screen_identity, "providerId": "host-desktop-browser", "generation": "generation"}
    return gateway, socket, provider, access


@pytest.mark.parametrize(("field", "value", "code"), [
    ("hostId", "other-host", "BROWSER_PROVIDER_NOT_OWNED"),
    ("profileId", "research", "BROWSER_PROVIDER_NOT_OWNED"),
    ("screenId", "other-screen", "BROWSER_PROVIDER_NOT_OWNED"),
    ("generation", "old", "STALE_GENERATION"),
])
def test_binding_rejects_target_drift(screen_gateway, field, value, code):
    gateway, socket, provider, access = screen_gateway
    _, error = gateway._browser_binding_for_chat("viewer", socket, {**access, field: value})
    assert error["code"] == code
    provider._connect.assert_not_awaited()


def test_old_socket_cannot_grant_new_screen_access(screen_gateway):
    gateway, socket, _, access = screen_gateway
    gateway._ws_clients["viewer"] = Socket()
    _, error = gateway._browser_binding_for_chat("viewer", socket, access)
    assert error["code"] == "BROWSER_ACCESS_NOT_GRANTED"


def test_client_cannot_register_as_host_screen_provider(screen_gateway):
    gateway, *_ = screen_gateway
    with pytest.raises(ValueError, match="reserved"):
        gateway._register_browser_provider("viewer", {"providerId": "host-desktop-browser"})


def test_corrupt_runtime_returns_a_typed_binding_error(screen_gateway):
    gateway, socket, provider, access = screen_gateway
    provider.runtime.status = Mock(side_effect=ScreenError("RUNTIME_STATE_INVALID"))
    _, error = gateway._browser_binding_for_chat("viewer", socket, access)
    assert error["code"] == "CONTROL_UNAVAILABLE"


async def test_screen_run_uses_original_target_and_releases_only_its_run_lease(screen_gateway):
    gateway, socket, provider, access = screen_gateway
    gateway._register_browser_provider("viewer", {"providerId": "local-browser", "type": "embedded"})
    binding, error = gateway._browser_binding_for_chat("viewer", socket, access)
    assert error is None
    seen = {}

    async def chat(*args):
        assert SCREEN_RUN_BINDING.get().run_id == "origin-run"
        assert gateway.has_browser_provider()
        assert not gateway.has_browser_provider("local-browser")
        seen["result"] = await gateway.send_browser_tool_request("read", "get_page_text", {})
        assert provider.runtime.authority.snapshot()["runId"] == "origin-run"
        assert (await gateway.send_browser_tool_request("foreign", "click", {}, "local-browser"))["error_code"] == "BROWSER_ACCESS_NOT_GRANTED"
        return "done", {}

    gateway.on_chat_message = chat
    await gateway._run_chat(socket, "viewer", "origin-session", "work", "origin-run", AsyncMock(), browser_binding=binding)
    assert seen["result"]["text"] == "remote page"
    assert not any(message.get("type") == "tool_request" for message in socket.messages)
    assert provider.runtime.authority.snapshot()["runId"] is None
    assert SCREEN_RUN_BINDING.get() is None
    assert gateway._browser_provider_active == "local-browser"


async def test_screen_bound_capture_cannot_fall_back_to_physical_display(screen_gateway, monkeypatch):
    from flowly.agent.tools.computer import ComputerTool
    from flowly.agent.tools.screenshot import ScreenshotTool

    gateway, socket, provider, access = screen_gateway
    binding, _ = gateway._browser_binding_for_chat("viewer", socket, access)
    screenshot = ScreenshotTool()
    # Existing native/Electron capture implementations must not run.
    monkeypatch.setattr(screenshot, "_capture_via_electron_sync", Mock(side_effect=AssertionError("physical capture")))
    for name in ("_capture_linux", "_capture_macos", "_capture_windows"):
        monkeypatch.setattr(screenshot, name, AsyncMock(side_effect=AssertionError("physical capture")))
    seen = {}

    async def chat(*_):
        human = provider.runtime.authority.acquire("human", "generation", 1)
        seen["capture"] = json.loads(await screenshot.execute())
        seen["computer"] = json.loads(await ComputerTool().execute("click", x=10, y=10))
        provider.runtime.authority.release("human", "generation", human["epoch"])
        return "done", {}

    gateway.on_chat_message = chat
    await gateway._run_chat(socket, "viewer", "origin-session", "work", "run", AsyncMock(), browser_binding=binding)
    assert seen["capture"]["error_code"] == "HUMAN_HAS_CONTROL"
    assert seen["computer"]["error_code"] == "BROWSER_SCOPE_ONLY"
    provider._dispatch.assert_not_awaited()


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
                    observation_valid=lambda result: True)
    token = SCREEN_RUN_BINDING.set(ScreenRunBinding(provider, "generation", "run"))
    try:
        result = await ScreenshotTool().execute()
        paths, summary = extract_reply_media(result)
        assert paths == [str(saved)]
        assert "attached" in summary
        assert "data:image" not in result
    finally:
        SCREEN_RUN_BINDING.reset(token)
