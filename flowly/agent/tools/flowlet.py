"""Flowlet tool — the agent builds and maintains dynamic mini-screens.

Mirrors :class:`flowly.agent.tools.artifact.ArtifactTool`: one action-based
tool with an ``on_change`` broadcast callback (wired by the CLI after the
gateway exists). The agent authors a declarative definition against the
component catalog; this tool validates it, persists it, and broadcasts the
change so Desktop + iOS re-render live.

Client taps never come here — those are handled deterministically by
``flowlets.action`` (see :mod:`flowly.flowlets.actions`). This tool is the
*authoring* + *agent-side data* surface (create / update / log / query).
"""

from __future__ import annotations

import json
from typing import Any, Awaitable, Callable

from loguru import logger

from flowly.agent.tools.base import Tool
from flowly.flowlets import catalog, queries
from flowly.flowlets.schema import FlowletValidationError, validate_definition
from flowly.flowlets.store import now_ms


def _compact_preview(values: dict) -> dict:
    """Trim a resolved values map for the tool response: long arrays (chart
    buckets, sample rows) truncate to a few entries so the agent sees the SHAPE
    without a wall of data. Reserved (`__…`) keys dropped."""
    if not isinstance(values, dict):
        return {}
    out: dict = {}
    for k, v in values.items():
        if k.startswith("__"):
            continue
        if isinstance(v, list) and len(v) > 3:
            out[k] = v[:3] + [f"…(+{len(v) - 3} more)"]
        else:
            out[k] = v
    return out


def _extract_meta(definition: dict) -> dict:
    return {
        "name": str(definition.get("name", "")),
        "icon": definition.get("icon"),
        "accent": definition.get("accent"),
        "catalog": int(definition.get("catalog", catalog.CATALOG_VERSION)),
    }


def _summary(flowlet: dict, values: dict | None = None) -> dict:
    s = {
        "id": flowlet["id"],
        "name": flowlet.get("name"),
        "icon": flowlet.get("icon"),
        "accent": flowlet.get("accent"),
        "pinned": flowlet.get("pinned"),
        "version": flowlet.get("version"),
        "catalog": flowlet.get("catalog"),
        "updatedAt": flowlet.get("updated_at"),
    }
    if values is not None:
        s["values"] = values
        preview = queries.flowlet_preview(flowlet.get("definition") or {}, values)
        if preview is not None:
            s["preview"] = preview
    return s


class FlowletTool(Tool):
    """Action-based tool for authoring and updating flowlets."""

    def __init__(
        self,
        store: Any,
        on_change: Callable[[str, dict], Awaitable[None]] | None = None,
    ):
        self._store = store
        self._on_change = on_change
        self._watch_hook: Callable[[str], Awaitable[None]] | None = None
        self._channel = ""
        self._chat_id = ""

    def set_on_change(self, callback: Callable[[str, dict], Awaitable[None]]) -> None:
        self._on_change = callback

    def set_watch_hook(self, callback: Callable[[str], Awaitable[None]]) -> None:
        """Register an async ``(flowlet_id) -> None`` invoked after the agent
        mutates a flowlet's state, so reactive watches fire promptly (a goal it
        just logged for you celebrates now, not at the next heartbeat)."""
        self._watch_hook = callback

    def set_context(self, channel: str, chat_id: str) -> None:
        """Record the current chat so a created flowlet knows where an
        ``agent``-action reply should land. Wired per-message by the agent loop."""
        self._channel = channel or ""
        self._chat_id = chat_id or ""

    def _origin_session(self, kw: dict) -> str | None:
        # Prefer the live per-message context; fall back to an explicit
        # session_key kwarg then None.
        if self._channel and self._chat_id:
            return f"{self._channel}:{self._chat_id}"
        return kw.get("session_key")

    @property
    def name(self) -> str:
        return "flowlet"

    @property
    def description(self) -> str:
        return (
            "Build and maintain flowlets — personal, persistent mini-screens the "
            "user controls on Desktop and iOS (a water tracker, a habit grid, a "
            "mood log). A flowlet is a declarative JSON `definition` written "
            "against the component catalog; it renders natively and stays in sync "
            "across the user's devices. Read the `flowlets` skill for the full "
            "catalog and worked examples before authoring one.\n\n"
            "Actions:\n"
            "- create: Create a flowlet from a `definition` object.\n"
            "- update: Replace a flowlet's `definition` (versioned) or set `pinned`.\n"
            "- get: Get one flowlet's definition AND its current live values "
            "(use this to answer questions like 'how much water today?').\n"
            "- list: List the user's flowlets with their current values.\n"
            "- delete: Delete a flowlet permanently.\n"
            "- log: Append a data point to a series (e.g. the user tells you they "
            "drank 500ml) — updates every connected screen.\n"
            "- set_state: Set a state value (e.g. change a goal).\n"
            "- query: Aggregate a series (agg over a window) to answer a question.\n\n"
            "The user taps buttons/sliders themselves — you do NOT relay those; "
            "they are applied instantly without you. Use `log`/`set_state` only "
            "when the user tells YOU something in chat."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["create", "update", "get", "list",
                             "delete", "log", "set_state", "query", "notify"],
                    "description": "The action to perform",
                },
                "flowlet_id": {
                    "type": "string",
                    "description": "Target flowlet id (all actions except create/list)",
                },
                "definition": {
                    "type": "object",
                    "description": (
                        "The full flowlet definition (catalog, name, icon, accent, "
                        "state, series, computed, layout). Required for create; for "
                        "update it replaces the definition and bumps the version."
                    ),
                },
                "pinned": {
                    "type": "boolean",
                    "description": "Pin/unpin (update action)",
                },
                "series": {"type": "string", "description": "Series name (log/query)"},
                "key": {"type": "string", "description": "State key (set_state)"},
                "title": {"type": "string", "description": "Notification title (notify)"},
                "body": {"type": "string", "description": "Notification body (notify)"},
                "value": {
                    "description": "Value to log / set (number for log, any for set_state)",
                },
                "agg": {
                    "type": "string",
                    "enum": ["sum", "count", "avg", "min", "max", "last"],
                    "description": "Aggregation for query (default sum)",
                },
                "window": {
                    "type": "string",
                    "enum": ["today", "7d", "30d", "90d", "all"],
                    "description": "Time window for query (default today)",
                },
            },
            "required": ["action"],
        }

    async def execute(self, action: str = "", **kwargs: Any) -> str:
        handlers = {
            "create": self._create,
            "update": self._update,
            "get": self._get,
            "list": self._list,
            "delete": self._delete,
            "log": self._log,
            "set_state": self._set_state,
            "query": self._query,
            "notify": self._notify_action,
        }
        handler = handlers.get(action)
        if not handler:
            return json.dumps({"error": f"Unknown action: {action}. Valid: {list(handlers)}"})
        try:
            return await handler(**kwargs)
        except FlowletValidationError as exc:
            # Surface the precise, fixable message so the model can correct it.
            return json.dumps({"error": f"invalid definition: {exc}", "action": action})
        except Exception as exc:  # noqa: BLE001
            logger.error("Flowlet {} error: {}", action, exc)
            return json.dumps({"error": str(exc), "action": action})

    # ── values helper ─────────────────────────────────────────────────────────

    def _values(self, flowlet: dict) -> dict:
        return queries.resolve_values(
            flowlet["definition"],
            self._store.get_state(flowlet["id"]),
            self._store.get_events(flowlet["id"]),
            now_ms(),
            None,  # local tz
        )

    @staticmethod
    def _review(definition: dict) -> dict:
        """A create/update self-check the agent reads back: deterministic lint
        findings + a preview of the flowlet resolved against sample rows. Both
        best-effort — a review must never fail the authoring call."""
        review: dict = {}
        try:
            from flowly.flowlets.lint import lint_definition
            findings = lint_definition(definition)
            if findings:
                review["lint"] = findings
        except Exception as exc:  # noqa: BLE001
            logger.debug("flowlet lint failed: {}", exc)
        try:
            from flowly.flowlets.synth import preview_values
            pv = _compact_preview(preview_values(definition, now_ms(), None))
            if pv:
                review["preview"] = pv
        except Exception as exc:  # noqa: BLE001
            logger.debug("flowlet preview failed: {}", exc)
        return review

    # ── actions ───────────────────────────────────────────────────────────────

    async def _create(self, **kw: Any) -> str:
        definition = kw.get("definition")
        if not isinstance(definition, dict):
            return json.dumps({"error": "definition (object) is required"})
        # Forgotten ids are ASSIGNED, not rejected — "button carries an action,
        # so it needs a unique `id`" was a whole authoring-failure class, and an
        # id-less chart silently rendered empty. Persist the assigned ids.
        from flowly.flowlets.normalize import assign_missing_ids
        definition = assign_missing_ids(definition)
        validate_definition(definition)
        meta = _extract_meta(definition)
        flowlet = self._store.create(
            name=meta["name"],
            definition=definition,
            icon=meta["icon"],
            accent=meta["accent"],
            catalog=meta["catalog"],
            pinned=bool(kw.get("pinned", False)),
            origin_session=self._origin_session(kw),
        )
        values = self._values(flowlet)
        await self._notify("flowlet.created", _summary(flowlet, values))
        return json.dumps({
            "action": "create",
            "flowlet": _summary(flowlet, values),
            "message": f"Flowlet '{meta['name']}' created (id: {flowlet['id']})",
            **self._review(definition),
        })

    async def _update(self, **kw: Any) -> str:
        flowlet_id = kw.get("flowlet_id", "")
        if not flowlet_id:
            return json.dumps({"error": "flowlet_id is required"})
        if not self._store.get(flowlet_id):
            return json.dumps({"error": f"Flowlet not found: {flowlet_id}"})

        definition = kw.get("definition")
        name = icon = accent = None
        if definition is not None:
            if not isinstance(definition, dict):
                return json.dumps({"error": "definition must be an object"})
            from flowly.flowlets.normalize import assign_missing_ids
            definition = assign_missing_ids(definition)
            validate_definition(definition)
            meta = _extract_meta(definition)
            name, icon, accent = meta["name"], meta["icon"], meta["accent"]

        flowlet = self._store.update(
            flowlet_id,
            name=name,
            icon=icon,
            accent=accent,
            definition=definition,
            pinned=kw.get("pinned"),
        )
        values = self._values(flowlet)
        await self._notify("flowlet.updated", _summary(flowlet, values))
        return json.dumps({
            "action": "update",
            "flowlet": _summary(flowlet, values),
            "message": f"Flowlet updated (v{flowlet['version']})",
            **(self._review(definition) if definition is not None else {}),
        })

    async def _get(self, **kw: Any) -> str:
        flowlet_id = kw.get("flowlet_id", "")
        flowlet = self._store.get(flowlet_id)
        if not flowlet:
            return json.dumps({"error": f"Flowlet not found: {flowlet_id}"})
        values = self._values(flowlet)
        return json.dumps({
            "action": "get",
            "flowlet": {
                "id": flowlet["id"],
                "name": flowlet["name"],
                "definition": flowlet["definition"],
                "values": values,
            },
        })

    async def _list(self, **kw: Any) -> str:
        rows = self._store.list(limit=int(kw.get("limit", 50) or 50))
        out = []
        for f in rows:
            try:
                out.append(_summary(f, self._values(f)))
            except Exception:
                out.append(_summary(f))
        return json.dumps({"action": "list", "count": len(out), "flowlets": out})

    async def _delete(self, **kw: Any) -> str:
        flowlet_id = kw.get("flowlet_id", "")
        if not self._store.delete(flowlet_id):
            return json.dumps({"error": f"Flowlet not found: {flowlet_id}"})
        await self._notify("flowlet.deleted", {"id": flowlet_id})
        return json.dumps({"action": "delete", "deleted": True,
                           "message": f"Flowlet {flowlet_id} deleted"})

    async def _log(self, **kw: Any) -> str:
        flowlet_id = kw.get("flowlet_id", "")
        flowlet = self._store.get(flowlet_id)
        if not flowlet:
            return json.dumps({"error": f"Flowlet not found: {flowlet_id}"})
        series = kw.get("series")
        declared = (flowlet["definition"].get("series") or {})
        if series not in declared:
            return json.dumps({"error": f"series '{series}' is not declared in this flowlet"})
        try:
            value = float(kw.get("value", 1))
        except (TypeError, ValueError):
            return json.dumps({"error": "value must be a number"})
        self._store.add_event(flowlet_id, series, value)
        values = self._values(flowlet)
        _ev = {"id": flowlet_id, "values": values}
        _pv = queries.flowlet_preview(flowlet["definition"], values)
        if _pv is not None:
            _ev["preview"] = _pv
        await self._notify("flowlet.state", _ev)
        return json.dumps({"action": "log", "flowletId": flowlet_id, "values": values})

    async def _set_state(self, **kw: Any) -> str:
        flowlet_id = kw.get("flowlet_id", "")
        flowlet = self._store.get(flowlet_id)
        if not flowlet:
            return json.dumps({"error": f"Flowlet not found: {flowlet_id}"})
        key = kw.get("key")
        spec = (flowlet["definition"].get("state") or {}).get(key)
        if not isinstance(spec, dict):
            return json.dumps({"error": f"state key '{key}' is not declared"})
        self._store.set_state(flowlet_id, key, queries.coerce_state(kw.get("value"), spec))
        values = self._values(flowlet)
        _ev = {"id": flowlet_id, "values": values}
        _pv = queries.flowlet_preview(flowlet["definition"], values)
        if _pv is not None:
            _ev["preview"] = _pv
        await self._notify("flowlet.state", _ev)
        return json.dumps({"action": "set_state", "flowletId": flowlet_id, "values": values})

    async def _query(self, **kw: Any) -> str:
        flowlet_id = kw.get("flowlet_id", "")
        flowlet = self._store.get(flowlet_id)
        if not flowlet:
            return json.dumps({"error": f"Flowlet not found: {flowlet_id}"})
        series = kw.get("series")
        if series not in (flowlet["definition"].get("series") or {}):
            return json.dumps({"error": f"series '{series}' is not declared"})
        events = [e for e in self._store.get_events(flowlet_id) if e["series"] == series]
        result = queries.aggregate_scalar(
            events, kw.get("agg", "sum"), kw.get("window", "today"), now_ms(), None,
        )
        return json.dumps({
            "action": "query", "flowletId": flowlet_id, "series": series,
            "agg": kw.get("agg", "sum"), "window": kw.get("window", "today"),
            "result": result,
        })

    async def _notify_action(self, **kw: Any) -> str:
        """Send a reminder notification that deep-links to a flowlet — APNs/FCM
        to mobile, a native notification on desktop. Use from a cron job (or
        directly) to nudge the user about a screen."""
        flowlet_id = kw.get("flowlet_id", "")
        flowlet = self._store.get(flowlet_id)
        if not flowlet:
            return json.dumps({"error": f"Flowlet not found: {flowlet_id}"})
        title = str(kw.get("title") or flowlet.get("name") or "Flowlet")
        body = str(kw.get("body") or "")
        from flowly.push.flowlet_push import notify_flowlet
        await notify_flowlet(flowlet_id, title, body, broadcast=self._on_change)
        return json.dumps({
            "action": "notify", "flowletId": flowlet_id, "sent": True,
            "message": f"Reminder sent for '{flowlet.get('name')}'",
        })

    # ── broadcast ─────────────────────────────────────────────────────────────

    async def _notify(self, event_name: str, data: dict) -> None:
        if self._on_change:
            try:
                await self._on_change(event_name, data)
            except Exception as exc:  # noqa: BLE001
                logger.debug("Flowlet broadcast error: {}", exc)
        # A state change may satisfy a reactive watch — evaluate it now.
        if event_name == "flowlet.state" and self._watch_hook:
            fid = data.get("id")
            if fid:
                try:
                    await self._watch_hook(str(fid))
                except Exception as exc:  # noqa: BLE001
                    logger.debug("Flowlet watch hook error: {}", exc)
