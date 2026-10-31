"""Hide this agent's saved secrets in anything about to leave a command.

The vault knows the exact values, so nothing is guessed from patterns: each
value, and the forms a command commonly prints it in (base64, base64url, URL
encoding), becomes ``[secret:NAME]``. Values shorter than
:data:`MIN_REDACTED_CHARS` are left alone; hiding every short word that
happens to equal one would garble ordinary output and hides little.

Output is often cut before it is shown (the last 1000 characters, the first
N): a cut can fall inside a value and leave part of it at the start or end of
the text. Such a part, :data:`MIN_EDGE_CHARS` or longer, is hidden too.

Applied where a command's output turns into something the model, the
conversation, the server or a log sees (``ToolRegistry.execute``, background
process notices, gateway logs).
"""

from __future__ import annotations

import base64
import re
import threading
from urllib.parse import quote

MIN_REDACTED_CHARS = 8
#: The shortest part of a value, cut at the text's start or end, that is hidden.
MIN_EDGE_CHARS = 6


def _forms(value: str) -> set[str]:
    raw = value.encode("utf-8")
    forms = {
        value,
        base64.b64encode(raw).decode("ascii").rstrip("="),
        base64.urlsafe_b64encode(raw).decode("ascii").rstrip("="),
        quote(value, safe=""),
    }
    return {form for form in forms if len(form) >= MIN_REDACTED_CHARS}


class Redactor:
    """Replaces known values; built from ``{name: value}``."""

    def __init__(self, values: dict[str, str]) -> None:
        replacements: dict[str, str] = {}
        for name in sorted(values):
            value = values[name]
            if not isinstance(value, str) or len(value) < MIN_REDACTED_CHARS:
                continue
            for form in _forms(value):
                replacements.setdefault(form, f"[secret:{name}]")
        self._replacements = replacements
        # Longest first, so a value is never half-replaced by a shorter one inside it.
        self._pattern = (re.compile("|".join(re.escape(form) for form in sorted(replacements, key=len, reverse=True)))
                         if replacements else None)

    def __bool__(self) -> bool:
        return self._pattern is not None

    def redact(self, text: str) -> str:
        if self._pattern is None or not isinstance(text, str) or not text:
            return text
        text = self._pattern.sub(lambda match: self._replacements[match.group(0)], text)
        return self._redact_edges(text)

    def _redact_edges(self, text: str) -> str:
        """Hide the longest part of a value left at the start or the end by a cut."""
        head = tail = (0, "")
        for form, marker in self._replacements.items():
            longest = min(len(form) - 1, len(text))
            # The text ends with the form's beginning: try each place its first character occurs.
            first = form[0]
            index = text.find(first, len(text) - longest)
            while index != -1 and len(text) - index >= MIN_EDGE_CHARS:
                if form.startswith(text[index:]):
                    if len(text) - index > tail[0]:
                        tail = (len(text) - index, marker)
                    break
                index = text.find(first, index + 1)
            # The text starts with the form's end: try each place its last character occurs.
            last = form[-1]
            end = text.rfind(last, 0, longest)
            while end != -1 and end + 1 >= MIN_EDGE_CHARS:
                if form.endswith(text[:end + 1]):
                    if end + 1 > head[0]:
                        head = (end + 1, marker)
                    break
                end = text.rfind(last, 0, end)
        if head[0] + tail[0] > len(text):
            return head[1] if head[0] >= tail[0] else tail[1]
        if head[0]:
            text = head[1] + text[head[0]:]
        if tail[0]:
            text = text[:len(text) - tail[0]] + tail[1]
        return text


_EMPTY = Redactor({})
_lock = threading.Lock()
_cached: tuple[object, Redactor] | None = None


def current_redactor() -> Redactor:
    """The running agent's redactor, rebuilt when its vault changed.

    Never raises: a vault that cannot be opened has no values to hide, and its
    commands were not given any.
    """
    global _cached
    from flowly.secrets.vault import SecretsError, get_vault

    vault = get_vault()
    try:
        data = vault.load()
    except SecretsError:
        return _EMPTY
    except Exception:
        return _EMPTY
    if data is None or not data.secrets:
        return _EMPTY
    with _lock:
        if _cached is not None and _cached[0] is data:
            return _cached[1]
        redactor = Redactor({name: secret.value for name, secret in data.secrets.items()})
        _cached = (data, redactor)
        return redactor


def redact(text: str) -> str:
    """``text`` with the running agent's saved secrets hidden."""
    if not isinstance(text, str) or not text:
        return text
    return current_redactor().redact(text)


def _reset_for_tests() -> None:
    global _cached
    with _lock:
        _cached = None
