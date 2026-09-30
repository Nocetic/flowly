"""Third-party ``logging`` into the gateway's log, minus the known noise.

Libraries (aiohttp, slack_sdk, httpx, …) log through the stdlib. The gateway
used to send that to stderr only (``logging.basicConfig``), so none of it
reached ``gateway.log`` — the file Settings → Logs reads — while a public
gateway's stderr filled with tracebacks nobody could act on.

Every stdlib record now goes to loguru, keeping its logger name, function and
line, so it lands in the same file and format as Flowly's own lines. Two kinds
of record are turned down first, because they are expected and say nothing
new each time:

- **Malformed requests from the internet.** A gateway on a public port is
  probed by scanners (HTTP/2 prefaces, TLS on a plain port, garbage). aiohttp
  rejects each one and logs a full traceback at ERROR. They become one INFO
  line per window: how many were rejected and the latest source.
- **A messaging channel's socket dropping.** slack_sdk logs ERROR every time
  Slack closes its socket-mode connection, then reconnects on its own. That is
  a WARNING line saying so, with no traceback.

Everything else passes through at its own level, exceptions included.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from typing import Any

from loguru import logger

REJECTED_WINDOW_SECONDS = 600
_PEER = re.compile(r"from (?P<ip>[0-9a-fA-F:.]+)")
_LEVELS = {logging.CRITICAL: "CRITICAL", logging.ERROR: "ERROR", logging.WARNING: "WARNING",
           logging.INFO: "INFO", logging.DEBUG: "DEBUG"}


def _malformed_request(record: logging.LogRecord) -> bool:
    """aiohttp's own rejection of a request it could not parse."""
    if not record.name.startswith("aiohttp.server") or not record.exc_info:
        return False
    try:
        from aiohttp.http_exceptions import HttpProcessingError
    except Exception:  # noqa: BLE001 — without aiohttp there is nothing to match
        return False
    error = record.exc_info[1]
    return isinstance(error, HttpProcessingError)


def _channel_dropped(record: logging.LogRecord) -> str | None:
    """The messaging channel whose socket closed and will reconnect, if any."""
    if record.name.startswith("slack_sdk.socket_mode") and "ConnectionClosed" in record.getMessage():
        return "Slack"
    return None


class LoguruBridge(logging.Handler):
    """A stdlib handler that writes into loguru."""

    def __init__(self) -> None:
        super().__init__()
        self._lock = threading.Lock()
        self._rejected = 0
        self._rejected_since = 0.0

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if _malformed_request(record):
                self._note_rejected(record)
                return
            channel = _channel_dropped(record)
            if channel:
                self._write(record, "WARNING", f"{channel} connection dropped; reconnecting on its own.")
                return
            level = _LEVELS.get(record.levelno, record.levelname)
            self._write(record, level, record.getMessage(), exc_info=record.exc_info)
        except Exception:  # noqa: BLE001 — logging must never raise into a library
            self.handleError(record)

    def _note_rejected(self, record: logging.LogRecord) -> None:
        match = _PEER.search(record.getMessage())
        ip = match.group("ip") if match else "unknown"
        with self._lock:
            now = time.monotonic()
            self._rejected += 1
            if self._rejected_since and now - self._rejected_since < REJECTED_WINDOW_SECONDS:
                return
            count, self._rejected, self._rejected_since = self._rejected, 0, now
        self._write(record, "INFO", f"Rejected {count} malformed request(s) from the internet; latest from {ip}. "
                                    "Usually a scanner probing the open port; nothing reached the agent.")

    @staticmethod
    def _write(record: logging.LogRecord, level: str, message: str, exc_info: Any = None) -> None:
        def origin(entry: dict[str, Any]) -> None:
            entry.update(name=record.name, function=record.funcName, line=record.lineno)

        bound = logger.patch(origin).opt(exception=exc_info or None)
        try:
            bound.log(level, message)
        except ValueError:  # a custom level loguru does not know
            bound.log("INFO", message)


_installed: LoguruBridge | None = None


def install(level: int = logging.WARNING) -> LoguruBridge:
    """Route the stdlib's root logger through the bridge. Idempotent."""
    global _installed
    root = logging.getLogger()
    if _installed is None:
        _installed = LoguruBridge()
    root.handlers = [handler for handler in root.handlers if not isinstance(handler, LoguruBridge)]
    root.addHandler(_installed)
    root.setLevel(level)
    return _installed
