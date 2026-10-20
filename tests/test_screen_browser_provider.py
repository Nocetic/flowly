"""Host browser admission and reference fencing without a browser dependency."""

from unittest.mock import AsyncMock, Mock

import pytest

from flowly.desktop_runtime.authority import ScreenAuthority
from flowly.desktop_runtime.browser_provider import HostBrowserProvider, _url
from flowly.desktop_runtime.execution import ScreenExecution


@pytest.fixture
def provider(tmp_path):
    authority = ScreenAuthority(tmp_path)
    authority.start("generation")
    runtime = Mock(authority=authority)
    instance = HostBrowserProvider(runtime, ScreenExecution(authority))
    instance._connect = AsyncMock()
    instance._dispatch = AsyncMock(return_value={"success": True, "text": "page text"})
    return instance


async def test_human_control_prevents_connection_reads_and_capture(provider):
    provider.runtime.authority.acquire("human", "generation", 1)
    for action in ("read_page", "get_page_text", "screenshot", "tabs_list", "click"):
        result = await provider.request("run", action, "generation", action, {})
        assert result["error_code"] == "HUMAN_HAS_CONTROL"
    provider._connect.assert_not_awaited()
    provider._dispatch.assert_not_awaited()


async def test_generation_mismatch_never_connects_to_replacement_runtime(provider):
    result = await provider.request("run", "a", "old-generation", "read_page", {})
    assert result["error_code"] == "STALE_GENERATION"
    provider._connect.assert_not_awaited()


async def test_unsupported_operations_do_not_launch_or_fall_back(provider):
    for action in ("evaluate", "upload_file", "batch", "unknown"):
        result = await provider.request("run", action, "generation", action, {})
        assert result["error_code"] == "UNSUPPORTED_ACTION"
    provider._connect.assert_not_awaited()
    assert provider.runtime.authority.snapshot()["runId"] is None


async def test_observation_is_invalid_as_soon_as_human_takes_control(provider):
    result = await provider.request("run", "read", "generation", "read_page", {})
    assert provider.observation_valid(result)
    provider.runtime.authority.acquire("human", "generation", 1)
    assert not provider.observation_valid(result)


async def test_handback_invalidates_previous_element_handles(provider):
    await provider.request("run", "read", "generation", "read_page", {})
    old = Mock(dispose=AsyncMock())
    provider._refs["ref_1"] = (Mock(), old)
    human = provider.runtime.authority.acquire("human", "generation", 1)
    provider.runtime.authority.release("human", "generation", human["epoch"])
    await provider.request("run", "read-again", "generation", "read_page", {})
    old.dispose.assert_awaited_once()
    assert provider._refs == {}


async def test_backend_exception_text_never_leaks_credentials(provider):
    provider._browser = Mock(is_connected=lambda: True)
    provider._dispatch.side_effect = RuntimeError("typed-value secret-credential-example")
    result = await provider.request("run", "type", "generation", "type", {"text": "secret-credential-example"})
    assert result["error_code"] == "BROWSER_ACTION_FAILED"
    assert "secret-credential-example" not in str(result)
    assert not provider.runtime.authority.snapshot()["actionInFlight"]


async def test_backend_disconnect_keeps_action_closed_until_recovery(provider):
    provider._browser = Mock(is_connected=lambda: False)
    provider._dispatch.side_effect = RuntimeError("CDP disconnected")
    result = await provider.request("run", "a", "generation", "click", {})
    assert result["error_code"] == "ACTION_OUTCOME_UNKNOWN"
    assert provider.runtime.authority.snapshot()["actionInFlight"]


@pytest.mark.parametrize("url", ["file:///etc/passwd", "javascript:alert(1)", "data:text/html,hello", "https://user:password@example.com", "http:", None])
def test_navigation_has_no_local_file_or_credential_url_escape(url):
    from flowly.desktop_runtime.authority import ScreenError

    with pytest.raises(ScreenError):
        _url(url)


async def test_disconnecting_adapter_does_not_close_running_browser(provider):
    provider._browser = Mock(close=AsyncMock())
    browser = provider._browser
    provider._driver = Mock(stop=AsyncMock())
    driver = provider._driver
    await provider.disconnect()
    browser.close.assert_not_awaited()
    driver.stop.assert_awaited_once()


async def test_a_failed_action_logs_its_kind_never_its_text(provider):
    from loguru import logger

    lines = []
    sink = logger.add(lambda message: lines.append(str(message)), level="INFO")
    try:
        provider._browser = Mock(is_connected=lambda: True)
        class TargetClosedError(Exception):
            """Shaped like a driver's error, carrying page text."""

        provider._dispatch.side_effect = TargetClosedError("typed secret-credential-example into https://bank.example/login")
        result = await provider.request("run", "type", "generation", "type", {"text": "secret-credential-example"})
    finally:
        logger.remove(sink)
    assert result["error_code"] == "BROWSER_ACTION_FAILED"
    logged = "".join(lines)
    assert "browser action type failed: TargetClosedError" in logged
    assert "secret-credential-example" not in logged and "bank.example" not in logged
