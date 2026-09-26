"""Bounded, nonblocking handoff for content-free Voice diagnostic rows."""
from __future__ import annotations

import json
import queue
import threading
import time

from loguru import logger

_QUEUE: queue.Queue[str] = queue.Queue(maxsize=256)
_LOCK = threading.Lock()
_STARTED = False
_DROPPED = 0
_HANDED = 0
_FAILED = 0
_OVERSIZE = 0
_DIRTY = False
_DIRTY_SEQ = 0
_MAX = 2_147_483_647


def health() -> dict[str, int]:
    with _LOCK:
        return {'pending': _QUEUE.qsize(), 'dropped': _DROPPED, 'handedToLogger': _HANDED,
                'loggerCallErrors': _FAILED, 'oversize': _OVERSIZE}


def _run() -> None:
    global _HANDED, _FAILED, _DIRTY, _DIRTY_SEQ
    last_health = time.monotonic()
    while True:
        try:
            payload = _QUEUE.get(timeout=1)
        except queue.Empty:
            payload = None
        if payload is not None:
            try:
                logger.info('Live Voice diagnostic {}', payload)
                with _LOCK:
                    _HANDED = min(_MAX, _HANDED + 1)
            except Exception:
                with _LOCK:
                    _FAILED = min(_MAX, _FAILED + 1)
                    _DIRTY = True
                    _DIRTY_SEQ += 1
            finally:
                _QUEUE.task_done()
        now = time.monotonic()
        if now - last_health >= 60 and _DIRTY:
            last_health = now
            try:
                with _LOCK:
                    dirty_seq = _DIRTY_SEQ
                row = {'event': 'live_voice_telemetry_health', 'component': 'core', **health()}
                logger.info('Live Voice diagnostic {}', json.dumps(row, separators=(',', ':')))
                with _LOCK:
                    if _DIRTY_SEQ == dirty_seq:
                        _DIRTY = False
            except Exception:
                pass  # Health logging cannot recurse into the queue.


def emit_voice_diagnostic(row: dict) -> None:
    """Never wait on a sink; full queues drop metadata rather than delaying Voice."""
    global _STARTED, _DROPPED, _OVERSIZE, _DIRTY, _DIRTY_SEQ
    try:
        if not _STARTED:
            with _LOCK:
                if not _STARTED:
                    threading.Thread(target=_run, name='voice-diagnostics', daemon=True).start()
                    _STARTED = True
        payload = json.dumps(row, separators=(',', ':'))
        if len(payload.encode('utf-8')) > 4096:
            with _LOCK:
                _OVERSIZE = min(_MAX, _OVERSIZE + 1)
                _DIRTY = True
                _DIRTY_SEQ += 1
            return
        _QUEUE.put_nowait(payload)
    except queue.Full:
        with _LOCK:
            _DROPPED = min(_MAX, _DROPPED + 1)
            _DIRTY = True
            _DIRTY_SEQ += 1
    except Exception:
        pass  # Telemetry cannot change a Voice operation.
