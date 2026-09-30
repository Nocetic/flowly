"""Request-bound answers in the worker process; no new agent turn is created."""
from __future__ import annotations

import hashlib
import json
import re
import time

from flowly.live_voice.sessions import VoiceError, bounded_text
from flowly.session.commands import validate_chat_target


def _scope(params: dict) -> tuple[str, str]:
    validate_chat_target(params)
    session_key = bounded_text(params.get('sessionKey'), 'sessionKey', maximum=200)
    run_id = bounded_text(params.get('runId'), 'runId', maximum=256)
    if not re.fullmatch(r'desktop:voice-work:[A-Za-z0-9_.:-]{1,128}', session_key):
        raise VoiceError('INVALID_PARAMS', 'A voice work conversation is required.')
    if not params.get('expectedBotId'):
        raise VoiceError('INVALID_PARAMS', 'The selected agent identity is required.')
    return session_key, run_id


def _request_wire(kind: str, pending) -> dict:
    from flowly.clarify.wire import clarify_to_wire
    from flowly.exec.wire import approval_to_wire

    wire = (clarify_to_wire if kind == 'clarify' else approval_to_wire)(pending)
    wire.update({'requestType': kind, 'runId': pending.run_id})
    wire['requestRevision'] = hashlib.sha256(json.dumps(
        wire, sort_keys=True, separators=(',', ':'), allow_nan=False,
    ).encode()).hexdigest()
    return wire


def _managers():
    from flowly.clarify.manager import get_clarify_manager
    from flowly.exec.approval_manager import get_approval_manager

    return {'clarify': get_clarify_manager(), 'approval': get_approval_manager()}


def pending_requests(params: dict) -> dict:
    session_key, run_id = _scope(params)
    requests = []
    for kind, manager in _managers().items():
        for pending in manager.list_pending():
            if pending.session_key == session_key and pending.run_id == run_id:
                future = manager._futures.get(pending.id)
                if future is not None and not future.done():
                    requests.append(_request_wire(kind, pending))
    return {'sessionKey': session_key, 'runId': run_id, 'requests': requests[:20]}


def respond(params: dict) -> dict:
    session_key, run_id = _scope(params)
    request_id = bounded_text(params.get('requestId'), 'requestId', maximum=128)
    revision = bounded_text(params.get('requestRevision'), 'requestRevision', maximum=64)
    kind = params.get('requestType')
    manager = _managers().get(kind) if isinstance(kind, str) else None
    if manager is None:
        raise VoiceError('INVALID_PARAMS', 'Unknown request type.')
    pending = manager.get_pending(request_id)
    if (pending is None or pending.expires_at <= time.time() or pending.session_key != session_key
            or pending.run_id != run_id or _request_wire(kind, pending)['requestRevision'] != revision):
        raise VoiceError('STALE_REQUEST', 'This request changed, expired, or was already answered.')
    if kind == 'approval':
        answer = params.get('decision')
        # A voice answer grants one action. Persistent permission belongs to
        # the existing explicit controls in the full work conversation.
        if answer not in {'allow-once', 'deny'}:
            raise VoiceError('INVALID_PARAMS', 'Choose allow-once or deny for this request.')
    else:
        answer = bounded_text(params.get('answer'), 'answer', maximum=4000)
    # Validation and Future resolution are synchronous in this worker loop;
    # another device cannot replace the request between these operations.
    if not manager.resolve(request_id, answer):
        raise VoiceError('STALE_REQUEST', 'This request was already answered.')
    return {'status': 'applied', 'requestId': request_id, 'requestRevision': revision, 'runId': run_id}
