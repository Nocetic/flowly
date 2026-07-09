"""Live data sources — bring the outside world onto a flowlet.

A definition's ``sources`` object declares named bindings. The bot resolves each
on a schedule and writes the result into a *source-owned* state key (a scalar or
a ``list``), which components render like any other value. This is what turns a
flowlet from "a screen the user fills" into "a live window onto their world"
(a repo's commits, a calendar, a metric).

Phase 2 ships the ``agent`` kind: a model turn (the same privilege as a cron
self-prompt) fetches the data with the agent's own tools and returns JSON that
matches the target's schema. It is throttled (a refresh floor + failure
backoff), and the detail screen lists every source in plain language so the user
always sees what's being fetched. Direct ``tool`` (LLM-free) and ``device``
(HealthKit) kinds land in Phase 3.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from datetime import tzinfo
from typing import Any, Awaitable, Callable

from loguru import logger

from flowly.flowlets import catalog
from flowly.flowlets.queries import coerce_state, flowlet_preview, render_template, resolve_values
from flowly.flowlets.store import FlowletStore
from flowly.flowlets.store import now_ms as _now_ms

AgentSourceRunner = Callable[[dict, str], Awaitable[str | None]]

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_BACKOFF_CAP_MS = 6 * 3600 * 1000


def _parse_refresh_minutes(v: Any) -> int | None:
    if not isinstance(v, str):
        return None
    m = re.match(r"^\s*(\d+)\s*([mh])\s*$", v)
    if not m:
        return None
    n = int(m.group(1))
    return n if m.group(2) == "m" else n * 60


# ── JSON extraction + coercion (external data is messy — be lenient) ──────────

def _extract_json(text: str) -> Any:
    """Pull a JSON payload out of an agent reply — tolerating code fences and a
    little surrounding prose. Raises ValueError if nothing parses."""
    t = (text or "").strip()
    if t.startswith("```"):
        parts = t.split("```")
        if len(parts) >= 2:
            t = parts[1]
            if t.lstrip().lower().startswith("json"):
                t = t.lstrip()[4:]
            t = t.strip()
    for opener, closer in (("[", "]"), ("{", "}")):
        i = t.find(opener)
        j = t.rfind(closer)
        if 0 <= i < j:
            try:
                return json.loads(t[i : j + 1])
            except ValueError:
                pass
    return json.loads(t)  # a bare scalar ("23.5" / a quoted string)


def _coerce_field(ftype: str, v: Any) -> Any:
    if v is None:
        return None
    if ftype == "string":
        return str(v).strip()[: catalog.MAX_STRING_INPUT] or None
    if ftype == "number":
        if isinstance(v, bool):
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None
    if ftype == "bool":
        if isinstance(v, bool):
            return v
        return str(v).strip().lower() in ("true", "1", "yes", "evet")
    if ftype == "date":
        s = str(v).strip()[:10]
        return s if _DATE_RE.match(s) else None
    return None


def _coerce_item(raw: Any, fields: dict) -> dict | None:
    if not isinstance(raw, dict):
        return None
    out: dict[str, Any] = {}
    for f, ftype in fields.items():
        if f in raw:
            cv = _coerce_field(ftype, raw[f])
            if cv is not None:
                out[f] = cv
    if not out:
        return None
    out["id"] = f"src_{os.urandom(4).hex()}"
    return out


def _coerce_into(into_spec: dict, data: Any, limit: int | None) -> Any:
    """Shape a parsed JSON payload to the target state key's type."""
    if into_spec.get("type") == "list":
        fields = into_spec.get("item") or {}
        rows = data if isinstance(data, list) else []
        items = [it for it in (_coerce_item(r, fields) for r in rows) if it]
        cap = min(int(limit) if limit else catalog.MAX_LIST_ITEMS, catalog.MAX_LIST_ITEMS)
        return items[:cap]
    # scalar target
    return coerce_state(data, into_spec)


def _build_prompt(flowlet: dict, sid: str, spec: dict, into_spec: dict, values: dict) -> str:
    name = flowlet.get("name") or "Flowlet"
    task = render_template(spec.get("prompt", ""), values)
    header = (
        f"[Flowlet data source — {sid} · {name}]\n"
        f"Fetch data for this panel of the user's '{name}' screen and return it as "
        "JSON — nothing else. Use your tools to get real, current data.\n\n"
        f"What to fetch: {task}\n\n"
    )
    if into_spec.get("type") == "list":
        fields = into_spec.get("item") or {}
        cols = ", ".join(f"{f}: {t}" for f, t in fields.items())
        limit = spec.get("limit") or catalog.MAX_LIST_ITEMS
        return (
            header
            + f"Return ONLY a JSON array (max {limit} items) of objects with these fields: "
            + "{" + cols + "}. "
            + 'string → text, number → a number, bool → true/false, date → "YYYY-MM-DD". '
            + "No prose, no markdown fences — just the JSON array. If there's nothing, return []."
        )
    return (
        header
        + f"Return ONLY a single JSON {into_spec.get('type', 'value')} value — no prose, "
        + "no fences."
    )


# ── Engine ────────────────────────────────────────────────────────────────────


class SourceEngine:
    """Refreshes flowlet data sources and writes their snapshots into state.

    Serialised with an :class:`asyncio.Lock` so the heartbeat and an on-open /
    manual refresh never resolve the same source twice at once. Runs the model
    turn + writes + broadcast outside no lock beyond the store's own.
    """

    def __init__(
        self,
        store: FlowletStore,
        *,
        broadcast: Callable[[str, dict], Awaitable[None]] | None = None,
        agent_runner: AgentSourceRunner | None = None,
        tz: tzinfo | None = None,
    ) -> None:
        self._store = store
        self._broadcast = broadcast
        self._agent = agent_runner
        self._tz = tz
        self._lock = asyncio.Lock()

    async def refresh_all(self, *, now_ms: int | None = None) -> int:
        """Heartbeat pass: refresh every due source across all flowlets."""
        done = 0
        for fl in self._store.list(limit=500):
            if not (fl.get("definition") or {}).get("sources"):
                continue
            try:
                done += await self._refresh_flowlet(fl, now_ms=now_ms, force=False, on_open=False)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[flowlet] source pass failed for {}: {}", fl.get("id"), exc)
        return done

    async def refresh_flowlet(self, flowlet_id: str, *, force: bool = False) -> int:
        """A client opened the screen (force=False → due + first-load) or tapped
        refresh (force=True → all sources now)."""
        fl = self._store.get(flowlet_id)
        if not fl:
            return 0
        return await self._refresh_flowlet(fl, now_ms=None, force=force, on_open=not force)

    async def _refresh_flowlet(
        self, flowlet: dict, *, now_ms: int | None, force: bool, on_open: bool
    ) -> int:
        defn = flowlet.get("definition") or {}
        sources = defn.get("sources") or {}
        if not sources:
            return 0
        fid = flowlet["id"]
        now = now_ms if now_ms is not None else _now_ms()
        done = 0
        async with self._lock:
            ss_all = self._store.get_source_state(fid)
            due = [
                (sid, spec)
                for sid, spec in sources.items()
                if force or self._is_due(spec, ss_all.get(sid, {}), now, on_open)
            ]
        for sid, spec in due:
            if await self._resolve(flowlet, sid, spec, now):
                done += 1
        return done

    @staticmethod
    def _is_due(spec: dict, ss: dict, now: int, on_open: bool) -> bool:
        refresh = spec.get("refresh", "manual")
        never = ss.get("last_ok_ms") is None
        if refresh == "manual":
            # A manual source auto-loads once (on first open); after that it only
            # refreshes on an explicit tap.
            return on_open and never
        interval = (_parse_refresh_minutes(refresh) or catalog.SOURCE_DEFAULT_REFRESH_MIN) * 60_000
        fail = ss.get("fail_count") or 0
        if fail > 0:
            backoff = min(interval * (2 ** min(fail, 6)), _BACKOFF_CAP_MS)
            return now - (ss.get("last_err_ms") or 0) >= backoff
        if never:
            return True
        return now - ss["last_ok_ms"] >= interval

    async def _resolve(self, flowlet: dict, sid: str, spec: dict, now: int) -> bool:
        if self._agent is None:
            return False
        fid = flowlet["id"]
        defn = flowlet["definition"]
        into = spec.get("into")
        into_spec = (defn.get("state") or {}).get(into) or {}
        try:
            values = resolve_values(
                defn, self._store.get_state(fid), self._store.get_events(fid), now, self._tz
            )
            prompt = _build_prompt(flowlet, sid, spec, into_spec, values)
            reply = await self._agent(flowlet, prompt)
            written = _coerce_into(into_spec, _extract_json(reply or ""), spec.get("limit"))
        except Exception as exc:  # noqa: BLE001 — keep stale data, back off, log
            fail = (self._store.get_source_state(fid).get(sid, {}).get("fail_count") or 0) + 1
            self._store.set_source_state(
                fid, sid, last_err_ms=now, fail_count=fail, last_error=str(exc)[:200]
            )
            logger.warning("[flowlet] source '{}' on {} failed: {}", sid, fid, exc)
            return False

        self._store.set_state(fid, into, written)
        self._store.set_source_state(fid, sid, last_ok_ms=now, fail_count=0, last_error=None)
        logger.debug("[flowlet] source '{}' refreshed on {}", sid, fid)

        if self._broadcast is not None:
            fresh = resolve_values(
                defn, self._store.get_state(fid), self._store.get_events(fid), _now_ms(), self._tz
            )
            data = {"id": fid, "values": fresh}
            preview = flowlet_preview(defn, fresh)
            if preview is not None:
                data["preview"] = preview
            try:
                await self._broadcast("flowlet.state", data)
            except Exception as exc:  # noqa: BLE001
                logger.debug("[flowlet] source broadcast skipped: {}", exc)
        return True

    async def run_heartbeat(
        self, *, interval_s: int = 120, stop_event: asyncio.Event | None = None
    ) -> None:
        """Tick every ``interval_s`` refreshing due sources until cancelled."""
        logger.info("[flowlet] source heartbeat started (every {}s)", interval_s)
        try:
            while not (stop_event is not None and stop_event.is_set()):
                try:
                    n = await self.refresh_all()
                    if n:
                        logger.debug("[flowlet] source heartbeat refreshed {}", n)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("[flowlet] source heartbeat error: {}", exc)
                if stop_event is not None:
                    try:
                        await asyncio.wait_for(stop_event.wait(), timeout=interval_s)
                        break
                    except asyncio.TimeoutError:
                        pass
                else:
                    await asyncio.sleep(interval_s)
        except asyncio.CancelledError:
            pass
        logger.info("[flowlet] source heartbeat stopped")
