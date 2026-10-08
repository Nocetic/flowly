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
from flowly.flowlets.authoring import (
    definition_parameters,
    guide,
    repair_definition,
    structural_errors,
)
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


_FIX_HINT = ("Fix every listed problem in ONE retry. flowlet(action='guide') has the "
             "contract; template(template_id) gives a valid starting point.")


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
            "Build and maintain flowlets: personal, persistent mini-screens the user "
            "controls on every device (a water tracker, a habit grid, a calorie "
            "journal). One call makes a working screen; no skill, file read or shell "
            "command is needed.\n\n"
            "STANDARD: create(template_id, lang, pinned). Templates: water, habits, "
            "expenses, tasks, sleep, mood, meals (photo calorie journal). Requested "
            "reminders go in the SAME call: watches=[{preset:'daily_summary'}] "
            "(ready-made, localized; add at:'21:30' to move it). `templates` lists "
            "each template's reminders.\n\n"
            "CUSTOM: create(definition), written straight from this contract:\n"
            "- {catalog:3, name, icon?, accent?, locale?:'en'|'tr'|'es', state, series?, "
            "computed?, layout:[...], screens?, watches?}\n"
            "- state: {goal:{type:'number',default:2000}, rows:{type:'list',item:"
            "{name:'string',kcal:'number',date:'date',shot:'image'}}}; types number|bool|"
            "string|timer|list; a list holds at most 200 rows.\n"
            "- computed: {total:{list:'rows',field:'kcal',agg:'sum',where:"
            "'days_since(date)==0'}}, {left:{expr:'max(0, goal-total)'}}, {mood:{cases:"
            "[{when:'total>goal',text:'Over by {left}'}],else:'On track'}}; a series "
            "aggregate is {series:'water',agg:'sum',window:'today'}.\n"
            "- Binds are key strings (stat/progress value:'total', max:'goal'); text "
            "interpolates {key}.\n"
            "- Entry: form{id,into:'rows',fields:[{field:'name'},{field:'date',default:"
            "'today'}]}; photo{id,label,action:{op:'vision',into:'rows',prompt,"
            "dateDefaults:['date']}}; button{id,text,action:{op:'increment',key,by} or "
            "{op:'log',series,value}}.\n"
            "- Lists: repeater{source:'rows',item:{type:'list_row',title:'$.name',value:"
            "'{$.kcal} kcal',thumb:'$.shot'}}; tracker_card{id,list:'rows',field:'kcal',"
            "window:'7d',chart:'bar'}.\n"
            "- Reminders: watches:[{id,trigger:'schedule',at:'21:00',when?:'total>0',"
            "notify:{title,body:'{total} today'},otherwise?:{title,body}}]; trigger "
            "'condition'/'goal' take when (+after:'18:00'), 'stale' takes idleMinutes. "
            "Times are the host's local time.\n"
            "- The screen is already titled with name: don't open with a header repeating it.\n"
            "Every problem is reported at once: fix them all in one retry. Slips with "
            "one meaning are repaired and listed under `normalized`. guide returns the "
            "full component catalog; template(template_id) returns an editable example.\n\n"
            "EXISTING: list/get are the inventory, never memory. update replaces the "
            "definition (versioned) or sets pinned.\n\n"
            "Actions: create, update, get (definition + live values, e.g. 'how much "
            "water today?'), list, delete, log (append to a series when the user tells "
            "you a value), set_state, query (aggregate a series), notify, templates, "
            "template, guide, validate (dry run; create/update already validate).\n"
            "The user's own taps apply instantly without you; use log/set_state only "
            "for what the user tells YOU in chat."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["create", "update", "get", "list",
                             "delete", "log", "set_state", "query", "notify",
                             "templates", "template", "guide", "validate"],
                    "description": "The action to perform",
                },
                "flowlet_id": {
                    "type": "string",
                    "description": "Target flowlet id for update/get/delete/log/set_state/query/notify",
                },
                "definition": definition_parameters(),
                "template_id": {"type": "string", "description":
                    "Template for create/template: water, habits, expenses, tasks, sleep, mood, meals."},
                "lang": {"type": "string", "description": "Template UI language: en, tr or es."},
                "name": {"type": "string", "description": "Optional name for template creation."},
                "watches": {"type": "array", "items": {"type": "object"}, "description":
                    "create: requested reminders only. A template's ready-made one by "
                    "name {preset:'daily_summary'} (optionally with at:'21:30'), or a full "
                    "watch object. Joins a custom definition's own watches."},
                "pinned": {
                    "type": "boolean",
                    "description": "Pin at creation or pin/unpin on update",
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
            "templates": self._templates,
            "template": self._template,
            "guide": self._guide,
            "validate": self._validate,
        }
        handler = handlers.get(action)
        if not handler:
            return json.dumps({"error": f"Unknown action: {action}. Valid: {list(handlers)}"})
        try:
            return await handler(**kwargs)
        except FlowletValidationError as exc:
            # Surface every precise, fixable problem so one retry fixes them all.
            return json.dumps({"error": "invalid definition", "errors": exc.errors,
                               "action": action, "hint": _FIX_HINT}, ensure_ascii=False)
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

    @staticmethod
    def _prepare(definition: Any) -> tuple[dict | None, list[str], str | None]:
        """Repair unambiguous slips, then validate everything at once.

        Returns ``(definition, notes, None)`` when it is ready to persist, or
        ``(None, notes, error_json)`` listing every problem found.
        """
        if not isinstance(definition, dict):
            return None, [], json.dumps({"error": "definition (object) is required"})
        definition, notes = repair_definition(definition)
        errors = [f"{e['path']}: {e['message']}" for e in structural_errors(definition)]
        if not errors:
            from flowly.flowlets.normalize import assign_missing_ids
            definition = assign_missing_ids(definition)
            try:
                validate_definition(definition)
            except FlowletValidationError as exc:
                errors = exc.errors
        if errors:
            out: dict[str, Any] = {"error": "invalid definition", "errors": errors,
                                   "hint": _FIX_HINT}
            if notes:
                out["normalized"] = notes
            return None, notes, json.dumps(out, ensure_ascii=False)
        return definition, notes, None

    # ── actions ───────────────────────────────────────────────────────────────

    async def _guide(self, **kw: Any) -> str:
        return json.dumps(guide(), ensure_ascii=False)

    async def _templates(self, **kw: Any) -> str:
        from flowly.flowlets.templates import list_templates
        return json.dumps({"templates": list_templates(kw.get("lang"))}, ensure_ascii=False)

    async def _template(self, **kw: Any) -> str:
        from flowly.flowlets.templates import (
            build_template,
            describe_reminder,
            list_templates,
            template_reminders,
        )
        try:
            definition = build_template(kw.get("template_id"), kw.get("lang"))
        except KeyError:
            return json.dumps({"error": "Unknown template_id", "templates": list_templates(kw.get("lang"))})
        # Conversational creation only includes reminders the user requested;
        # the ready-made ones are offered by id instead.
        definition.pop("watches", None)
        out: dict[str, Any] = {"definition": definition}
        reminders = template_reminders(kw.get("template_id"), kw.get("lang"))
        if reminders:
            out["reminders"] = {rid: describe_reminder(w) for rid, w in reminders.items()}
        return json.dumps(out, ensure_ascii=False)

    @staticmethod
    def _resolve_watches(requested: Any, presets: dict[str, dict]) -> tuple[list | None, str | None]:
        """Turn ``watches`` entries into watch objects: a preset id
        (``"daily_summary"``), a preset with overrides
        (``{"preset": "daily_summary", "at": "21:30"}``) or a full watch."""
        if not isinstance(requested, list):
            return None, "watches must be an array"
        out: list = []
        for w in requested:
            name = w if isinstance(w, str) else (w.get("preset") if isinstance(w, dict) else None)
            if name is not None:
                base = presets.get(name)
                if base is None:
                    return None, (f"Unknown reminder {name!r}. Available: {sorted(presets) or 'none'}; "
                                  "or pass a full watch object.")
                overrides = {k: v for k, v in w.items() if k != "preset"} if isinstance(w, dict) else {}
                if "everyMinutes" in overrides:
                    base = {k: v for k, v in base.items() if k != "at"}
                out.append({**base, **overrides})
            else:
                out.append(w)
        return out, None

    async def _validate(self, **kw: Any) -> str:
        definition, notes, error = self._prepare(kw.get("definition"))
        if error:
            return error
        out: dict[str, Any] = {"valid": True, **self._review(definition)}
        if notes:
            out["normalized"] = notes
        return json.dumps(out, ensure_ascii=False)

    async def _create(self, **kw: Any) -> str:
        definition = kw.get("definition")
        if kw.get("template_id"):
            if definition is not None:
                return json.dumps({"error": "Supply template_id OR definition, not both"})
            result = json.loads(await self._template(**kw))
            if "error" in result:
                return json.dumps(result)
            definition = result["definition"]
            if kw.get("name"):
                definition["name"] = kw["name"]
            if kw.get("watches"):
                from flowly.flowlets.templates import template_reminders
                watches, err = self._resolve_watches(
                    kw["watches"], template_reminders(kw["template_id"], kw.get("lang")))
                if err:
                    return json.dumps({"error": err})
                definition["watches"] = watches
        elif kw.get("watches"):
            # Same call either way: reminders passed beside a custom definition
            # join its own watches instead of costing a retry.
            if not isinstance(definition, dict):
                return json.dumps({"error": "definition (object) is required"})
            watches, err = self._resolve_watches(kw["watches"], {})
            if err:
                return json.dumps({"error": err})
            definition = {**definition,
                          "watches": [*(definition.get("watches") or []), *watches]}
        # Unambiguous slips are repaired (and reported), forgotten ids are
        # ASSIGNED, and every remaining problem is returned at once.
        definition, notes, error = self._prepare(definition)
        if error:
            return error
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
            **({"normalized": notes} if notes else {}),
            **self._review(definition),
        }, ensure_ascii=False)

    async def _update(self, **kw: Any) -> str:
        flowlet_id = kw.get("flowlet_id", "")
        if not flowlet_id:
            return json.dumps({"error": "flowlet_id is required"})
        if not self._store.get(flowlet_id):
            return json.dumps({"error": f"Flowlet not found: {flowlet_id}"})

        definition = kw.get("definition")
        name = icon = accent = None
        notes: list[str] = []
        if definition is not None:
            definition, notes, error = self._prepare(definition)
            if error:
                return error
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
            **({"normalized": notes} if notes else {}),
            **(self._review(definition) if definition is not None else {}),
        }, ensure_ascii=False)

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
