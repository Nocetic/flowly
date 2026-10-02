"""What never leaves the machine in a log served to a client.

The log file itself stays as written (it is the owner's, on their disk). What
``logs.events`` / ``logs.tail`` hand out — possibly over the relay to a phone —
goes through ``redact`` first:

- credentials: API keys, bearer and bot tokens, JWTs, ``password=…`` pairs;
- the owner's home directory, which carries their account name, becomes ``~``;
- long identifiers (UUIDs) are shortened to their first eight characters,
  enough to tell two apart, not enough to address one.
"""

from __future__ import annotations

import re
from pathlib import Path

MASK = "•••"

_SECRETS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b(?:sk|pk|rk)-(?:[A-Za-z0-9]+-)*[A-Za-z0-9_-]{16,}"),
    re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}"),
    re.compile(r"\bxapp-[A-Za-z0-9-]{10,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{30,}"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
    re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b"),
)
_BEARER = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}")
_PAIR = re.compile(
    r"(?i)\b([\w-]*(?:api[_-]?key|token|secret|password|passwd|authorization|cookie)[\w-]*)"
    # The scheme of "Authorization: Bearer …" is not the secret; _BEARER masks the token.
    r"(\s*[=:]\s*)(\"[^\"]*\"|'[^']*'|(?!(?:bearer|basic)\s)[^\s,;&]+)"
)
_HOMES = re.compile(r"(?:/Users|/home|[A-Za-z]:\\Users)[/\\][^/\\\s'\"]+")
_UUID = re.compile(r"\b([0-9a-fA-F]{8})-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")


def redact(text: str, home: str | None = None) -> str:
    if not text:
        return text
    for pattern in _SECRETS:
        text = pattern.sub(MASK, text)
    text = _BEARER.sub(lambda match: f"{match.group(1)} {MASK}", text)
    text = _PAIR.sub(lambda match: f"{match.group(1)}{match.group(2)}{MASK}", text)
    home = home if home is not None else str(Path.home())
    if home and home not in ("/", "~"):
        # Any home, not only /Users or /home: a server agent often runs as
        # root. Only the whole directory: /root, never the start of /rootfs.
        text = re.sub(re.escape(home.rstrip("/\\")) + r"(?![\w.-])", "~", text)
    text = _HOMES.sub("~", text)
    return _UUID.sub(lambda match: f"{match.group(1)}…", text)
