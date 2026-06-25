"""Unit tests for Home Assistant tools.

Focuses on the security-critical surface:

- Entity ID regex (rejects path-traversal, dots, slashes, uppercase)
- Service/domain regex applied *before* blocklist (no bypass via traversal)
- Blocked-domain enforcement (six domains that allow code execution)
- ``data`` parameter accepts dict or JSON string, rejects garbage
- Bearer header is set on outgoing requests

HTTP is faked with ``httpx.MockTransport`` so no network is touched.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from flowly.agent.tools import homeassistant as ha
from flowly.agent.tools.homeassistant import (
    HACallServiceTool,
    HAGetStateTool,
    HAListEntitiesTool,
    HAListServicesTool,
    _BLOCKED_DOMAINS,
    _validate_entity_id,
    _validate_service_call,
)


# ---- pure validators ----------------------------------------------------


def test_validate_entity_id_accepts_canonical():
    assert _validate_entity_id("light.living_room") is None
    assert _validate_entity_id("sensor.temp_1") is None
    assert _validate_entity_id("binary_sensor.front_door") is None


def test_validate_entity_id_rejects_empty():
    err = _validate_entity_id("")
    assert err and "Missing" in err


def test_validate_entity_id_rejects_path_traversal():
    # The ID is interpolated into /api/states/{entity_id}; anything that
    # could escape the path must fail format validation.
    for bad in [
        "../config",
        "light/../sensor.temp",
        "light.living_room/../../etc/passwd",
        "light.living room",  # space
        "Light.Living_Room",  # uppercase
        "light",              # missing object
        ".light",
        "light.",
    ]:
        err = _validate_entity_id(bad)
        assert err is not None, f"{bad!r} should be rejected"


def test_validate_service_call_accepts_canonical():
    assert _validate_service_call("light", "turn_on") is None
    assert _validate_service_call("climate", "set_temperature") is None


def test_validate_service_call_blocks_dangerous_domains():
    # All six domains must be blocked even with a valid format.
    for domain in _BLOCKED_DOMAINS:
        err = _validate_service_call(domain, "turn_on")
        assert err and "blocked" in err.lower()


def test_validate_service_call_rejects_traversal_before_blocklist():
    # Traversal attempts must fail format check first, otherwise a bypass
    # like "shell_command/../light" could slip through if the blocklist
    # ran first and only saw the suffix.
    for domain in [
        "shell_command/../light",
        "../../api/config",
        "shell_command/foo",
        "Light",  # uppercase rejected by format
        "light.foo",
    ]:
        err = _validate_service_call(domain, "turn_on")
        assert err is not None, f"{domain!r} should be rejected"
        # Critically: must mention format, not blocklist — proves order.
        if "/" in domain or "." in domain or domain[0].isupper():
            assert "format" in err.lower(), (
                f"{domain!r} should fail format check (got: {err})"
            )


def test_validate_service_call_rejects_bad_service_name():
    assert _validate_service_call("light", "../states") is not None
    assert _validate_service_call("light", "turn_on/extra") is not None
    assert _validate_service_call("light", "Turn_On") is not None
    assert _validate_service_call("light", "") is not None


def test_validate_service_call_requires_both_fields():
    assert _validate_service_call("", "turn_on") is not None
    assert _validate_service_call("light", "") is not None


# ---- HTTP-mocked tool tests --------------------------------------------


def _patch_client(monkeypatch: pytest.MonkeyPatch, handler) -> None:
    """Replace httpx.AsyncClient with a MockTransport-backed instance."""
    transport = httpx.MockTransport(handler)
    real_init = httpx.AsyncClient.__init__

    def patched_init(self, *args: Any, **kwargs: Any) -> None:
        kwargs["transport"] = transport
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched_init)


async def test_list_entities_filters_by_domain(monkeypatch: pytest.MonkeyPatch):
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("authorization")
        return httpx.Response(
            200,
            json=[
                {"entity_id": "light.salon", "state": "on", "attributes": {"friendly_name": "Salon"}},
                {"entity_id": "sensor.temp", "state": "21", "attributes": {"friendly_name": "Temp"}},
                {"entity_id": "light.kitchen", "state": "off", "attributes": {"friendly_name": "Kitchen"}},
            ],
        )

    _patch_client(monkeypatch, handler)
    tool = HAListEntitiesTool(url="http://hass.local:8123/", token="abc123")
    out = await tool.execute(domain="light")

    assert captured["auth"] == "Bearer abc123"
    assert captured["url"].endswith("/api/states")
    body = json.loads(out)
    assert body["count"] == 2
    assert {e["entity_id"] for e in body["entities"]} == {"light.salon", "light.kitchen"}


async def test_list_entities_filters_by_area_substring(monkeypatch: pytest.MonkeyPatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                {"entity_id": "light.a", "state": "on", "attributes": {"friendly_name": "Living Room Light"}},
                {"entity_id": "light.b", "state": "on", "attributes": {"friendly_name": "Kitchen Light"}},
                {"entity_id": "light.c", "state": "on", "attributes": {"area": "Living Room"}},
            ],
        )

    _patch_client(monkeypatch, handler)
    tool = HAListEntitiesTool(url="http://hass.local:8123", token="t")
    out = await tool.execute(area="living room")
    body = json.loads(out)
    assert body["count"] == 2
    ids = {e["entity_id"] for e in body["entities"]}
    assert ids == {"light.a", "light.c"}


async def test_get_state_validates_entity_id(monkeypatch: pytest.MonkeyPatch):
    # Bad entity_id must short-circuit before any HTTP call.
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json={})

    _patch_client(monkeypatch, handler)
    tool = HAGetStateTool(url="http://hass.local:8123", token="t")
    out = await tool.execute(entity_id="../../api/config")
    assert "Error" in out and "entity_id" in out.lower()
    assert called is False


async def test_get_state_returns_attributes(monkeypatch: pytest.MonkeyPatch):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/states/climate.thermo"
        return httpx.Response(200, json={
            "entity_id": "climate.thermo",
            "state": "heat",
            "attributes": {"current_temperature": 21, "temperature": 22},
            "last_changed": "2026-05-09T12:00:00",
            "last_updated": "2026-05-09T12:00:00",
        })

    _patch_client(monkeypatch, handler)
    tool = HAGetStateTool(url="http://hass.local:8123", token="t")
    out = await tool.execute(entity_id="climate.thermo")
    body = json.loads(out)
    assert body["state"] == "heat"
    assert body["attributes"]["current_temperature"] == 21


async def test_call_service_blocks_shell_command(monkeypatch: pytest.MonkeyPatch):
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json=[])

    _patch_client(monkeypatch, handler)
    tool = HACallServiceTool(url="http://hass.local:8123", token="t")
    out = await tool.execute(domain="shell_command", service="run")
    assert "blocked" in out.lower()
    assert called is False, "blocked domain must not reach HA"


async def test_call_service_rejects_traversal_payload(monkeypatch: pytest.MonkeyPatch):
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json=[])

    _patch_client(monkeypatch, handler)
    tool = HACallServiceTool(url="http://hass.local:8123", token="t")
    # Format check fires before blocklist; this string would naively
    # split to "shell_command" + suffix and bypass. Must be rejected.
    out = await tool.execute(domain="shell_command/../light", service="turn_on")
    assert "format" in out.lower() or "invalid" in out.lower()
    assert called is False


async def test_call_service_parses_json_data(monkeypatch: pytest.MonkeyPatch):
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content.decode())
        return httpx.Response(200, json=[
            {"entity_id": "light.salon", "state": "on"},
        ])

    _patch_client(monkeypatch, handler)
    tool = HACallServiceTool(url="http://hass.local:8123", token="t")
    out = await tool.execute(
        domain="light",
        service="turn_on",
        entity_id="light.salon",
        data='{"brightness": 128, "color_name": "blue"}',
    )

    assert captured["url"].endswith("/api/services/light/turn_on")
    body = captured["body"]
    # entity_id from explicit param wins, brightness/color preserved.
    assert body["entity_id"] == "light.salon"
    assert body["brightness"] == 128
    assert body["color_name"] == "blue"

    out_body = json.loads(out)
    assert out_body["success"] is True
    assert out_body["service"] == "light.turn_on"


async def test_call_service_rejects_garbage_data(monkeypatch: pytest.MonkeyPatch):
    _patch_client(monkeypatch, lambda req: httpx.Response(200, json=[]))
    tool = HACallServiceTool(url="http://hass.local:8123", token="t")
    out = await tool.execute(domain="light", service="turn_on", data="not-json{")
    assert "Invalid JSON" in out


async def test_call_service_rejects_non_object_data(monkeypatch: pytest.MonkeyPatch):
    _patch_client(monkeypatch, lambda req: httpx.Response(200, json=[]))
    tool = HACallServiceTool(url="http://hass.local:8123", token="t")
    out = await tool.execute(domain="light", service="turn_on", data='[1, 2, 3]')
    assert "object" in out.lower()


async def test_unauthorized_token_surfaces_clear_error(monkeypatch: pytest.MonkeyPatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": "Unauthorized"})

    _patch_client(monkeypatch, handler)
    tool = HAListEntitiesTool(url="http://hass.local:8123", token="bad")
    out = await tool.execute()
    assert "401" in out or "token" in out.lower()


def test_url_trailing_slash_normalized():
    # Internal contract: URL is stripped once at construction time so
    # later concat with /api/... can't double up.
    client = ha._HAClient(url="http://hass.local:8123/", token="t")
    assert client._base == "http://hass.local:8123"
    client2 = ha._HAClient(url="http://hass.local:8123", token="t")
    assert client2._base == "http://hass.local:8123"


# ---- empty affected_entities heuristic ---------------------------------


def test_is_state_changing_explicit_set():
    assert ha._is_state_changing("turn_on") is True
    assert ha._is_state_changing("turn_off") is True
    assert ha._is_state_changing("toggle") is True
    assert ha._is_state_changing("lock") is True
    assert ha._is_state_changing("media_play") is True


def test_is_state_changing_prefixes():
    # Prefix-based: covers set_temperature, set_hvac_mode, select_option, etc.
    assert ha._is_state_changing("set_temperature") is True
    assert ha._is_state_changing("set_hvac_mode") is True
    assert ha._is_state_changing("select_option") is True
    assert ha._is_state_changing("increase_speed") is True


def test_is_state_changing_fire_and_forget_excluded():
    # These return empty affected_entities normally; must NOT be flagged.
    assert ha._is_state_changing("send_message") is False
    assert ha._is_state_changing("reload") is False
    assert ha._is_state_changing("notify") is False
    assert ha._is_state_changing("persistent_notification") is False


async def test_call_service_flags_empty_state_change(monkeypatch: pytest.MonkeyPatch):
    # The TV bug: HA returns 200 + [] for media_player.turn_off when the
    # device doesn't actually act on it. Old behavior reported
    # success=true; new behavior must flag it so the LLM doesn't claim
    # victory.
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[])  # no state changed

    _patch_client(monkeypatch, handler)
    tool = HACallServiceTool(url="http://hass.local:8123", token="t")
    out = await tool.execute(
        domain="media_player",
        service="turn_off",
        entity_id="media_player.tv",
    )
    body = json.loads(out)
    assert body["success"] is False
    assert body["state_changed"] is False
    assert body["affected_entities"] == []
    assert "warning" in body
    # The warning must mention the diagnostic hint (remote.{name}) so the
    # LLM has somewhere to go next.
    assert "remote" in body["warning"].lower()


async def test_call_service_does_not_flag_fire_and_forget(monkeypatch: pytest.MonkeyPatch):
    # notify.persistent_notification: empty list is the normal response,
    # NOT a failure. Must report success.
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[])

    _patch_client(monkeypatch, handler)
    tool = HACallServiceTool(url="http://hass.local:8123", token="t")
    out = await tool.execute(
        domain="persistent_notification",
        service="create",
    )
    body = json.loads(out)
    assert body["success"] is True
    assert body["state_changed"] is False
    assert "warning" not in body


async def test_call_service_success_when_state_changed(monkeypatch: pytest.MonkeyPatch):
    # Real success: HA returns the changed entity state. success=true,
    # state_changed=true, no warning.
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[
            {"entity_id": "light.salon", "state": "on"},
        ])

    _patch_client(monkeypatch, handler)
    tool = HACallServiceTool(url="http://hass.local:8123", token="t")
    out = await tool.execute(
        domain="light",
        service="turn_on",
        entity_id="light.salon",
    )
    body = json.loads(out)
    assert body["success"] is True
    assert body["state_changed"] is True
    assert body["affected_entities"] == [{"entity_id": "light.salon", "state": "on"}]
    assert "warning" not in body


async def test_call_service_flags_set_temperature_with_no_change(monkeypatch: pytest.MonkeyPatch):
    # Prefix-matched service (set_*): same flagging as turn_on/turn_off.
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[])

    _patch_client(monkeypatch, handler)
    tool = HACallServiceTool(url="http://hass.local:8123", token="t")
    out = await tool.execute(
        domain="climate",
        service="set_temperature",
        entity_id="climate.thermostat",
        data='{"temperature": 22}',
    )
    body = json.loads(out)
    assert body["success"] is False
    assert "warning" in body
