"""Things worth an owner's attention, written and read from one definition.

``notable("channel.connected", channel="Telegram", account="@bot")`` writes a
plain log line from the template below, at the template's level, attributed
to the caller. The reader (``events``) compiles the same templates back into
patterns, so a line and its meaning cannot drift apart: change the words here
and both sides follow.

Only lifecycle moments and problems belong here. Anything that happens every
minute (an app connecting, a device registering) is DEBUG, not notable.
"""

from __future__ import annotations

import re

from loguru import logger

REASON_MAX = 300

# code: (level, template). Placeholders are filled from keyword arguments.
NOTABLE: dict[str, tuple[str, str]] = {
    "gateway.started": ("INFO", "Gateway started on {address}"),
    "gateway.stopped": ("INFO", "Gateway stopped"),
    "channel.connected": ("INFO", "{channel} connected as {account}"),
    "channel.failed": ("ERROR", "{channel} could not start: {reason}"),
    "channel.reconnecting": ("WARNING", "{channel} connection dropped; reconnecting on its own."),
    "relay.connected": ("INFO", "Connected to Flowly Cloud"),
    "relay.lost": ("WARNING", "Lost the connection to Flowly Cloud; retrying in {delay}s: {reason}"),
    "routine.failed": ("ERROR", "Routine '{name}' failed: {reason}"),
    "agents.started": ("INFO", "Started {count} agent(s); they keep running until you stop them."),
    "agent.start_failed": ("ERROR", "Agent '{name}' did not start: {reason}"),
    "agents.autostart_failed": ("ERROR", "Flowly could not start your agents: {reason}"),
    "net.rejected_request": (
        "INFO",
        "Rejected {count} malformed request(s) from the internet; latest from {ip}. "
        "Usually a scanner probing the open port; nothing reached the agent.",
    ),
}


def _clean(value: object) -> str:
    text = " ".join(str(value).split())
    return text if len(text) <= REASON_MAX else text[: REASON_MAX - 1].rstrip() + "…"


def notable(code: str, *, depth: int = 0, **params: object) -> None:
    """Write the notable line for ``code``; never raises into the caller."""
    try:
        level, template = NOTABLE[code]
        message = template.format(**{key: _clean(value) for key, value in params.items()})
        logger.opt(depth=depth + 1).log(level, message)
    except Exception as exc:  # noqa: BLE001 — a log line must never break the caller
        logger.opt(depth=depth + 1).warning(f"notable {code!r} could not be written: {type(exc).__name__}")


def _compile(template: str) -> re.Pattern[str]:
    pattern = re.escape(template)
    pattern = re.sub(r"\\\{(\w+)\\\}", lambda match: f"(?P<{match.group(1)}>.+?)", pattern)
    return re.compile(f"^{pattern}$")


PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (code, _compile(template)) for code, (_level, template) in NOTABLE.items()
)
