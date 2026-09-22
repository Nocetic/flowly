"""Bounded, content-free diagnostics for the shared Voice RPC facade."""
from __future__ import annotations

import json
import re
import time
from typing import Any

from loguru import logger

_UUID = re.compile(r"^[a-f0-9]{8}-(?:[a-f0-9]{4}-){3}[a-f0-9]{12}$", re.I)
_CODE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_METHODS = frozenset({
    'voice.open', 'voice.end', 'voice.delete', 'voice.tasks.dispatch',
    'voice.tasks.prepare', 'voice.tasks.steer', 'voice.tasks.cancel',
    'voice.tasks.respond', 'voice.chats.open', 'voice.notice',
})


def voice_rpc_diagnostic(method: str, params: dict[str, Any], started: float,
                         outcome: str, reason_code: str | None = None) -> None:
    if method not in _METHODS:
        return
    try:
        connection_id = params.get('connectionId')
        run_id = params.get('voiceRunId') if method == 'voice.open' else None
        row: dict[str, Any] = {
            'event': 'live_voice_stage', 'component': 'core', 'version': 1,
            'stage': 'core_rpc', 'method': method, 'outcome': outcome,
            'durationMs': max(0, min(300_000, round((time.monotonic() - started) * 1000))),
        }
        if isinstance(connection_id, str) and _UUID.fullmatch(connection_id):
            row['connectionId'] = connection_id.lower()
        if isinstance(run_id, str) and _UUID.fullmatch(run_id):
            row['runId'] = run_id.lower()
        if isinstance(reason_code, str) and _CODE.fullmatch(reason_code):
            row['reasonCode'] = reason_code
        logger.info('Live Voice diagnostic {}', json.dumps(row, separators=(',', ':')))
    except Exception:
        pass  # Logging must never alter an RPC result.
