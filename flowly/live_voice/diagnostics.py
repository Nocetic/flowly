"""Bounded, content-free diagnostics for the shared Voice RPC facade."""
from __future__ import annotations

import json
import re
import time
from typing import Any

from loguru import logger

_UUID = re.compile(r"^[a-f0-9]{8}-(?:[a-f0-9]{4}-){3}[a-f0-9]{12}$", re.I)
_CODE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_OPERATION = re.compile(r"^(?:[a-f0-9]{64}|[a-f0-9]{8}-(?:[a-f0-9]{4}-){3}[a-f0-9]{12})$", re.I)
_METHODS = frozenset({
    'voice.open', 'voice.end', 'voice.delete', 'voice.tasks.dispatch',
    'voice.tasks.prepare', 'voice.tasks.steer', 'voice.tasks.cancel',
    'voice.tasks.respond', 'voice.chats.open', 'voice.notice',
})


def records_voice_method(method: str) -> bool:
    return method in _METHODS


def voice_rpc_diagnostic(method: str, params: dict[str, Any], started: float,
                         outcome: str, reason_code: str | None = None,
                         binding: dict[str, str] | None = None) -> None:
    if method not in _METHODS:
        return
    try:
        connection_id = (binding or {}).get('connectionId')
        run_id = (binding or {}).get('runId')
        row: dict[str, Any] = {
            'event': 'live_voice_stage', 'component': 'core', 'version': 1,
            'stage': 'core_rpc', 'method': method, 'outcome': outcome,
            'durationMs': max(0, min(300_000, round((time.monotonic() - started) * 1000))),
        }
        if isinstance(connection_id, str) and _UUID.fullmatch(connection_id):
            row['connectionId'] = connection_id.lower()
        if isinstance(run_id, str) and _UUID.fullmatch(run_id):
            row['runId'] = run_id.lower()
        diagnostic = params.get('_voiceDiagnostic')
        operation_id = diagnostic.get('operationId') if isinstance(diagnostic, dict) else None
        if connection_id and isinstance(operation_id, str) and _OPERATION.fullmatch(operation_id):
            row['operationId'] = operation_id.lower()
        row['correlation'] = 'session_verified' if run_id and connection_id else 'unbound'
        if isinstance(reason_code, str) and _CODE.fullmatch(reason_code):
            row['reasonCode'] = reason_code
        logger.info('Live Voice diagnostic {}', json.dumps(row, separators=(',', ':')))
    except Exception:
        pass  # Logging must never alter an RPC result.
