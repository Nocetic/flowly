"""The gateway log as events an owner can read.

Reading
    ``read_tail`` returns only *complete* lines: a line still being written
    stays for the next read, so a poller never sees half a line and then its
    remainder as a separate one. The cursor is ``(file_id, offset)``; another
    file id (rotation) or an offset past the end (truncation) starts over from
    the tail.

Parsing
    Two line shapes occur: loguru's (``2026-10-01 00:13:12.974 | INFO     |
    module:function:line - message``), which every gateway writes, and the
    stdlib default (``ERROR:aiohttp.server:message``) that older installs left
    in their stderr capture. Anything else continues the previous event — a
    traceback or a multi-line message — and becomes its ``detail``.

Classifying
    Lines written through ``notable`` (lifecycle moments and known problems)
    are recognised from the same templates that wrote them; a few library
    lines are recognised by pattern. Each gets a stable ``code`` with its
    ``params``, which clients explain in plain words. ``notable`` marks what
    an owner should see by default: those codes, and every warning or error.
    ``signature`` identifies "the same thing again", so clients collapse
    repeats even across polls.

Serving
    Messages, details and params pass through ``redact``: credentials, the
    home directory and full identifiers do not leave the machine.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from flowly.gateway_logs.files import current_log_file, file_id
from flowly.gateway_logs.notable import NOTABLE, PATTERNS
from flowly.gateway_logs.redact import redact

TAIL_BYTES = 256 * 1024
MESSAGE_MAX = 600
DETAIL_MAX = 8_000
LEVELS = ("debug", "info", "warning", "error", "critical")
ISSUE_LEVELS = frozenset({"warning", "error", "critical"})

_LOGURU = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:\.\d{1,6})?)\s*\|\s*(?P<level>[A-Z]+)\s*\|\s*"
    r"(?P<source>[\w.<>-]+):(?P<function>[\w<>.-]*):(?P<line>\d+)\s+-\s(?P<message>.*)$"
)
_STDLIB = re.compile(r"^(?P<level>DEBUG|INFO|WARNING|ERROR|CRITICAL):(?P<source>[\w.]+):(?P<message>.*)$")
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_LEVEL_ALIASES = {"trace": "debug", "success": "info", "warn": "warning", "fatal": "critical"}


@dataclass
class Tail:
    lines: list[tuple[int, str]]  # (byte offset of the line, text)
    cursor: int
    file_id: str
    available: bool
    reset: bool


def read_tail(cursor: int | None, known_file: str | None, *, path: Path | None = None) -> Tail:
    """Complete lines after ``cursor`` in the current log file."""
    path = path or current_log_file()
    if path is None:
        return Tail([], 0, "", False, False)
    try:
        identity = file_id(path)
        size = path.stat().st_size
        reset = cursor is None or known_file != identity or cursor > size
        start = max(0, size - TAIL_BYTES) if reset else cursor
        with path.open("rb") as handle:
            handle.seek(start)
            chunk = handle.read(size - start)
    except OSError:
        return Tail([], 0, "", False, False)
    # A tail that starts mid-file begins mid-line: drop that fragment.
    if reset and start > 0:
        newline = chunk.find(b"\n")
        if newline < 0:
            return Tail([], start, identity, True, True)
        start += newline + 1
        chunk = chunk[newline + 1:]
    # Only complete lines; the one still being written waits for the next read.
    end = chunk.rfind(b"\n")
    if end < 0:
        return Tail([], start, identity, True, reset)
    lines: list[tuple[int, str]] = []
    offset = start
    for raw in chunk[: end + 1].split(b"\n")[:-1]:
        text = _ANSI.sub("", raw.decode("utf-8", errors="replace")).rstrip("\r")
        if text.strip():
            lines.append((offset, text))
        offset += len(raw) + 1
    return Tail(lines, start + end + 1, identity, True, reset)


def _timestamp(value: str) -> int | None:
    try:
        seconds, _, fraction = value.partition(".")
        base = time.mktime(time.strptime(seconds, "%Y-%m-%d %H:%M:%S"))
        return int(base * 1000) + int((fraction or "0")[:3].ljust(3, "0"))
    except (ValueError, OverflowError):
        return None


def _level(value: str) -> str:
    level = value.strip().lower()
    level = _LEVEL_ALIASES.get(level, level)
    return level if level in LEVELS else "info"


def _cap(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


# ── What a line means ────────────────────────────────────────────────────

# Lines Flowly does not write through ``notable`` — a library's, or an older
# Flowly's — that still mean something specific.
_IP = r"(?P<ip>[0-9a-fA-F:.]+)"
_FOREIGN: tuple[tuple[str, re.Pattern[str], tuple[str, ...]], ...] = (
    ("net.rejected_request", re.compile(r"^Error handling request from " + _IP), ("ip",)),
    ("channel.reconnecting", re.compile(r"^Failed to receive or enqueue a message: ConnectionClosed"), ()),
    ("mcp.disconnected",
     re.compile(r"^MCP server '(?P<server>[^']+)' disconnected: (?P<reason>.*?)"
                r"(?:; (?:reconnecting|probing parked server) in [\d.]+s)?$"),
     ("server", "reason")),
)
_SOURCE_DEFAULTS = {"slack_sdk": {"channel": "Slack"}}
NOTABLE_CODES = frozenset({*NOTABLE, *(code for code, _pattern, _keys in _FOREIGN)})
# Identifiers that differ between otherwise identical messages.
_VOLATILE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|\b\d+(?:\.\d+)*\b|\b[sS]_\d+\b|0x[0-9a-f]+",
    re.IGNORECASE,
)
# What makes two occurrences "the same thing": the channel, server or routine,
# never a count, an address or a reason.
_SIGNATURE_PARAMS = {"channel", "server", "name"}


def classify(source: str, message: str) -> tuple[str | None, dict[str, str]]:
    for code, pattern in PATTERNS:
        match = pattern.match(message)
        if match:
            return code, {key: _cap(value, 120) for key, value in match.groupdict().items()}
    for code, pattern, keys in _FOREIGN:
        match = pattern.search(message)
        if match:
            params = {key: _cap(match.group(key), 120) for key in keys if match.group(key)}
            for prefix, defaults in _SOURCE_DEFAULTS.items():
                if source.startswith(prefix):
                    for key, value in defaults.items():
                        params.setdefault(key, value)
            return code, params
    return None, {}


def _signature(level: str, source: str, code: str | None, params: dict[str, str], message: str) -> str:
    if code:
        stable = ",".join(f"{key}={params[key]}" for key in sorted(params) if key in _SIGNATURE_PARAMS)
        return f"{code}|{stable}"
    return f"{level}|{source}|{_VOLATILE.sub('#', message)[:200]}"


def parse(lines: list[tuple[int, str]], identity: str) -> list[dict[str, Any]]:
    """Events, oldest first; continuation lines join the event before them."""
    events: list[dict[str, Any]] = []
    detail: list[str] = []

    def close() -> None:
        if events and detail:
            events[-1]["detail"] = _cap("\n".join(detail), DETAIL_MAX)
        detail.clear()

    for offset, text in lines:
        match = _LOGURU.match(text) or _STDLIB.match(text)
        if match is None:
            if events:
                detail.append(text)
            continue
        close()
        groups = match.groupdict()
        source = groups["source"]
        message = groups["message"].strip()
        level = _level(groups["level"])
        code, params = classify(source, message)
        events.append({
            "id": f"{identity}@{offset}",
            "ts": _timestamp(groups["ts"]) if groups.get("ts") else None,
            "level": level,
            "source": source,
            "message": message,
            **({"code": code, "params": params} if code else {}),
        })
    close()
    home = str(Path.home())
    for event in events:
        # A traceback line can carry the real reason; let the rules see it too.
        if "code" not in event and event.get("detail"):
            code, params = classify(event["source"], event["detail"].splitlines()[-1].strip())
            if code:
                event.update({"code": code, "params": params})
        event["notable"] = event.get("code") in NOTABLE_CODES or event["level"] in ISSUE_LEVELS
        # Nothing secret or personal leaves in what is served — the signature
        # included, so it is derived again from the redacted text.
        message = redact(event["message"], home)
        event["message"] = _cap(message, MESSAGE_MAX)
        if event.get("detail"):
            event["detail"] = redact(event["detail"], home)
        if event.get("params"):
            event["params"] = {key: redact(value, home) for key, value in event["params"].items()}
        event["signature"] = _signature(event["level"], event["source"], event.get("code"), event.get("params") or {}, message)
    return events


def collapse(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Repeats of the same thing become one event with ``count``, kept at the
    position of the latest one, carrying the first and last time."""
    merged: dict[str, dict[str, Any]] = {}
    for event in events:
        # Popping and re-inserting moves the group to the latest position.
        seen = merged.pop(event["signature"], None)
        merged[event["signature"]] = {
            **event,
            "count": seen["count"] + 1 if seen else 1,
            "firstTs": seen["firstTs"] if seen else event.get("ts"),
        }
    return list(merged.values())
