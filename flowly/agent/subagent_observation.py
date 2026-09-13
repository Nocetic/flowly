"""Versioned task snapshots and bounded, best-effort live delivery.

Snapshots remain authoritative after disconnect, queue overflow or timeout.
No provider response waits for an observer; progress never includes tool args.
"""
from __future__ import annotations

import asyncio
import time
from collections import Counter, OrderedDict
from copy import deepcopy
from typing import Any, Awaitable, Callable

from loguru import logger

from flowly.agent.subagent_registry import SubagentRunRecord


def event_version(params: dict) -> int:
    """Optional, explicit negotiation; unknown versions never select v2."""
    version = params.get("eventVersion", 1)
    if type(version) is not int or version not in (1, 2):
        raise ValueError("eventVersion must be 1 or 2")
    return version


def client_event(event: str, data: dict, version: int = 1) -> tuple[str, dict]:
    """Keep the installed desktop's short-ID lifecycle contract by default.

    Legacy listeners subscribe to every `subagent.*` event and treat any
    nonterminal payload as a start. Progress therefore projects to an
    idempotent legacy start, even when coalescing replaced the original start.
    Never mutate the shared v2 snapshot while preparing another client's frame.
    """
    if version == 2 or data.get("schemaVersion") != 2:
        return event, data
    run_id = str(data.get("runId") or "")[:8]
    delegate = data.get("kind") == "delegate"
    agent_id = data.get("agentId")
    label = data.get("label") or ""
    outcome = data.get("outcome")
    if outcome or event == "subagent.completed":
        legacy = {"runId": run_id, "label": f"@{agent_id}" if delegate else label,
                  "outcome": outcome, "running": data.get("running", 0)}
        if not delegate:
            legacy.update(error=data.get("error"), toolTrace=data.get("toolTrace", []))
        return "subagent.completed", legacy
    task = str(data.get("task") or "")
    legacy = {"runId": run_id, "label": f"@{agent_id}: {task[:60]}" if delegate else label,
              "task": task[:200], "model": data.get("model")}
    if delegate:
        legacy["running"] = data.get("running", 0)
    return "subagent.started", legacy


def run_view(record: SubagentRunRecord, *, detail: bool = False) -> dict[str, Any]:
    counts = Counter(step.get("tool", "unknown") for step in record.tool_trace)
    duration = None
    if record.started_at is not None:
        end = record.ended_at if record.ended_at is not None else time.time()
        duration = round(max(0, end - record.started_at), 1)
    view = {
        "schemaVersion": 2, "runId": record.run_id, "shortId": record.run_id[:8],
        "label": record.display_name or record.label, "task": record.task,
        "model": record.model, "kind": record.kind, "agentId": record.agent_id,
        "status": "running" if record.ended_at is None else (record.outcome or "unknown"),
        "duration": duration, "createdAt": record.created_at,
        "startedAt": record.started_at, "endedAt": record.ended_at,
        "updatedAt": record.updated_at or record.ended_at or record.created_at,
        "revision": record.revision, "parentSessionKey": record.parent_session_key,
        "activity": deepcopy(record.activity), "error": record.error, "errorCode": record.error_code,
        "toolsUsed": [{"name": name, "count": count} for name, count in counts.items()],
        "toolCount": len(record.tool_trace),
        "completedToolCount": sum(t.get("status") != "running" for t in record.tool_trace),
        "resultPreview": record.result_preview, "resultChars": record.result_chars,
        "resultAvailable": record.result_available, "artifactIds": list(record.artifact_ids),
        "deliveryState": record.delivery_state,
    }
    if detail:
        view["toolTrace"] = deepcopy(record.tool_trace)
    return view


class TaskEvents:
    def __init__(self, send: Callable[[str, dict], Awaitable[None]], *, capacity: int = 128,
                 timeout: float = 2.0) -> None:
        self._send = send
        self._capacity = capacity
        self._timeout = timeout
        self._pending: OrderedDict[str, tuple[str, dict]] = OrderedDict()
        self._worker: asyncio.Task | None = None

    def publish(self, event: str, data: dict) -> None:
        key = data["runId"]
        prior = self._pending.get(key)
        if prior and prior[1]["revision"] >= data["revision"]:
            return
        self._pending[key] = (event, deepcopy(data))
        if len(self._pending) > self._capacity:
            self._pending.popitem(last=False)
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._drain())

    async def _drain(self) -> None:
        while self._pending:
            _, (event, data) = self._pending.popitem(last=False)
            try:
                await asyncio.wait_for(self._send(event, data), timeout=self._timeout)
            except Exception:
                logger.debug("Task observer unavailable; next snapshot will reconcile")

    async def flush(self) -> None:
        if self._worker:
            await self._worker
