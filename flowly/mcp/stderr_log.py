"""Per-transport stderr capture; no child ever receives a raw log-file handle.

The SDK needs a real OS descriptor. A bounded pipe reader frames and redacts
records before handing them to the diagnostic writer. Each capture retains its
own bounded sanitized excerpt, so concurrent server output cannot be confused.
"""

from __future__ import annotations

import json
import os
import re
import select
import threading
import time
from collections import deque
from typing import Any

from flowly.mcp.security import _sensitive_key

MAX_STDERR_RECORD_BYTES = 64 * 1024
_PEM_START = re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----")
_PEM_END = re.compile(r"-----END (?:[A-Z0-9]+ )*PRIVATE KEY-----")
_PENDING_FIELD = re.compile(r"""([\w-]+)["']?\s*[:=]\s*$""")


class StderrCapture:
    """One pipe and reader thread per stdio transport; fail closed on overflow."""

    def __init__(self, diagnostics: Any):
        self._diagnostics = diagnostics
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._recent: deque[str] = deque(maxlen=16)
        self._recent_lock = threading.Lock()
        self._line = bytearray()
        self._oversized_line = False
        self._record = ""
        self._record_kind = ""
        self._disabled = False
        read = write = None
        try:
            if os.name != "posix":
                raise OSError("Nonblocking pipe capture is unavailable")
            read, write = os.pipe()
            os.set_blocking(read, False)
            self.file = os.fdopen(write, "wb", buffering=0)
            write = None
            self._read_fd = read
            self._thread = threading.Thread(target=self._run, name="mcp-stderr-reader", daemon=True)
            self._thread.start()
        except (OSError, RuntimeError):
            if read is not None:
                os.close(read)
            if write is not None:
                os.close(write)
            if hasattr(self, "file"):
                self.file.close()
            self._thread = None
            # Never fall back to sys.stderr. If even devnull cannot open,
            # abort the spawn instead of giving the child an unsafe descriptor.
            self.file = open(os.devnull, "wb", buffering=0)
            self._emit("Error: secure stderr capture unavailable; subprocess diagnostics discarded")

    def _emit(self, data: Any):
        text = self._diagnostics.emit("warning", "stderr", data, source="stderr")
        if text is not None:
            with self._recent_lock:
                self._recent.append(text)

    def _flush_record(self):
        if self._record:
            self._emit(self._record)
        self._record = self._record_kind = ""

    def _on_line(self, line: str):
        if self._disabled:
            return
        if self._record_kind:
            self._record += line + "\n"
            if len(self._record.encode("utf-8")) > MAX_STDERR_RECORD_BYTES:
                # A malformed/oversized multiline secret cannot be safely
                # resynchronized by assuming the next newline ends its value.
                self._disabled = True
                self._record = self._record_kind = ""
                self._emit("Error: stderr multiline record exceeded the size limit; remaining stream discarded")
                return
            if self._record_kind == "pem":
                if _PEM_END.search(line):
                    self._flush_record()
                return
            if self._record_kind == "json":
                try:
                    json.loads(self._record)
                except (ValueError, RecursionError):
                    return
            elif self._record_kind == "field":
                value = self._record.split("\n", 1)[1].strip()
                if value.startswith('"'):
                    try:
                        json.loads(value.rstrip(",;"))
                    except (ValueError, RecursionError):
                        return
                elif value.startswith("'") and not re.fullmatch(r"'(?:[^'\\]|\\.)*'\s*[,;]?", value, flags=re.S):
                    return
            self._flush_record()
            return
        if _PEM_START.search(line):
            self._record, self._record_kind = line + "\n", "pem"
            if _PEM_END.search(line):
                self._flush_record()
            return
        stripped = line.strip()
        if stripped in {"{", "["} or re.match(r'^\{\s*"|^\[\s*["{]', stripped):
            try:
                json.loads(stripped)
            except (ValueError, RecursionError):
                self._record, self._record_kind = line + "\n", "json"
                return
        match = _PENDING_FIELD.search(line)
        if match and _sensitive_key(match[1]):
            self._record, self._record_kind = line + "\n", "field"
            return
        self._emit(line)

    def _feed(self, chunk: bytes):
        if self._disabled:
            return
        parts = chunk.split(b"\n")
        for index, part in enumerate(parts):
            if not self._oversized_line:
                if len(self._line) + len(part) > MAX_STDERR_RECORD_BYTES:
                    self._line.clear()
                    self._oversized_line = True
                else:
                    self._line.extend(part)
            if index == len(parts) - 1:
                break
            if self._oversized_line:
                if self._record_kind:
                    self._disabled = True
                    self._record = self._record_kind = ""
                self._emit("Error: stderr line exceeded the size limit and was discarded")
            else:
                self._on_line(self._line.decode("utf-8", errors="replace"))
            self._line.clear()
            self._oversized_line = False

    def _run(self):
        deadline = None
        clean_eof = False
        try:
            while True:
                if self._stop.is_set():
                    if deadline is None:
                        deadline = time.monotonic() + 0.2
                    if time.monotonic() >= deadline:
                        break
                readable, _, _ = select.select([self._read_fd], [], [], 0.02)
                if not readable:
                    continue
                try:
                    chunk = os.read(self._read_fd, 8192)
                except BlockingIOError:
                    continue
                if not chunk:
                    clean_eof = True
                    break
                self._feed(chunk)
        except (OSError, ValueError):
            self._emit("Error: stderr capture failed; remaining diagnostics discarded")
        finally:
            os.close(self._read_fd)
            if clean_eof and not self._disabled:
                if self._oversized_line:
                    self._emit("Error: stderr line exceeded the size limit and was discarded")
                elif self._line:
                    self._on_line(self._line.decode("utf-8", errors="replace"))
                self._flush_record()
            elif self._line or self._record or self._oversized_line:
                self._emit("Error: incomplete stderr tail discarded during bounded shutdown")
            self._line.clear()
            self._record = ""

    def excerpt(self) -> str:
        with self._recent_lock:
            return "\n".join(self._recent)

    def close(self):
        self.file.close()
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1)


def summarize_stderr_excerpt(text: str) -> str:
    """Return one safe, actionable startup diagnostic from stderr."""
    if not text:
        return ""

    # mcp-remote emits this shape when the supplied URL is a registry/server
    # manifest rather than the actual JSON-RPC endpoint. This was previously
    # hidden behind a generic TaskGroup error and then a fake 300 s timeout.
    if (
        "ZodError" in text
        and '"$schema"' in text
        and '"remotes"' in text
        and "Invalid input" in text
    ):
        return (
            "the URL returned an MCP manifest instead of JSON-RPC; "
            "use the endpoint in remotes[].url"
        )

    candidates: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if (
            not line
            or line.startswith("at ")
            or line.startswith("=====")
            or line == "Shutting down..."
        ):
            continue
        lowered = line.lower()
        if any(
            marker in lowered
            for marker in (
                "connection error",
                "error:",
                "failed",
                "unauthorized",
                "forbidden",
                "econnrefused",
                "enotfound",
            )
        ):
            candidates.append(line)

    if not candidates:
        return ""

    from flowly.mcp.security import sanitize_error

    return sanitize_error(candidates[-1])[:500]
