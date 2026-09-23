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
_TOOL_READS = frozenset({'voice.tasks.events', 'voice.tasks.get', 'voice.tasks.requests', 'voice.focus'})


def records_voice_method(method: str, params: dict | None = None) -> bool:
    return method in _METHODS or (method in _TOOL_READS and isinstance(params, dict) and isinstance(params.get('_voiceDiagnostic'), dict))


def voice_rpc_diagnostic(method: str, params: dict[str, Any], started: float,
                         outcome: str, reason_code: str | None = None,
                         binding: dict[str, str] | None = None, *, stage: str = 'core_rpc',
                         finished: float | None = None) -> None:
    if stage not in {'core_rpc', 'core_auth', 'core_handler'} or not records_voice_method(method, params):
        return
    try:
        connection_id = (binding or {}).get('connectionId')
        run_id = (binding or {}).get('runId')
        row: dict[str, Any] = {
            'event': 'live_voice_stage', 'component': 'core', 'version': 1,
            'stage': stage, 'method': method, 'outcome': outcome,
            'durationMs': max(0, min(300_000, round(((finished if finished is not None else time.monotonic()) - started) * 1000))),
        }
        if isinstance(connection_id, str) and _UUID.fullmatch(connection_id):
            row['connectionId'] = connection_id.lower()
        if isinstance(run_id, str) and _UUID.fullmatch(run_id):
            row['runId'] = run_id.lower()
        session_ref = (binding or {}).get('sessionRef')
        if isinstance(session_ref, str) and re.fullmatch(r'[a-f0-9]{64}', session_ref):
            row['sessionRef'] = session_ref
        run_ref = (binding or {}).get('runRef')
        if isinstance(run_ref, str) and re.fullmatch(r'[a-f0-9]{64}', run_ref):
            row['runRef'] = run_ref
        diagnostic = params.get('_voiceDiagnostic')
        operation_id = diagnostic.get('operationId') if isinstance(diagnostic, dict) else None
        if 'connectionId' in row and isinstance(operation_id, str) and _OPERATION.fullmatch(operation_id):
            row['operationId'] = operation_id.lower()
        row['correlation'] = ('session_verified' if 'sessionRef' in row else 'host_verified') if 'runId' in row and 'connectionId' in row else 'unbound'
        if isinstance(reason_code, str) and _CODE.fullmatch(reason_code):
            row['reasonCode'] = reason_code
        logger.info('Live Voice diagnostic {}', json.dumps(row, separators=(',', ':')))
    except Exception:
        pass  # Logging must never alter an RPC result.
