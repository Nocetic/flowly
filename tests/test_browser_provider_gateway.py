"""Browser provider registry and request-routing tests."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock

import aiohttp
import pytest
from aiohttp.test_utils import TestServer

from flowly.gateway.server import GatewayServer


class FakeWebSocket:
    def __init__(self) -> None:
        self.closed = False
        self.messages: list[dict[str, Any]] = []

    async def send_json(self, data: dict[str, Any]) -> None:
        self.messages.append(data)


def _server_with_clients(*client_ids: str) -> tuple[GatewayServer, dict[str, FakeWebSocket]]:
    server = GatewayServer(host="127.0.0.1", port=0)
    clients = {client_id: FakeWebSocket() for client_id in client_ids}
    server._ws_clients.update(clients)  # type: ignore[arg-type]
    return server, clients


def test_legacy_extension_registers_as_provider() -> None:
    server, _ = _server_with_clients("chrome-1")

    result = server._register_browser_provider("chrome-1", {}, legacy=True)

    assert result["ok"] is True
    assert result["provider"]["id"] == "chrome-extension:chrome-1"
    assert result["provider"]["type"] == "chrome_extension"
    assert result["provider"]["active"] is True
    assert server.has_extension_client() is True
    assert server.has_browser_provider() is True


def test_legacy_extension_accepts_historical_type_labels() -> None:
    server, _ = _server_with_clients("chrome-1")

    result = server._register_browser_provider(
        "chrome-1", {"type": "browser-use-extension-v1"}, legacy=True
    )

    assert result["provider"]["type"] == "chrome_extension"


def test_explicit_selection_survives_other_provider_reregister() -> None:
    server, _ = _server_with_clients("chrome-1", "desktop-browser")
    server._register_browser_provider(
        "chrome-1",
        {"providerId": "chrome", "type": "chrome_extension"},
    )
    server._register_browser_provider(
        "desktop-browser",
        {"providerId": "embedded", "type": "embedded"},
    )
    assert server._browser_provider_active == "embedded"

    assert server._select_browser_provider("chrome", explicit=True) is True
    server._register_browser_provider(
        "desktop-browser",
        {"providerId": "embedded", "type": "embedded"},
    )

    assert server._browser_provider_active == "chrome"
    providers = {provider["id"]: provider for provider in server._list_browser_providers()}
    assert providers["chrome"]["active"] is True
    assert providers["embedded"]["active"] is False


@pytest.mark.asyncio
async def test_tool_result_must_come_from_target_provider() -> None:
    server, clients = _server_with_clients("chrome-1", "desktop-browser")
    server._register_browser_provider(
        "chrome-1",
        {"providerId": "chrome", "type": "chrome_extension"},
    )
    server._register_browser_provider(
        "desktop-browser",
        {"providerId": "embedded", "type": "embedded"},
    )

    request = asyncio.create_task(
        server.send_browser_tool_request(
            "request-1", "read_page", {}, provider_id="embedded"
        )
    )
    await asyncio.sleep(0)

    assert clients["desktop-browser"].messages[-1]["providerId"] == "embedded"
    server._handle_extension_tool_result(
        {"id": "request-1", "result": {"source": "wrong"}}, "chrome-1"
    )
    await asyncio.sleep(0)
    assert request.done() is False

    server._handle_extension_tool_result(
        {"id": "request-1", "result": {"source": "embedded"}},
        "desktop-browser",
    )
    assert await request == {"source": "embedded"}


@pytest.mark.asyncio
async def test_disconnect_cancels_only_that_providers_requests() -> None:
    server, _ = _server_with_clients("chrome-1", "desktop-browser")
    server._register_browser_provider(
        "chrome-1",
        {"providerId": "chrome", "type": "chrome_extension"},
    )
    server._register_browser_provider(
        "desktop-browser",
        {"providerId": "embedded", "type": "embedded"},
    )

    chrome_request = asyncio.create_task(
        server.send_browser_tool_request("chrome-request", "tabs_list", {}, "chrome")
    )
    embedded_request = asyncio.create_task(
        server.send_browser_tool_request(
            "embedded-request", "tabs_list", {}, "embedded"
        )
    )
    await asyncio.sleep(0)

    assert server._unregister_browser_provider("chrome-1") == 1
    assert await chrome_request == {
        "error": "Browser provider disconnected",
        "error_code": "BROWSER_PROVIDER_UNAVAILABLE",
    }
    assert embedded_request.done() is False

    server._handle_extension_tool_result(
        {"id": "embedded-request", "result": {"tabs": []}},
        "desktop-browser",
    )
    assert await embedded_request == {"tabs": []}


@pytest.mark.asyncio
async def test_capabilities_reject_unsupported_action_before_send() -> None:
    server, clients = _server_with_clients("desktop-browser")
    server._register_browser_provider(
        "desktop-browser",
        {
            "providerId": "embedded",
            "type": "embedded",
            "capabilities": ["read_page"],
        },
    )

    result = await server.send_browser_tool_request(
        "request-1", "upload_file", {}, "embedded"
    )

    assert result["error_code"] == "UNSUPPORTED_ACTION"
    assert clients["desktop-browser"].messages == []


def test_disconnect_falls_back_to_most_recent_connected_provider() -> None:
    server, _ = _server_with_clients("chrome-1", "desktop-browser", "chrome-2")
    server._register_browser_provider(
        "chrome-1", {"providerId": "chrome-1", "type": "chrome_extension"}
    )
    server._register_browser_provider(
        "desktop-browser", {"providerId": "embedded", "type": "embedded"}
    )
    server._register_browser_provider(
        "chrome-2", {"providerId": "chrome-2", "type": "chrome_extension"}
    )
    server._select_browser_provider("embedded", explicit=True)

    server._unregister_browser_provider("desktop-browser")

    assert server._browser_provider_active == "chrome-2"
    assert server._extension_active == "chrome-2"


@pytest.mark.asyncio
async def test_provider_protocol_over_real_websocket() -> None:
    """Exercise registration, discovery and tool routing through /ws."""
    gateway = GatewayServer(
        host="127.0.0.1",
        port=0,
        on_chat_message=AsyncMock(),
    )
    http_server = TestServer(gateway._create_app())
    await http_server.start_server()
    session = aiohttp.ClientSession()
    provider = None
    controller = None
    try:
        provider_url = str(
            http_server.make_url("/ws?clientId=flowly-embedded-browser")
        ).replace("http://", "ws://", 1)
        controller_url = str(
            http_server.make_url("/ws?clientId=desktop-controller")
        ).replace("http://", "ws://", 1)
        provider = await session.ws_connect(provider_url)
        controller = await session.ws_connect(controller_url)

        await provider.send_json({
            "type": "rpc",
            "id": "register",
            "method": "browser.provider.register",
            "params": {
                "providerId": "flowly-embedded-browser",
                "type": "embedded",
                "displayName": "Flowly Embedded Browser",
                "protocolVersion": 1,
                "capabilities": ["read_page"],
            },
        })
        register_reply = await provider.receive_json()
        assert register_reply["result"]["provider"]["active"] is True
        registration_id = register_reply["result"]["provider"]["registrationId"]

        await controller.send_json({
            "type": "rpc",
            "id": "list",
            "method": "browser.providers.list",
        })
        list_reply = await controller.receive_json()
        assert list_reply["result"]["activeProviderId"] == "flowly-embedded-browser"
        assert "registrationId" not in list_reply["result"]["providers"][0]

        tool_result = asyncio.create_task(
            gateway.send_browser_tool_request("tool-1", "read_page", {})
        )
        tool_request = await provider.receive_json()
        assert tool_request == {
            "type": "tool_request",
            "id": "tool-1",
            "action": "read_page",
            "params": {},
            "providerId": "flowly-embedded-browser",
            "registrationId": registration_id,
        }
        await provider.send_json({
            "type": "tool_result",
            "id": "tool-1",
            "result": {"success": True, "title": "Example"},
        })
        assert await asyncio.wait_for(tool_result, timeout=1) == {
            "success": True,
            "title": "Example",
        }
    finally:
        if provider is not None:
            await provider.close()
        if controller is not None:
            await controller.close()
        await session.close()
        await http_server.close()


@pytest.mark.asyncio
async def test_session_bound_chat_round_trip_over_one_websocket() -> None:
    """Remote desktop contract: register, chat, tool result on one socket."""
    gateway: GatewayServer

    async def on_chat(*_args: Any) -> tuple[str, dict]:
        result = await gateway.send_browser_tool_request(
            "chat-browser-tool", "read_page", {}
        )
        return str(result.get("title") or result.get("error")), {}

    gateway = GatewayServer(host="127.0.0.1", port=0, on_chat_message=on_chat)
    http_server = TestServer(gateway._create_app())
    await http_server.start_server()
    session = aiohttp.ClientSession()
    desktop = None
    try:
        url = str(http_server.make_url("/ws?clientId=remote-desktop")).replace(
            "http://", "ws://", 1
        )
        desktop = await session.ws_connect(url)
        await desktop.send_json({
            "type": "rpc",
            "id": "register-v2",
            "method": "browser.provider.register",
            "params": {
                "providerId": "flowly-embedded-browser",
                "type": "embedded",
                "protocolVersion": 2,
                "sessionScoped": True,
                "capabilities": ["read_page"],
            },
        })
        registered = await desktop.receive_json()
        provider = registered["result"]["provider"]
        assert provider["active"] is False

        await desktop.send_json({
            "type": "rpc",
            "id": "chat-send",
            "method": "chat.send",
            "params": {
                "sessionKey": "desktop:remote-bound",
                "message": "read this page",
                "idempotencyKey": "run-bound",
                "browserAccess": {
                    "providerId": provider["id"],
                    "registrationId": provider["registrationId"],
                },
            },
        })
        accepted = await desktop.receive_json()
        assert accepted["result"] == {"runId": "run-bound", "status": "accepted"}

        request = await desktop.receive_json()
        assert request["type"] == "tool_request"
        assert request["registrationId"] == provider["registrationId"]
        await desktop.send_json({
            "type": "tool_result",
            "id": request["id"],
            "registrationId": provider["registrationId"],
            "result": {"success": True, "title": "Remote page"},
        })

        final = await desktop.receive_json()
        assert final["event"] == "chat"
        assert final["data"]["state"] == "final"
        assert final["data"]["message"]["content"][0]["text"] == "Remote page"

        await desktop.send_json({
            "type": "rpc",
            "id": "unregister-v2",
            "method": "browser.provider.unregister",
            "params": {
                "providerId": provider["id"],
                "registrationId": provider["registrationId"],
            },
        })
        unregistered = await desktop.receive_json()
        assert unregistered["result"]["ok"] is True
    finally:
        if desktop is not None:
            await desktop.close()
        await session.close()
        await http_server.close()


@pytest.mark.asyncio
async def test_direct_chat_without_browser_access_cannot_inherit_global_provider() -> None:
    """A direct chat must opt in; process-global selection is legacy-only."""
    server = GatewayServer(host="0.0.0.0", port=0, auth_token="remote-secret")
    clients = {"desktop-controller": FakeWebSocket()}
    server._ws_clients.update(clients)  # type: ignore[arg-type]
    server._register_browser_provider(
        "desktop-controller",
        {"providerId": "embedded", "type": "embedded", "protocolVersion": 2},
    )
    binding, error = server._browser_binding_for_chat(
        "desktop-controller", clients["desktop-controller"], None  # type: ignore[arg-type]
    )
    assert error is None

    observed: dict[str, Any] = {}

    async def on_chat(*_args: Any) -> tuple[str, dict]:
        observed["connected"] = server.has_browser_provider()
        observed["request"] = await server.send_browser_tool_request(
            "denied-request", "read_page", {}
        )
        return "done", {}

    server.on_chat_message = on_chat
    await server._run_chat(
        clients["desktop-controller"],  # type: ignore[arg-type]
        "desktop-controller",
        "desktop:test-no-browser",
        "hello",
        "run-no-browser",
        AsyncMock(),
        browser_binding=binding,
    )

    assert observed["connected"] is False
    assert observed["request"]["error_code"] == "BROWSER_ACCESS_NOT_GRANTED"
    assert not any(message.get("type") == "tool_request" for message in clients["desktop-controller"].messages)


def test_loopback_chat_preserves_legacy_global_provider_fallback() -> None:
    server, clients = _server_with_clients("desktop-controller", "local-browser")
    server._register_browser_provider(
        "local-browser",
        {"providerId": "embedded", "type": "embedded", "protocolVersion": 1},
    )

    binding, error = server._browser_binding_for_chat(
        "desktop-controller", clients["desktop-controller"], None  # type: ignore[arg-type]
    )

    assert error is None
    assert binding is None
    assert server.has_browser_provider() is True


@pytest.mark.asyncio
async def test_direct_chat_binding_routes_only_to_owned_registration() -> None:
    server, clients = _server_with_clients("desktop-controller")
    registered = server._register_browser_provider(
        "desktop-controller",
        {
            "providerId": "embedded",
            "type": "embedded",
            "protocolVersion": 2,
            "sessionScoped": True,
            "capabilities": ["read_page"],
        },
    )
    registration_id = registered["provider"]["registrationId"]
    binding, error = server._browser_binding_for_chat(
        "desktop-controller",
        clients["desktop-controller"],  # type: ignore[arg-type]
        {"providerId": "embedded", "registrationId": registration_id},
    )
    assert error is None

    async def on_chat(*_args: Any) -> tuple[str, dict]:
        result = await server.send_browser_tool_request(
            "bound-request", "read_page", {}
        )
        assert result == {"success": True, "title": "Bound"}
        return "done", {}

    server.on_chat_message = on_chat
    run = asyncio.create_task(
        server._run_chat(
            clients["desktop-controller"],  # type: ignore[arg-type]
            "desktop-controller",
            "desktop:test-bound-browser",
            "hello",
            "run-bound-browser",
            AsyncMock(),
            browser_binding=binding,
        )
    )
    await asyncio.sleep(0)

    request = next(
        message
        for message in clients["desktop-controller"].messages
        if message.get("type") == "tool_request"
    )
    assert request["providerId"] == "embedded"
    assert request["registrationId"] == registration_id
    server._handle_extension_tool_result(
        {
            "id": "bound-request",
            "registrationId": registration_id,
            "result": {"success": True, "title": "Bound"},
        },
        "desktop-controller",
    )
    await asyncio.wait_for(run, timeout=1)


def test_chat_binding_rejects_provider_owned_by_another_socket() -> None:
    server, clients = _server_with_clients("provider-owner", "other-client")
    registered = server._register_browser_provider(
        "provider-owner",
        {
            "providerId": "embedded",
            "type": "embedded",
            "protocolVersion": 2,
            "sessionScoped": True,
        },
    )

    _binding, error = server._browser_binding_for_chat(
        "other-client",
        clients["other-client"],  # type: ignore[arg-type]
        {
            "providerId": "embedded",
            "registrationId": registered["provider"]["registrationId"],
        },
    )

    assert error is not None
    assert error["code"] == "BROWSER_PROVIDER_NOT_OWNED"


def test_reregister_invalidates_previous_registration() -> None:
    server, clients = _server_with_clients("desktop-controller")
    first = server._register_browser_provider(
        "desktop-controller",
        {"providerId": "embedded", "type": "embedded", "protocolVersion": 2},
    )
    second = server._register_browser_provider(
        "desktop-controller",
        {"providerId": "embedded", "type": "embedded", "protocolVersion": 2},
    )
    assert first["provider"]["registrationId"] != second["provider"]["registrationId"]

    _binding, error = server._browser_binding_for_chat(
        "desktop-controller",
        clients["desktop-controller"],  # type: ignore[arg-type]
        {
            "providerId": "embedded",
            "registrationId": first["provider"]["registrationId"],
        },
    )
    assert error is not None
    assert error["code"] == "BROWSER_REGISTRATION_EXPIRED"


@pytest.mark.asyncio
async def test_session_scoped_provider_is_never_a_global_fallback() -> None:
    server, _clients = _server_with_clients("remote-desktop")
    registered = server._register_browser_provider(
        "remote-desktop",
        {
            "providerId": "embedded",
            "type": "embedded",
            "protocolVersion": 2,
            "sessionScoped": True,
        },
    )

    assert registered["provider"]["sessionScoped"] is True
    assert registered["provider"]["active"] is False
    assert server.has_browser_provider() is False
    result = await server.send_browser_tool_request(
        "background-request", "read_page", {}, provider_id="embedded"
    )
    assert result["error_code"] == "BROWSER_PROVIDER_UNAVAILABLE"
