"""Argument coercion helpers for tool parameters.

Small language models — particularly Claude Haiku — occasionally emit tool
arguments in shapes that don't match the declared JSON schema. The most
common failure mode is passing an array-typed parameter as a JSON-encoded
*string* rather than a real array, especially when the array contents
include Windows-style file paths whose backslashes confuse the model's
argument emission.

Observed in production (2026-04 Windows regression):

    expected: media_paths = ["C:\\Users\\foo\\bar.png"]
    actual  : media_paths = "[\"C:\\\\Users\\\\foo\\\\bar.png\"]"

This module provides defensive coercion that repairs these shapes without
touching well-formed inputs. **It is a no-op for any value that's already a
list or None**, so well-behaved agents and platforms where the model
consistently emits correct schemas (macOS in practice) see zero behavioural
change.
"""

from __future__ import annotations

import json
import re
from typing import Any

from loguru import logger


def coerce_str_list(
    value: Any,
    *,
    param_name: str = "value",
    tool_name: str | None = None,
) -> list[str] | None:
    """Coerce a list-or-None-or-string argument to list[str] or None.

    Happy paths (no work done, returned as-is):
      - None  → None
      - list  → the same list, shallow-copied and stringified per element

    Recovery paths (model-emission bug, coerced with a WARN log):
      - "[\"a\", \"b\"]"                 → ["a", "b"]              (valid JSON)
      - "[\"C:\\\\a\\\\b.png\"]"         → ["C:\\a\\b.png"]        (valid JSON with escaped backslashes)
      - "[\"C:\\a\\b.png\"]"             → ["C:\\a\\b.png"]        (invalid JSON, Windows raw backslashes → re-escape + retry)
      - "[\"a.png\", \"b.png\"]" malformed → ["a.png", "b.png"]   (last-resort manual split on commas)
      - "/single/path.png"               → ["/single/path.png"]   (bare string path)
      - ""                               → None                    (empty string)

    This is deliberately permissive. Callers pass the result through their
    own validators (file exists, size cap, mime check, etc.), so garbage
    still gets rejected downstream — coercion only tries to recover what
    LOOKS like the agent's intent.

    Args:
        value: The argument value as it arrived from the tool dispatcher.
        param_name: For diagnostic logs. E.g. "media_paths".
        tool_name: For diagnostic logs. E.g. "message".

    Returns:
        list[str] | None — ready to iterate safely.
    """
    if value is None:
        return None

    # Pass-through for well-formed lists. CRITICAL: this is the macOS-typical
    # path where the model emits a real JSON array. Must not be altered.
    if isinstance(value, list):
        return [str(item) for item in value]

    # Non-string, non-list, non-None — silently wrap as a single-element list
    # so we don't crash downstream. Extremely rare but defensive.
    if not isinstance(value, str):
        return [str(value)]

    stripped = value.strip()
    if not stripped:
        return None

    # If it doesn't look like an array literal, treat the whole string as
    # one path. Agents sometimes pass `media_paths: "/tmp/a.png"` instead of
    # `media_paths: ["/tmp/a.png"]`.
    if not (stripped.startswith("[") and stripped.endswith("]")):
        _log_coercion(tool_name, param_name, "bare-string", stripped)
        return [stripped]

    # ── Stringified array: try strict JSON first. ─────────────────────────
    # When backslashes are already properly escaped (e.g. the model did emit
    # \\) this succeeds cleanly.
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, list):
        _log_coercion(tool_name, param_name, "json-array-string", stripped)
        return [str(item) for item in parsed]

    # ── Retry by doubling EVERY backslash and parsing. ────────────────────
    # Haiku (on Windows) routinely emits raw path backslashes like
    # `C:\Users\...` inside stringified arrays. These are a mix of:
    #   - invalid JSON escapes (\U, \H, \P) → json.loads raises
    #   - valid JSON escapes (\b, \f, \n, \r, \t, \v) → json.loads SILENTLY
    #     turns them into control characters, corrupting the path
    # A selective re-escape that only fixes invalids would still corrupt the
    # valid-escape cases (e.g. `D:\bin\...` losing the \b). Since a legitimate
    # control character in a filename is semantically nonsense, we just
    # double every backslash. Safe because this branch only runs AFTER the
    # properly-escaped strict-JSON attempt above has already failed — a
    # well-formed `["C:\\\\Users\\\\..."]` succeeds there and never gets here.
    try:
        doubled = stripped.replace("\\", "\\\\")
        parsed = json.loads(doubled)
        if isinstance(parsed, list):
            _log_coercion(tool_name, param_name, "windows-backslash-repaired", stripped)
            return [str(item) for item in parsed]
    except json.JSONDecodeError:
        pass

    # ── Last resort: manual split. ────────────────────────────────────────
    # Strip [ ], split on commas, strip surrounding quotes from each part.
    # Good enough for "[\"a\", \"b\"]" shaped inputs that don't round-trip
    # through either JSON attempt.
    inner = stripped[1:-1].strip()
    parts: list[str] = []
    for part in inner.split(","):
        part = part.strip().strip('"').strip("'")
        if part:
            parts.append(part)
    if parts:
        _log_coercion(tool_name, param_name, "manual-split-fallback", stripped)
        return parts

    # Nothing recognisable — wrap original string. Downstream validators
    # will reject it with a clear error.
    _log_coercion(tool_name, param_name, "unparseable-wrapped", stripped)
    return [stripped]


def coerce_int(value: Any, *, default: int = 0) -> int:
    """Coerce an integer-typed tool argument to int, falling back to default.

    Models occasionally emit integer parameters as strings ("500") or floats
    (500.0). Anything unparseable returns ``default`` — callers treat the
    default as "parameter not provided".
    """
    if value is None:
        return default
    if isinstance(value, bool):  # bool is an int subclass; reject explicitly
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return default
    return default


def _log_coercion(tool_name: str | None, param_name: str, recovery: str, raw: str) -> None:
    """Surface a structured warning so we can monitor how often this fires.

    Production telemetry idea: counter on (tool_name, recovery) to quantify
    model-emission regressions over time. For now a loguru WARN line is
    enough — a log aggregator can derive the rate.
    """
    preview = raw[:120] + ("..." if len(raw) > 120 else "")
    logger.warning(
        "[arg_coercion] Recovered stringified array parameter "
        f"tool={tool_name or '?'} param={param_name} recovery={recovery} "
        f"raw={preview!r}"
    )
