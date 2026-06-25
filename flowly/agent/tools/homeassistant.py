"""Home Assistant tools — control smart home devices via REST API.

Registers four LLM-callable tools, each gated on a non-empty
``integrations.home_assistant.{url, token}`` config:

- ``ha_list_entities`` — list/filter entities by domain or area
- ``ha_get_state`` — get detailed state of a single entity
- ``ha_list_services`` — list available services (actions) per domain
- ``ha_call_service`` — call a HA service (turn_on, set_temperature, etc.)

Authentication uses a Long-Lived Access Token (HA Profile → Long-Lived
Access Tokens). Bearer header is sent on every call. The token never
leaves this process; HA is contacted directly over the local network.

Security notes
--------------
HA exposes service-level access only via the integration layer, so any
guards must live here:

- Domain and service identifiers are matched against ``_SERVICE_NAME_RE``
  *before* the blocklist check. This prevents path-traversal in
  ``/api/services/{domain}/{service}`` (e.g. ``domain="../../api/config"``)
  and bypasses like ``domain="shell_command/../light"``.
- ``_BLOCKED_DOMAINS`` contains six service domains that allow code or
  shell execution on the HA host (or SSRF from the HA server). They are
  rejected even if the user's token would otherwise authorise them.
- Entity IDs are validated against ``_ENTITY_ID_RE`` whenever supplied.

Adapted from an upstream homeassistant tool implementation.
"""

from __future__ import annotations

import json
import re
from typing import Any

import httpx
from loguru import logger

from flowly.agent.tools.base import Tool


# Valid HA entity_id format (e.g. "light.living_room", "sensor.temperature_1").
_ENTITY_ID_RE = re.compile(r"^[a-z_][a-z0-9_]*\.[a-z0-9_]+$")

# Valid HA service / domain names. Lowercase ASCII letters, digits,
# underscores only — no slashes, dots, or other URL-meaningful chars.
# The domain and service are interpolated into
# ``/api/services/{domain}/{service}``, so accepting arbitrary strings
# would enable path traversal and blocked-domain bypass.
_SERVICE_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")

# Service domains rejected for security.  HA gives no service-level
# authorisation, so the safety floor is here. These domains are either
# direct code execution surfaces or SSRF amplifiers from the HA host.
_BLOCKED_DOMAINS = frozenset({
    "shell_command",   # arbitrary shell as the HA user (often root in container)
    "command_line",    # sensors / switches that execute shell commands
    "python_script",   # sandboxed but escapable via hass.services.call()
    "pyscript",        # broader scripting integration
    "hassio",          # supervisor: addon control, host shutdown, container stdin
    "rest_command",    # outbound HTTP from HA server (SSRF vector)
})

_DEFAULT_TIMEOUT = 15.0

# Services where an empty ``affected_entities`` response is suspicious —
# the call was accepted by HA but no entity state changed. For these we
# flag ``success: False`` and surface a warning so the LLM doesn't blindly
# claim the action succeeded.
#
# Fire-and-forget services (notify.*, script.*, persistent_notification.*)
# are NOT in this set: empty list is the normal response shape for them.
_STATE_CHANGING_SERVICES: frozenset[str] = frozenset({
    "turn_on", "turn_off", "toggle",
    "open_cover", "close_cover", "stop_cover", "open_valve", "close_valve",
    "lock", "unlock",
    "play_media", "media_play", "media_pause", "media_stop",
    "media_next_track", "media_previous_track",
    "volume_mute", "volume_up", "volume_down",
    "arm_home", "arm_away", "arm_night", "disarm",
})

# Service-name prefixes that imply state mutation. We use prefix matching
# so we don't have to enumerate every ``set_temperature``,
# ``set_hvac_mode``, ``set_fan_speed``, ``select_option``, etc.
_STATE_CHANGING_PREFIXES: tuple[str, ...] = (
    "set_", "select_", "increase_", "decrease_",
)


def _is_state_changing(service: str) -> bool:
    """Return True if an empty ``affected_entities`` should be flagged."""
    if service in _STATE_CHANGING_SERVICES:
        return True
    return any(service.startswith(p) for p in _STATE_CHANGING_PREFIXES)


# ---------------------------------------------------------------------------
# Shared client helper
# ---------------------------------------------------------------------------


class _HAClient:
    """Tiny httpx wrapper that hides URL/token plumbing.

    Built once per tool. Tools share nothing else; each call opens its
    own ``AsyncClient`` so we don't keep sockets open between LLM turns.
    """

    def __init__(self, url: str, token: str):
        self._base = url.rstrip("/")
        self._token = token
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

    async def get(self, path: str) -> Any:
        async with httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT) as client:
            resp = await client.get(f"{self._base}{path}", headers=self._headers)
            self._raise_for_status(resp)
            return resp.json()

    async def post(self, path: str, payload: dict) -> Any:
        async with httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT) as client:
            resp = await client.post(
                f"{self._base}{path}",
                headers=self._headers,
                json=payload,
            )
            self._raise_for_status(resp)
            return resp.json()

    @staticmethod
    def _raise_for_status(resp: httpx.Response) -> None:
        if resp.status_code == 401:
            raise RuntimeError(
                "Home Assistant rejected the access token (401). "
                "Generate a new Long-Lived Access Token from your HA Profile."
            )
        if resp.status_code == 404:
            raise RuntimeError("Home Assistant returned 404 — entity or service not found.")
        if resp.status_code >= 500:
            raise RuntimeError(f"Home Assistant server error ({resp.status_code}).")
        if resp.status_code >= 400:
            raise RuntimeError(f"Home Assistant returned {resp.status_code}: {resp.text[:200]}")


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def _validate_entity_id(entity_id: str) -> str | None:
    """Return an error string if ``entity_id`` is malformed, else None."""
    if not entity_id:
        return "Missing required parameter: entity_id"
    if not _ENTITY_ID_RE.match(entity_id):
        return f"Invalid entity_id format: {entity_id!r}"
    return None


def _validate_service_call(domain: str, service: str) -> str | None:
    """Validate a domain+service pair. Returns error string or None."""
    if not domain or not service:
        return "Missing required parameters: domain and service"
    # Format check first — guards against blocklist bypass via traversal.
    if not _SERVICE_NAME_RE.match(domain):
        return f"Invalid domain format: {domain!r}"
    if not _SERVICE_NAME_RE.match(service):
        return f"Invalid service format: {service!r}"
    if domain in _BLOCKED_DOMAINS:
        return (
            f"Service domain '{domain}' is blocked for security. "
            f"Blocked: {', '.join(sorted(_BLOCKED_DOMAINS))}"
        )
    return None


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


class HAListEntitiesTool(Tool):
    """List Home Assistant entities, optionally filtered by domain or area."""

    def __init__(self, url: str, token: str):
        self._client = _HAClient(url, token)

    @property
    def name(self) -> str:
        return "ha_list_entities"

    @property
    def description(self) -> str:
        return (
            "List Home Assistant entities. Optionally filter by domain "
            "(light, switch, climate, sensor, binary_sensor, cover, fan, "
            "media_player, etc.) or by area name (living room, kitchen, "
            "bedroom, etc.). Returns a compact summary (entity_id, state, "
            "friendly_name) for each match."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "domain": {
                    "type": "string",
                    "description": (
                        "Entity domain to filter by. Omit to list all entities."
                    ),
                },
                "area": {
                    "type": "string",
                    "description": (
                        "Area/room name to match against friendly names. "
                        "Case-insensitive substring match."
                    ),
                },
            },
            "required": [],
        }

    async def execute(self, **kwargs: Any) -> str:
        domain = kwargs.get("domain") or None
        area = kwargs.get("area") or None
        try:
            states = await self._client.get("/api/states")
        except Exception as e:
            logger.error(f"[HA] list_entities failed: {e}")
            return f"Error: {e}"

        if domain:
            states = [s for s in states if s.get("entity_id", "").startswith(f"{domain}.")]

        if area:
            needle = area.lower()
            filtered = []
            for s in states:
                attrs = s.get("attributes", {}) or {}
                name = (attrs.get("friendly_name") or "").lower()
                area_name = (attrs.get("area") or attrs.get("area_id") or "").lower()
                if needle in name or needle in area_name:
                    filtered.append(s)
            states = filtered

        entities = [
            {
                "entity_id": s.get("entity_id", ""),
                "state": s.get("state", ""),
                "friendly_name": (s.get("attributes", {}) or {}).get("friendly_name", ""),
            }
            for s in states
        ]
        return json.dumps({"count": len(entities), "entities": entities})


class HAGetStateTool(Tool):
    """Get detailed state of a single Home Assistant entity."""

    def __init__(self, url: str, token: str):
        self._client = _HAClient(url, token)

    @property
    def name(self) -> str:
        return "ha_get_state"

    @property
    def description(self) -> str:
        return (
            "Get the detailed state of a single Home Assistant entity, "
            "including all attributes (brightness, color, target temperature, "
            "sensor readings, etc.)."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "entity_id": {
                    "type": "string",
                    "description": (
                        "The entity ID to query (e.g. 'light.living_room', "
                        "'climate.thermostat', 'sensor.temperature')."
                    ),
                },
            },
            "required": ["entity_id"],
        }

    async def execute(self, **kwargs: Any) -> str:
        entity_id = kwargs.get("entity_id", "")
        err = _validate_entity_id(entity_id)
        if err:
            return f"Error: {err}"

        try:
            data = await self._client.get(f"/api/states/{entity_id}")
        except Exception as e:
            logger.error(f"[HA] get_state failed: {e}")
            return f"Error: {e}"

        return json.dumps({
            "entity_id": data.get("entity_id", ""),
            "state": data.get("state", ""),
            "attributes": data.get("attributes", {}) or {},
            "last_changed": data.get("last_changed"),
            "last_updated": data.get("last_updated"),
        })


class HAListServicesTool(Tool):
    """List available Home Assistant services per domain."""

    def __init__(self, url: str, token: str):
        self._client = _HAClient(url, token)

    @property
    def name(self) -> str:
        return "ha_list_services"

    @property
    def description(self) -> str:
        return (
            "List available Home Assistant services (actions) for device "
            "control. Use this to discover how to control devices found via "
            "ha_list_entities — shows what actions can be performed on each "
            "device type and the parameters they accept."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "domain": {
                    "type": "string",
                    "description": (
                        "Filter by domain (light, climate, switch, ...). "
                        "Omit to list services for all domains."
                    ),
                },
            },
            "required": [],
        }

    async def execute(self, **kwargs: Any) -> str:
        domain_filter = kwargs.get("domain") or None
        try:
            services = await self._client.get("/api/services")
        except Exception as e:
            logger.error(f"[HA] list_services failed: {e}")
            return f"Error: {e}"

        if domain_filter:
            services = [s for s in services if s.get("domain") == domain_filter]

        # Compact: keep only description + field descriptions, drop schemas.
        result = []
        for entry in services:
            d = entry.get("domain", "")
            domain_services: dict[str, Any] = {}
            for svc_name, svc_info in (entry.get("services") or {}).items():
                if not isinstance(svc_info, dict):
                    continue
                svc_entry: dict[str, Any] = {
                    "description": svc_info.get("description", ""),
                }
                fields = svc_info.get("fields") or {}
                if fields:
                    svc_entry["fields"] = {
                        k: (v.get("description", "") if isinstance(v, dict) else "")
                        for k, v in fields.items()
                    }
                domain_services[svc_name] = svc_entry
            result.append({"domain": d, "services": domain_services})

        return json.dumps({"count": len(result), "domains": result})


class HACallServiceTool(Tool):
    """Call a Home Assistant service to control a device."""

    def __init__(self, url: str, token: str):
        self._client = _HAClient(url, token)

    @property
    def name(self) -> str:
        return "ha_call_service"

    @property
    def description(self) -> str:
        return (
            "Call a Home Assistant service to control a device. Use "
            "ha_list_services first to discover available services and "
            "their parameters. For TVs and other media players, the HA "
            "dashboard power button often calls 'remote.turn_off' on a "
            "separate 'remote.{name}' entity rather than "
            "'media_player.turn_off' — if the latter returns success=false "
            "with empty affected_entities, list 'remote' domain entities "
            "and try that instead."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "domain": {
                    "type": "string",
                    "description": (
                        "Service domain (light, switch, climate, cover, "
                        "media_player, fan, scene, script)."
                    ),
                },
                "service": {
                    "type": "string",
                    "description": (
                        "Service name (turn_on, turn_off, toggle, "
                        "set_temperature, set_hvac_mode, open_cover, "
                        "close_cover, set_volume_level)."
                    ),
                },
                "entity_id": {
                    "type": "string",
                    "description": (
                        "Target entity ID (e.g. 'light.living_room'). "
                        "Some services like scene.turn_on may not need this."
                    ),
                },
                "data": {
                    "type": "string",
                    "description": (
                        "Additional service data as a JSON object string. "
                        'Examples: {"brightness": 255, "color_name": "blue"} '
                        'for lights, {"temperature": 22, "hvac_mode": "heat"} '
                        'for climate, {"volume_level": 0.5} for media players.'
                    ),
                },
            },
            "required": ["domain", "service"],
        }

    async def execute(self, **kwargs: Any) -> str:
        domain = kwargs.get("domain", "") or ""
        service = kwargs.get("service", "") or ""
        err = _validate_service_call(domain, service)
        if err:
            return f"Error: {err}"

        entity_id = kwargs.get("entity_id") or None
        if entity_id:
            err = _validate_entity_id(entity_id)
            if err:
                return f"Error: {err}"

        # ``data`` may arrive as a JSON string (LLM convention) or a dict.
        raw_data = kwargs.get("data")
        data: dict[str, Any] | None = None
        if isinstance(raw_data, str):
            stripped = raw_data.strip()
            if stripped:
                try:
                    parsed = json.loads(stripped)
                except json.JSONDecodeError as e:
                    return f"Error: Invalid JSON in 'data' parameter: {e}"
                if not isinstance(parsed, dict):
                    return "Error: 'data' must be a JSON object."
                data = parsed
        elif isinstance(raw_data, dict):
            data = raw_data
        elif raw_data is not None:
            return "Error: 'data' must be a JSON object or JSON-encoded string."

        payload: dict[str, Any] = {}
        if data:
            payload.update(data)
        if entity_id:
            payload["entity_id"] = entity_id  # explicit param wins over data

        try:
            result = await self._client.post(f"/api/services/{domain}/{service}", payload)
        except Exception as e:
            logger.error(f"[HA] call_service {domain}.{service} failed: {e}")
            return f"Error: {e}"

        affected = []
        if isinstance(result, list):
            for s in result:
                if isinstance(s, dict):
                    affected.append({
                        "entity_id": s.get("entity_id", ""),
                        "state": s.get("state", ""),
                    })

        # HA returns 200 OK for any well-formed service call, even when
        # nothing actually happens (device unreachable, integration silently
        # no-ops, target entity already in requested state, etc.).
        # ``affected_entities`` is the only ground truth: it lists states
        # that changed during the call. For state-changing services an
        # empty list almost always means the action did NOT take effect,
        # and reporting ``success: True`` would mislead the agent into
        # claiming the job is done. Surface this honestly so the LLM can
        # verify with ha_get_state, retry on a different entity (e.g. the
        # ``remote.{name}`` companion), or tell the user to check.
        response: dict[str, Any] = {
            "service": f"{domain}.{service}",
            "affected_entities": affected,
            "state_changed": bool(affected),
        }

        if not affected and _is_state_changing(service):
            target = entity_id or "(unspecified)"
            response["success"] = False
            response["warning"] = (
                f"Home Assistant accepted '{domain}.{service}' on "
                f"'{target}' but no entity state changed in the response. "
                "Possible causes: device already in the target state, "
                "device unreachable on the network, integration does not "
                "support this action for this entity, or the state change "
                "is asynchronous and will arrive later. Verify with "
                "ha_get_state. For TVs and media_players, the dashboard "
                "power button often targets 'remote.{name}' rather than "
                "'media_player.{name}' — try ha_list_entities domain=remote."
            )
        else:
            response["success"] = True

        return json.dumps(response)
