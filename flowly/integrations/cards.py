"""Schema for cards rendered by the TUI connection catalogs."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field as dc_field
from enum import Enum
from typing import Any, Literal


class FieldType(str, Enum):
    """Render hint for a single config field.

    ``PASSWORD`` differs from ``TEXT`` only in that the input is masked in
    the TUI and the value is never echoed in test-connection output. The
    underlying storage is identical — a future keychain migration will
    divert PASSWORD-marked values transparently.
    """
    TEXT = "text"
    PASSWORD = "password"
    INT = "int"
    BOOL = "bool"
    SELECT = "select"
    MULTI = "multi"     # comma-separated list of strings (stored as list[str])


@dataclass
class Field:
    """A single editable field within an integration card."""
    key: str                                # leaf key under card.config_path
    label: str
    type: FieldType = FieldType.TEXT
    placeholder: str = ""
    help: str = ""                          # one-line hint, shown under input
    required: bool = False
    # ``choices`` is a list of (value, display) tuples for SELECT fields.
    choices: list[tuple[str, str]] = dc_field(default_factory=list)
    default: Any = None


ProbeStatus = Literal[
    "ok",                   # service reachable and credentials valid
    "auth_failed",          # reachable but credentials rejected
    "down",                 # not reachable / network error / timeout
    "not_configured",       # required fields empty → nothing to test
    "unknown",              # no probe implemented yet
    "disabled",             # configured but enabled=False
]


@dataclass
class ProbeResult:
    status: ProbeStatus
    detail: str = ""        # short human text shown next to badge

    @property
    def badge(self) -> str:
        return {
            "ok":             "●",
            "auth_failed":    "⚠",
            "down":           "⚠",
            "not_configured": "○",
            "unknown":        "·",
            "disabled":       "○",
        }.get(self.status, "·")


@dataclass
class IntegrationCard:
    """One row in the ``/integrations`` catalog.

    ``config_path`` is a **dotted snake_case path** into the Pydantic Config
    tree (e.g. ``"channels.telegram"``, ``"integrations.home_assistant"``).
    Field values land at ``{config_path}.{field.key}`` after save.

    ``probe`` receives the **resolved field values** (snake_case dict, with
    ints/bools coerced) and returns a ``ProbeResult``. The probe must be
    cheap (< 5s) and never raise — wrap all errors in ``ProbeResult``.

    ``needs_gateway_restart`` decides whether the setup modal shows a
    "restart gateway to apply" hint after Save. Channels universally need
    it (their adapters are started at boot); LLM provider keys are
    re-read per request and don't.
    """
    key: str
    label: str
    category: Literal["channel", "tool", "voice", "provider", "system", "media"]
    description: str
    docs_url: str
    config_path: str
    fields: list[Field] = dc_field(default_factory=list)
    probe: Callable[[dict[str, Any]], Awaitable[ProbeResult]] | None = None
    needs_gateway_restart: bool = True
    # For system cards (e.g. iOS pairing) that aren't config-editable:
    # the modal renders the description + a custom action label instead
    # of the field form. ``custom_action`` is a free-form string the TUI
    # can route on (e.g. "login", "logout").
    custom_action: str = ""
