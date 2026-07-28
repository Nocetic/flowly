"""Client-advertised rich-rendering capabilities.

Capabilities are supplied by the chat client on each ``chat.send`` request.
They are deliberately allowlisted and bounded before they reach agent
metadata: an untrusted or newer client cannot turn arbitrary strings into
prompt instructions.
"""

from __future__ import annotations

from typing import Any

MERMAID_RENDER_CAPABILITY = "mermaid"
SUPPORTED_RENDER_CAPABILITIES = frozenset({MERMAID_RENDER_CAPABILITY})

_MAX_CAPABILITIES = 16
_MAX_CAPABILITY_LENGTH = 64


def normalize_render_capabilities(value: Any) -> tuple[str, ...]:
    """Return the supported, de-duplicated capabilities in wire order.

    The RPC field is a JSON array. Malformed values, unknown capabilities,
    non-string members, and oversized names are ignored so older and newer
    clients remain safely interoperable.
    """

    if not isinstance(value, (list, tuple)):
        return ()

    normalized: list[str] = []
    seen: set[str] = set()
    for item in value[:_MAX_CAPABILITIES]:
        if not isinstance(item, str):
            continue
        capability = item.strip().lower()
        if (
            not capability
            or len(capability) > _MAX_CAPABILITY_LENGTH
            or capability not in SUPPORTED_RENDER_CAPABILITIES
            or capability in seen
        ):
            continue
        seen.add(capability)
        normalized.append(capability)
    return tuple(normalized)


__all__ = [
    "MERMAID_RENDER_CAPABILITY",
    "SUPPORTED_RENDER_CAPABILITIES",
    "normalize_render_capabilities",
]
