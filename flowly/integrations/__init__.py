"""Integration registry + secure connect/test/save plumbing for the TUI.

This package is the single source of truth for **what third-party services
Flowly can talk to** and **how to set them up from the TUI**. The TUI renders
filtered catalogs from these cards: ``/integrations`` for service/tool cards,
``/channels`` for messaging channels, and dedicated provider/model surfaces
for LLM providers. All entries share the same ``IntegrationCard`` schema,
field definitions, and async health probes.

Design notes
------------
The cards are **declarative data, not classes** — adding a new integration
is one entry in ``registry.py`` plus (optionally) one probe function. The
TUI knows nothing about specific services; it only knows how to render a
card and write its fields to ``~/.flowly/config.json``.

Secrets currently land in ``config.json`` alongside non-sensitive settings
because that's where the channels/tools read them from today. The keychain
migration (see ``flowly.account.token_store``) is a future upgrade — the
field schema already marks secrets via ``FieldType.PASSWORD`` so the
storage layer can divert them later without touching the registry.
"""

from flowly.integrations.cards import (
    Field,
    FieldType,
    IntegrationCard,
    ProbeResult,
    ProbeStatus,
)
from flowly.integrations.config_io import (
    apply_card_values,
    read_card_values,
)
from flowly.integrations.registry import REGISTRY, get_card, list_cards

__all__ = [
    "Field",
    "FieldType",
    "IntegrationCard",
    "ProbeResult",
    "ProbeStatus",
    "REGISTRY",
    "apply_card_values",
    "get_card",
    "list_cards",
    "read_card_values",
]
