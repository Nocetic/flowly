"""Bounded, deterministic tool permissions for explicit user instructions.

Structured transport grants are ceilings. Natural-language selectors can
narrow them, never broaden them. This grammar recognizes imperative English
and Turkish clauses, not arbitrary prose, quoted examples or tool output.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

_DENY_POLICIES = frozenset({"none", "disabled", "deny", "no_tools", "no-tools"})
_ALLOW_POLICIES = frozenset({"auto", "allow", "allowed", "enabled", "tools"})
_GENERIC_SOURCES = frozenset({"", "builtin", "built in", "external", "mcp", "plugin"})
MAX_POLICY_CHARS = 65536
MAX_POLICY_CLAUSES = 256

_EN_VERB = r"(?:use|call|invoke|run|execute)"
_TR_VERB = r"(?:kullan|cagir|calistir)(?:in|iniz|unuz|arak|ip|mayi|mani)?"
_ONLY = r"(?:only|exclusively)"
_TR_ONLY = r"(?:sadece|yalnizca|yalniz)"
_TR_TOOLS = r"(?:tool\w*|arac\w*|mcp(?:\s+sunucu\w*)?)"
_EN_NEG = rf"(?:do\s+not|don['’]?t|never)\s+{_EN_VERB}"
_TR_NEG = r"(?:kullanma|cagirma|calistirma)(?:dan|yin|yiniz)?"
_COMMAND_PREFIX = r"(?:(?:please|now|then|and|but|lutfen|simdi|sonra|ve|ama|fakat|for\s+this\s+(?:task|turn|request)|bu\s+(?:gorev|is|tur)\s+icin|bu\s+(?:gorevde|turda|iste|konuda))\s*[, :]?\s+)*"
_DISCUSSION = re.compile(r"^(?:explain|describe|discuss|what|how|if|when|suppose|imagine|ornek|eger|acikla|anlat)\b")
_GLOBAL_DENY = re.compile(
    rf"^(?:{_EN_NEG}\s+(?:any\s+)?tools?\b|{_EN_VERB}\s+no\s+tools?\b|"
    rf"(?:hicbir\s+)?{_TR_TOOLS}\s+{_TR_NEG}\b|no[ -]?tools?\b)"
)
_OTHER_DENY = re.compile(
    rf"(?:{_EN_NEG}\s+(?:any\s+)?other\s+tools?\b|"
    rf"without\s+(?:using|calling|invoking|running|executing)\s+(?:any\s+)?other\s+tools?\b|"
    rf"{_EN_VERB}\s+no\s+other\s+tools?\b|\bno\s+other\s+tools?\b|"
    rf"\b(?:baska|diger)\s+(?:(?:hicbir|herhangi\s+bir|bir)\s+)?{_TR_TOOLS}\s+{_TR_NEG}\b)"
)
_EXCEPTION_EN = re.compile(rf"^(?:{_EN_NEG}\s+(?:any\s+)?|{_EN_VERB}\s+no\s+)tools?\s*,?\s+(?:except|other\s+than)\s+(.+)$")
_EXCEPTION_TR = re.compile(rf"^(.+?)\s+(?:disinda|haric(?:inde)?)\s+(?:(?:hicbir|baska|diger)\s+)?{_TR_TOOLS}\s+{_TR_NEG}$")
_EN_COMMAND = re.compile(rf"^(?:(?P<pre>{_ONLY})\s+)?{_EN_VERB}\s+(?P<target>.+)$")
_TR_COMMAND = re.compile(rf"^(?P<target>.+?)\s+{_TR_VERB}\b(?P<tail>.*)$")
_TR_NEGATIVE = re.compile(rf"^(.+?)\s+(?:{_TR_NEG}|degil|haric)\b")
_EN_NEGATIVE = re.compile(rf"^(?:{_EN_NEG}|not|never|no|excluding|except)\s+(.+)$")
_QUOTED = re.compile(r'"[^"\n]*"|“[^”\n]*”|(?<!\w)\x27[^\x27\n]+\x27(?!\w)|\x60[^\x60\n]+\x60')


def _fold(value: str) -> str:
    # Include decomposed capital Turkish I and ASCII keyboard spellings. Any
    # identifier collisions caused by normalization remain ambiguous below.
    return unicodedata.normalize("NFKC", value).casefold().translate(str.maketrans({
        "ç": "c", "ğ": "g", "ı": "i", "ö": "o", "ş": "s", "ü": "u", "\u0307": None,
    }))


def _search_form(value: str) -> str:
    return " ".join(re.sub(r"[\W_]+", " ", _fold(value)).split())


@dataclass(frozen=True, slots=True)
class TurnToolPolicy:
    allowed: bool
    source: str
    exclusive: bool = False


@dataclass(frozen=True, slots=True)
class _Selectors:
    # Each independently exclusive clause is a ceiling, hence intersection.
    groups: tuple[tuple[str, ...], ...] = ()
    denied: tuple[str, ...] = ()
    invoked: bool = False
    global_deny: bool = False
    overflow: bool = False


def normalize_tool_allowlist(value: object) -> frozenset[str] | None:
    """Absent means unrestricted; a malformed supplied grant fails closed."""
    if value is None:
        return None
    if not isinstance(value, (list, tuple, set, frozenset)) or len(value) > 1024:
        return frozenset()
    if any(not isinstance(item, str) or not item or len(item) > 512 for item in value):
        return frozenset()
    return frozenset(value)


def _instruction_text(content: str) -> str:
    text = _fold(content)
    # Fences, Markdown quotations, and quoted imperative examples are data.
    text = re.sub(r"(?ms)^\s*(\x60{3,}|~{3,}).*?^\s*\1[^\n]*$", " ", text)
    text = re.sub(r"(?m)^\s*>[^\n]*", " ", text)

    def quote(match):
        value = match.group(0)[1:-1]
        if re.search(rf"\b(?:{_EN_VERB}|{_TR_VERB}|{_TR_NEG})\b", value):
            return " "
        return value  # Quoted identifiers are legitimate selectors.

    return _QUOTED.sub(quote, text)


def _selectors(content: str) -> _Selectors:
    if len(content) > MAX_POLICY_CHARS:
        return _Selectors(global_deny=True, overflow=True)
    text = _instruction_text(content)
    text = re.sub(rf"(\b{_TR_VERB}\b)\s*(?:,|\s+(?:ama|fakat|ve)\b)\s*", r"\1;", text)
    # Do not split dots within identifiers, or commas joining target lists.
    text = re.sub(
        rf"(?:,|\b(?:and|but|ve|ama|fakat)\b)\s*(?=(?:{_EN_NEG}|{_EN_VERB}\s+no|not\b|no\s+other|baska\b|diger\b|(?:hicbir\s+)?{_TR_TOOLS}\s+{_TR_NEG}))",
        ";", text,
    )
    clauses = re.split(r"[;\n]+|[.!?](?:\s+|$)", text)
    if len(clauses) > MAX_POLICY_CLAUSES:
        return _Selectors(global_deny=True, overflow=True)
    positives: list[str] = []
    groups: list[tuple[str, ...]] = []
    negatives: list[str] = []
    global_deny = False
    other_deny = False
    for raw in clauses:
        clause = re.sub(rf"^{_COMMAND_PREFIX}", "", raw.strip()).strip(" ,")
        if not clause:
            continue
        if _DISCUSSION.match(clause):
            if re.match(r"^(?:explain|describe)\b", clause) and re.search(r"\bwithout\s+(?:using|calling|invoking|running|executing)\s+(?:any\s+)?tools?\b", clause):
                global_deny = True
            continue
        exception = _EXCEPTION_EN.match(clause) or _EXCEPTION_TR.match(clause)
        if exception:
            groups.append((exception.group(1),))
            positives.append(exception.group(1))
            continue
        clause, count = _OTHER_DENY.subn(" ", clause)
        other_deny |= bool(count)
        clause = clause.strip(" ,")
        if _GLOBAL_DENY.search(clause) or re.search(r"\bwithout\s+(?:using|calling|invoking|running|executing)\s+(?:any\s+)?tools?\b", clause):
            global_deny = True
            continue
        negative = _EN_NEGATIVE.match(clause) or _TR_NEGATIVE.match(clause)
        if negative:
            negatives.append(negative.group(1))
            continue
        exclusion = re.split(r"\s+(?:excluding|except|not|never|no)\s+", clause, maxsplit=1) if _EN_COMMAND.match(clause) else [clause]
        if len(exclusion) == 2:
            clause = exclusion[0]
            negatives.append(exclusion[1])
        command = _EN_COMMAND.match(clause)
        exclusive = False
        target = ""
        if command:
            target = command.group("target")
            exclusive = bool(command.group("pre"))
            target, count = re.subn(rf"^{_ONLY}\s+", "", target)
            exclusive |= bool(count)
            target, count = re.subn(rf"\s+{_ONLY}\s*$", "", target)
            exclusive |= bool(count)
            # Purpose/frequency clauses aren't selectors ("use X to explain Y").
            target = re.split(r"\s+(?:for|about|to|only\s+(?:once|twice|when|if))\b", target, maxsplit=1)[0]
        else:
            command = _TR_COMMAND.match(clause)
            if command:
                target = command.group("target")
                target, count = re.subn(rf"^{_TR_ONLY}\s+|\s+{_TR_ONLY}$", "", target)
                exclusive = bool(count) or bool(re.fullmatch(_TR_ONLY, command.group("tail").strip()))
        if target:
            positives.append(target)
            if exclusive:
                groups.append((target,))
    if other_deny:
        if positives:
            groups.append(tuple(positives))
        else:
            global_deny = True
    return _Selectors(tuple(groups), tuple(negatives), bool(positives), global_deny)


def has_explicit_tool_invocation_intent(content: str) -> bool:
    parsed = _selectors(str(content or ""))
    return parsed.invoked and not parsed.global_deny and bool(
        parsed.groups or re.search(r"\b(?:tools?\w*|mcp|arac\w*)\b", _instruction_text(str(content or "")))
    )


def resolve_turn_tool_policy(metadata: dict[str, Any] | None, content: str) -> TurnToolPolicy:
    metadata = metadata or {}
    policy = str(metadata.get("tool_policy") or "").strip().casefold()
    structured = metadata.get("tools_allowed")
    if structured is False or policy in _DENY_POLICIES:
        return TurnToolPolicy(False, "structured_deny")
    parsed = _selectors(str(content or ""))
    if parsed.overflow:
        return TurnToolPolicy(False, "content_policy_limit")
    if structured is True or policy in _ALLOW_POLICIES:
        # Explicit broad enablement keeps its established precedence over a
        # prose global deny, but does not erase a positive exclusive ceiling.
        return TurnToolPolicy(True, "structured_allow", exclusive=bool(parsed.groups))
    if parsed.global_deny:
        return TurnToolPolicy(False, "content_global_deny")
    if parsed.groups:
        return TurnToolPolicy(True, "content_scoped_exclusion", exclusive=True)
    return TurnToolPolicy(True, "default")


def _resolve_target(target: str, catalog: list[tuple[str, str]], *, denying: bool = False) -> frozenset[str]:
    """Resolve longest boundary-safe aliases; ambiguous positive aliases deny."""
    text = _search_form(target)
    aliases: dict[str, set[str]] = {}
    sources: dict[str, set[str]] = {}
    for name, source in catalog:
        normalized = _search_form(name)
        source = _search_form(source)
        aliases.setdefault(normalized, set()).add(name)
        prefix = f"mcp {source} "
        if source and normalized.startswith(prefix):
            aliases.setdefault(normalized[len(prefix):], set()).add(name)
        if source not in _GENERIC_SOURCES:
            sources.setdefault(source, set()).add(name)

    matches = []
    for kind, mapping in (("tool", aliases), ("source", sources)):
        for alias, names in mapping.items():
            # Common Turkish object suffixes may be written without an apostrophe.
            pattern = rf"(?<!\w){re.escape(alias)}(?:yi|yu|i|u)?(?!\w)"
            for match in re.finditer(pattern, text):
                matches.append((match.start(), match.end(), kind, names, len(alias)))
    matches.sort(key=lambda item: (-(item[1] - item[0]), -item[4], item[2] != "tool", item[0]))
    chosen = []
    for match in matches:
        if not any(match[0] < end and start < match[1] for start, end, *_ in chosen):
            chosen.append(match)
    named_sources = set().union(*(names for _, _, kind, names, _ in chosen if kind == "source"))
    exact: set[str] = set()
    for _, _, kind, names, _ in chosen:
        if kind != "tool":
            continue
        if len(names) > 1 and not denying:
            names = names & named_sources
            if len(names) != 1:
                return frozenset()
        exact.update(names)
    # A source next to a tool qualifies it; other explicitly named sources
    # still contribute their own family (mixed server/tool selector lists).
    result = set(exact)
    for _, _, kind, names, _ in chosen:
        if kind == "source" and not (names & exact):
            result.update(names)
    return frozenset(result)


def resolve_exclusive_tool_scope(content: str, tools: Iterable[tuple[str, str]]) -> frozenset[str]:
    parsed = _selectors(str(content or ""))
    if parsed.global_deny or not parsed.groups:
        return frozenset()
    catalog = [(name, source) for name, source in tools if isinstance(name, str) and name and isinstance(source, str)]
    ceilings = [set().union(*(_resolve_target(target, catalog) for target in group)) for group in parsed.groups]
    allowed = set.intersection(*ceilings)
    for target in parsed.denied:
        allowed.difference_update(_resolve_target(target, catalog, denying=True))
    return frozenset(allowed)
