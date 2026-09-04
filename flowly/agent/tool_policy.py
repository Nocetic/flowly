"""Deterministic per-turn tool permission parsing.

Structured transport metadata is the authority.  Plain-language detection is
only a convenience for unambiguous requests such as "answer without tools";
it must not turn a scoped instruction ("use X, no other tools") into a global
executor deny.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

_DENY_POLICIES = frozenset({"none", "disabled", "deny", "no_tools", "no-tools"})
_ALLOW_POLICIES = frozenset({"auto", "allow", "allowed", "enabled", "tools"})

# These patterns require an imperative tool-use verb close to an explicit tool
# noun.  Nominal phrases such as "explain this tool's usage" intentionally do
# not match: merely discussing tools must never force an action turn.
_ENGLISH_TOOL_ACTION = re.compile(
    r"(?:"
    r"\b(?:use|call|invoke|run|execute)\b(?:\s+[\w.'’:-]+){0,6}\s+"
    r"\b(?:tools?|mcp(?:\s+servers?)?)\b"
    r"|"
    r"\b(?:tools?|mcp(?:\s+servers?)?)\b(?:\s+[\w.'’:-]+){0,6}\s+"
    r"\b(?:use|call|invoke|run|execute)\b"
    r")",
    re.IGNORECASE,
)
_ENGLISH_NAMED_ACTION = re.compile(
    r"\b(?:use|call|invoke|run|execute)\s+"
    r"(?:the\s+)?[a-z0-9][\w.-]{2,}\b",
    re.IGNORECASE,
)

_TURKISH_ACTION_VERB = (
    r"(?:"
    r"kullan(?:ın|iniz|ınız|unuz|ünüz|arak|ıp|mayı|manı)?"
    r"|çağır(?:ın|ınız|arak|ıp|mayı|manı)?"
    r"|çalıştır(?:ın|ınız|arak|ıp|mayı|manı)?"
    r")"
)
_TURKISH_TOOL_ACTION = re.compile(
    r"(?:"
    r"\b(?:tool\w*|mcp|ara[çc]\w*)\b(?:\s+\S+){0,6}\s+"
    + rf"\b{_TURKISH_ACTION_VERB}\b"
    + r"|"
    + rf"\b{_TURKISH_ACTION_VERB}\b(?:\s+\S+){{0,6}}\s+"
    + r"\b(?:tool\w*|mcp|ara[çc]\w*)\b"
    r")",
    re.IGNORECASE,
)

# A scoped exclusion is not a global no-tools request.  We remove it before
# checking global-deny grammar, but only when the same turn also contains an
# explicit positive tool invocation.  This keeps a bare "use no other tools"
# fail-closed instead of guessing which tool the user meant to retain.
_SCOPED_EXCLUSIONS = (
    re.compile(
        r"\b(?:do\s+not|don['’]?t|never)\s+"
        r"(?:use|call|invoke|run|execute)\s+(?:any\s+)?other\s+tools?\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bwithout\s+(?:using|calling|invoking|running|executing)\s+"
        r"(?:any\s+)?other\s+tools?\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:use|call|invoke|run|execute)\s+no\s+other\s+tools?\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:başka|diğer)\s+"
        r"(?:(?:hiçbir|herhangi\s+bir|bir)\s+)?"
        r"(?:tool|araç)\w*(?:\s+\S+){0,3}\s+"
        r"(?:kullanma|çağırma|çalıştırma)\w*\b",
        re.IGNORECASE,
    ),
)

_GLOBAL_DENIES = (
    re.compile(
        r"\b(?:do\s+not|don['’]?t|never)\s+"
        r"(?:use|call|invoke|run|execute)\s+(?:any\s+)?tools?\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bwithout\s+(?:using|calling|invoking|running|executing)\s+"
        r"(?:any\s+)?tools?\b",
        re.IGNORECASE,
    ),
    re.compile(r"\b(?:use|call|invoke|run|execute)\s+no\s+tools?\b", re.IGNORECASE),
    re.compile(
        r"^\s*(?:please[,:]?\s*)?no[\s-]?tools?"
        r"(?:\s+(?:please|for\s+this\s+turn))?[.!]?\s*$",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:hiçbir\s+)?(?:tool|araç)\w*\s+"
        r"(?:kullanma(?:dan)?|çağırma|çalıştırma)\w*\b",
        re.IGNORECASE,
    ),
)


@dataclass(frozen=True, slots=True)
class TurnToolPolicy:
    """Resolved execution permission and its non-secret audit source."""

    allowed: bool
    source: str
    exclusive: bool = False


def _strip_scoped_exclusions(text: str) -> tuple[str, bool]:
    stripped = text
    matched = False
    for pattern in _SCOPED_EXCLUSIONS:
        stripped, count = pattern.subn(" ", stripped)
        matched = matched or bool(count)
    return stripped, matched


def _has_positive_tool_action(text: str, *, named_target: bool = False) -> bool:
    return bool(
        _ENGLISH_TOOL_ACTION.search(text)
        or _TURKISH_TOOL_ACTION.search(text)
        or (named_target and _ENGLISH_NAMED_ACTION.search(text))
    )


def has_explicit_tool_invocation_intent(content: str) -> bool:
    """Return whether *content* clearly asks to execute a tool.

    A negative clause is removed before action detection so "do not use any
    other tools" cannot count as its own positive invocation.  Within that
    scoped grammar an identifier such as ``resolve-library-id`` is accepted as
    a named target even when the user omits the literal word "tool".
    """

    text = str(content or "").casefold()
    stripped, scoped = _strip_scoped_exclusions(text)
    return _has_positive_tool_action(
        stripped if scoped else text,
        named_target=scoped,
    )


def resolve_turn_tool_policy(
    metadata: dict[str, Any] | None,
    content: str,
) -> TurnToolPolicy:
    """Resolve the backend tool gate with deterministic precedence.

    Explicit structured denies always win.  Explicit structured allows are
    next, then conservative natural-language parsing.  Scoped exclusions are
    distinguished from global denials so a request for one named integration
    cannot accidentally hide every tool schema.
    """

    metadata = metadata or {}
    policy = str(metadata.get("tool_policy") or "").strip().casefold()
    structured = metadata.get("tools_allowed")

    if structured is False or policy in _DENY_POLICIES:
        return TurnToolPolicy(False, "structured_deny")
    if structured is True or policy in _ALLOW_POLICIES:
        return TurnToolPolicy(True, "structured_allow")

    original_text = str(content or "").casefold()
    stripped_text, scoped = _strip_scoped_exclusions(original_text)
    if scoped and not _has_positive_tool_action(
        stripped_text,
        named_target=True,
    ):
        # A bare exclusion has no resolvable capability to retain. Treat it as
        # a deny instead of silently inventing an allowlist target.
        return TurnToolPolicy(False, "content_global_deny")
    text = stripped_text if scoped else original_text

    if any(pattern.search(text) for pattern in _GLOBAL_DENIES):
        return TurnToolPolicy(False, "content_global_deny")
    if scoped:
        return TurnToolPolicy(True, "content_scoped_exclusion", exclusive=True)
    return TurnToolPolicy(True, "default")


_GENERIC_SOURCES = frozenset({"", "builtin", "built in", "external", "mcp", "plugin"})


def _search_form(value: str) -> str:
    """Normalize a user phrase or identifier for boundary-safe matching."""

    # ``\w`` includes underscores, while users spell MCP identifiers with
    # spaces or hyphens. Treat underscores as separators too so all three
    # forms resolve to the same boundary-safe phrase.
    return " ".join(re.sub(r"[\W_]+", " ", value.casefold()).split())


def _contains_phrase(haystack: str, needle: str) -> bool:
    if len(needle) < 3:
        return False
    return re.search(rf"(?<!\w){re.escape(needle)}(?!\w)", haystack) is not None


def resolve_exclusive_tool_scope(
    content: str,
    tools: Iterable[tuple[str, str]],
) -> frozenset[str]:
    """Resolve an exclusive natural-language request to registered tool names.

    Exact tool names (including the remote suffix of an MCP wrapper) win over
    server/source mentions.  A source mention grants that source's complete
    tool family.  Unknown targets return an empty set so callers can avoid
    pretending an ambiguous phrase is an enforceable allowlist.
    """

    text = _search_form(str(content or ""))
    catalog = [
        (name, _search_form(name), _search_form(source))
        for name, source in tools
        if isinstance(name, str) and name
    ]

    exact: set[str] = set()
    for name, normalized_name, normalized_source in catalog:
        aliases = {normalized_name}
        prefix = f"mcp {normalized_source} " if normalized_source else ""
        if prefix and normalized_name.startswith(prefix):
            aliases.add(normalized_name[len(prefix):])
        if any(_contains_phrase(text, alias) for alias in aliases):
            exact.add(name)
    if exact:
        return frozenset(exact)

    named_sources = {
        source
        for _name, _normalized_name, source in catalog
        if source not in _GENERIC_SOURCES and _contains_phrase(text, source)
    }
    return frozenset(
        name
        for name, _normalized_name, source in catalog
        if source in named_sources
    )
