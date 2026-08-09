"""Route-scoped progressive disclosure for external tool catalogs.

The important distinction in this module is between *enabled* tools and
*advertised* tools.  Platform routing decides what is enabled.  This module
then keeps built-ins eager, promotes external tools that match the current
user request, and places only the unrelated remainder behind a compact
``tool_search`` / ``tool_call`` bridge.

Intent promotion is deliberately local and deterministic: no extra LLM call
is spent deciding which schemas to expose.  A request that names Context7,
Typefully, or an external capability can therefore call the matching tool
directly on the first model round, while a genuinely unknown capability still
has a lossless two-step fallback (search returns the input schema, then call).
"""

from __future__ import annotations

import json
import math
import re
import unicodedata
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from flowly.agent.tools.routing import infer_toolset

TOOL_SEARCH_NAME = "tool_search"
TOOL_DESCRIBE_NAME = "tool_describe"  # legacy-compatible dispatcher only
TOOL_CALL_NAME = "tool_call"
DISCOVERY_TOOL_NAMES = frozenset({
    TOOL_SEARCH_NAME,
    TOOL_DESCRIBE_NAME,
    TOOL_CALL_NAME,
})
VISIBLE_DISCOVERY_TOOL_NAMES = frozenset({TOOL_SEARCH_NAME, TOOL_CALL_NAME})
DEFAULT_DEFERRED_TOOLSETS = frozenset({"mcp", "extensions"})
DEFAULT_ALWAYS_VISIBLE_TOOLS: frozenset[str] = frozenset()

_TOKEN_RE = re.compile(r"[\w]+", re.UNICODE)
_STOP_WORDS = frozenset({
    # English routing noise.
    "a", "an", "and", "are", "as", "at", "be", "by", "can", "do",
    "for", "from", "get", "i", "in", "is", "it", "me", "my", "of",
    "on", "or", "please", "the", "this", "to", "tool", "use", "with",
    "you",
    # Turkish routing noise.
    "acaba", "bana", "ben", "bir", "bu", "da", "de", "et", "icin",
    "ile", "mi", "mı", "mu", "mü", "ne", "olan", "olarak", "onu",
    "şu", "ve", "ya", "yap", "yapar", "yapabilir", "için",
})

# These words are useful for search ranking but too generic to justify eagerly
# exposing a full external schema when the user did not name a provider.  A
# tool that only overlaps on phrases such as "connected external information"
# remains fully reachable through the compact catalog instead of consuming
# prompt tokens or distracting the model.
_GENERIC_PROMOTION_TOKENS = frozenset({
    "about", "action", "any", "appropriate", "authoritative", "available",
    "choose", "connected", "create", "data", "delete", "detail", "details",
    "external", "fetch", "find", "get", "information", "integration", "item",
    "list", "lookup", "name", "one", "project", "query", "read", "resolve",
    "resource", "result", "search", "send", "sentence", "service", "set",
    "update", "using", "value", "without", "yourself",
})

# A deliberately small, high-signal capability vocabulary lets the host match
# common user intents without spending another model call.  It is only a fast
# path: ambiguous or unknown capabilities remain available through the complete
# connected-source index and tool_search.  Prefix aliases cover ordinary
# Turkish suffixes (for example ``dokumantasyondan``) without language-specific
# stemming dependencies.
_CAPABILITY_RULES: dict[str, dict[str, frozenset[str]]] = {
    "documentation": {
        "tool_terms": frozenset({
            "docs", "documentation", "example", "examples", "library",
            "manual", "reference",
        }),
        "intent_aliases": frozenset({
            "docs", "documentation", "official documentation",
            "official docs", "reference manual", "dokuman", "dokumantasyon",
            "resmi dokuman", "resmi kaynak",
        }),
    },
    "social publishing": {
        "tool_terms": frozenset({
            "draft", "drafts", "post", "posts", "publish", "schedule",
            "social", "thread", "tweet",
        }),
        "intent_aliases": frozenset({
            "draft", "post", "publish", "schedule a post", "social media",
            "thread", "tweet", "gonderi", "paylas", "sosyal medya",
            "taslak", "yayinla",
        }),
    },
    "email": {
        "tool_terms": frozenset({"email", "inbox", "mail", "sender"}),
        "intent_aliases": frozenset({
            "email", "inbox", "mail", "e posta", "eposta", "gelen kutusu",
        }),
    },
    "calendar": {
        "tool_terms": frozenset({"calendar", "event", "meeting"}),
        "intent_aliases": frozenset({
            "calendar", "event", "meeting", "takvim", "etkinlik", "toplanti",
        }),
    },
    "project tracking": {
        "tool_terms": frozenset({
            "board", "issue", "project", "sprint", "task", "ticket",
        }),
        "intent_aliases": frozenset({
            "board", "issue", "project tracking", "sprint", "task", "ticket",
            "gorev", "is takibi", "proje takibi",
        }),
    },
    "source control": {
        "tool_terms": frozenset({
            "branch", "commit", "pull", "repository", "repo", "revision",
        }),
        "intent_aliases": frozenset({
            "branch", "commit", "pull request", "repository", "repo",
            "dal", "depo", "kod deposu",
        }),
    },
    "cloud storage": {
        "tool_terms": frozenset({
            "download", "drive", "file", "folder", "storage", "upload",
        }),
        "intent_aliases": frozenset({
            "cloud file", "cloud storage", "drive", "folder", "upload",
            "bulut dosya", "bulut depolama", "klasor", "yukle",
        }),
    },
    "database": {
        "tool_terms": frozenset({
            "database", "postgres", "record", "row", "schema", "sql", "table",
        }),
        "intent_aliases": frozenset({
            "database", "postgres", "record", "sql", "table", "veritabani",
            "kayit",
        }),
    },
    "monitoring": {
        "tool_terms": frozenset({
            "alert", "error", "incident", "log", "monitor", "trace",
        }),
        "intent_aliases": frozenset({
            "alert", "error tracking", "incident", "monitoring", "trace",
            "hata takibi", "izleme", "olay",
        }),
    },
    "payments": {
        "tool_terms": frozenset({
            "checkout", "invoice", "payment", "price", "subscription",
        }),
        "intent_aliases": frozenset({
            "checkout", "invoice", "payment", "subscription", "fatura",
            "odeme", "abonelik",
        }),
    },
    "crm": {
        "tool_terms": frozenset({
            "account", "contact", "customer", "deal", "lead", "pipeline",
        }),
        "intent_aliases": frozenset({
            "crm", "customer", "deal", "lead", "pipeline", "musteri",
            "satis firsati",
        }),
    },
    "design": {
        "tool_terms": frozenset({
            "canvas", "component", "design", "frame", "prototype",
        }),
        "intent_aliases": frozenset({
            "design", "prototype", "ui component", "tasarim", "prototip",
        }),
    },
    "media generation": {
        "tool_terms": frozenset({
            "audio", "image", "render", "speech", "video", "voice",
        }),
        "intent_aliases": frozenset({
            "generate image", "generate video", "image generation",
            "text to speech", "video generation", "voiceover", "gorsel uret",
            "resim uret", "seslendir", "video uret",
        }),
    },
}


def _definition_name(definition: Mapping[str, Any]) -> str:
    function = definition.get("function")
    if not isinstance(function, Mapping):
        return ""
    return str(function.get("name") or "").strip()


def _definition_description(definition: Mapping[str, Any], *, limit: int = 180) -> str:
    function = definition.get("function")
    if not isinstance(function, Mapping):
        return ""
    value = " ".join(str(function.get("description") or "").split())
    if not value:
        return "No description available."
    if len(value) <= limit:
        return value
    return value[: max(1, limit - 3)].rsplit(" ", 1)[0].rstrip() + "..."


def estimate_schema_tokens(definitions: Iterable[Mapping[str, Any]]) -> int:
    """Return a stable conservative estimate for JSON tool schemas."""
    compact = json.dumps(list(definitions), ensure_ascii=False, separators=(",", ":"))
    return max(1, (len(compact) + 3) // 4)


def estimate_schema_transmission_tokens(
    definitions: Iterable[Mapping[str, Any]],
    *,
    llm_rounds: int,
) -> int:
    """Estimate repeated schema input across a complete tool turn.

    Providers receive the tool array on every model round.  This helper keeps
    tests and telemetry honest: a smaller one-round schema is not necessarily
    cheaper when discovery introduces multiple extra rounds.
    """
    return estimate_schema_tokens(definitions) * max(1, int(llm_rounds))


def annotate_search_repetition(
    result: str,
    seen_result_sets: dict[tuple[str, ...], int],
) -> str:
    """Add turn-local repetition facts without blocking or steering the model."""
    try:
        payload = json.loads(result)
    except (TypeError, ValueError):
        return result
    if not isinstance(payload, dict):
        return result
    matches = payload.get("matches")
    if not isinstance(matches, list):
        return result
    signature = tuple(sorted(
        str(match.get("name") or "")
        for match in matches
        if isinstance(match, Mapping) and str(match.get("name") or "")
    ))
    if not signature:
        return result
    previous = seen_result_sets.get(signature, 0)
    seen_result_sets[signature] = previous + 1
    payload["repeated_result_set"] = previous > 0
    payload["previous_occurrences"] = previous
    if previous:
        payload["repeat_context"] = (
            "This exact set of tool names appeared in an earlier search during "
            "the current turn; this response adds no newly discovered names."
        )
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _fold_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", str(value or "").casefold())
    folded = "".join(char for char in normalized if not unicodedata.combining(char))
    return folded.replace("ı", "i")


def _tokens(value: str) -> list[str]:
    expanded = re.sub(r"[_:./-]+", " ", _fold_text(value))
    return [
        token
        for token in _TOKEN_RE.findall(expanded)
        if len(token) > 1 and token not in _STOP_WORDS
    ]


def _definition_capabilities(definition: Mapping[str, Any]) -> frozenset[str]:
    searchable = set(_search_tokens(definition))
    return frozenset(
        capability
        for capability, rule in _CAPABILITY_RULES.items()
        if searchable.intersection(rule["tool_terms"])
    )


def _intent_capabilities(intent_text: str) -> frozenset[str]:
    folded = _fold_text(intent_text)
    tokens = set(_tokens(folded))
    found: set[str] = set()
    for capability, rule in _CAPABILITY_RULES.items():
        for alias in rule["intent_aliases"]:
            folded_alias = _fold_text(alias)
            if " " in folded_alias:
                if folded_alias in folded:
                    found.add(capability)
                    break
                continue
            # Prefix matching handles inflection without making short, generic
            # fragments such as "post" match unrelated words like "postpone".
            if any(
                token == folded_alias
                or (len(folded_alias) >= 5 and token.startswith(folded_alias))
                for token in tokens
            ):
                found.add(capability)
                break
    return frozenset(found)


def _parameter_properties(definition: Mapping[str, Any]) -> Mapping[str, Any]:
    function = definition.get("function")
    if not isinstance(function, Mapping):
        return {}
    parameters = function.get("parameters")
    if not isinstance(parameters, Mapping):
        return {}
    properties = parameters.get("properties")
    return properties if isinstance(properties, Mapping) else {}


def _search_tokens(definition: Mapping[str, Any]) -> list[str]:
    name = _definition_name(definition)
    description = _definition_description(definition, limit=1_000)
    parameter_names = " ".join(str(name) for name in _parameter_properties(definition))
    return _tokens(f"{name} {description} {parameter_names}")


def _rank_definitions(
    definitions: Mapping[str, dict[str, Any]],
    query: str,
) -> list[tuple[float, str, dict[str, Any]]]:
    """Rank catalog entries with BM25 plus strong tool-name matches."""
    query_tokens = _tokens(query)
    if not query_tokens:
        return [
            (0.0, name, definition)
            for name, definition in sorted(definitions.items())
        ]

    docs = {name: _search_tokens(definition) for name, definition in definitions.items()}
    document_count = max(1, len(docs))
    average_length = sum(len(tokens) for tokens in docs.values()) / document_count
    frequencies: Counter[str] = Counter()
    for tokens in docs.values():
        frequencies.update(set(tokens))

    ranked: list[tuple[float, str, dict[str, Any]]] = []
    normalized_query = " ".join(query_tokens)
    for name, definition in definitions.items():
        document_tokens = docs[name]
        term_counts = Counter(document_tokens)
        document_length = max(1, len(document_tokens))
        score = 0.0
        for token in query_tokens:
            term_frequency = term_counts.get(token, 0)
            if not term_frequency:
                continue
            document_frequency = frequencies.get(token, 0)
            inverse_frequency = math.log(
                1 + (document_count - document_frequency + 0.5)
                / (document_frequency + 0.5)
            )
            denominator = term_frequency + 1.5 * (
                1 - 0.75 + 0.75 * document_length / max(average_length, 1.0)
            )
            score += inverse_frequency * (term_frequency * 2.5 / denominator)

        name_tokens = set(_tokens(name))
        parameter_tokens = set(_tokens(" ".join(_parameter_properties(definition))))
        score += 8.0 * len(name_tokens.intersection(query_tokens))
        score += 2.0 * len(parameter_tokens.intersection(query_tokens))
        normalized_name = " ".join(_tokens(name))
        if normalized_name and normalized_name in normalized_query:
            score += 24.0
        ranked.append((score, name, definition))

    ranked.sort(key=lambda item: (-item[0], item[1]))
    return ranked


def _mcp_source_token(name: str) -> str:
    """Return the stable server token from a normalized MCP tool name."""
    parts = str(name or "").lower().split("_")
    if len(parts) >= 3 and parts[0] == "mcp":
        return parts[1]
    return ""


def _normalize_source(value: str) -> str:
    """Return a short prompt-safe connected-source label."""
    folded = _fold_text(value)
    normalized = re.sub(r"[^a-z0-9._:-]+", "-", folded).strip("-._:")
    return normalized[:80]


def _resolved_sources(
    definitions: Mapping[str, dict[str, Any]],
    *,
    toolsets: Mapping[str, str] | None = None,
    sources: Mapping[str, str] | None = None,
) -> dict[str, str]:
    declared_toolsets = toolsets or {}
    declared_sources = sources or {}
    resolved: dict[str, str] = {}
    for name in definitions:
        explicit = _normalize_source(str(declared_sources.get(name) or ""))
        mcp_source = _normalize_source(_mcp_source_token(name))
        item_toolset = _normalize_source(
            str(declared_toolsets.get(name) or infer_toolset(name))
        )
        resolved[name] = explicit or mcp_source or item_toolset or "external"
    return resolved


def _named_sources(
    definitions: Mapping[str, dict[str, Any]],
    query: str,
    *,
    toolsets: Mapping[str, str] | None = None,
    sources: Mapping[str, str] | None = None,
) -> frozenset[str]:
    query_tokens = set(_tokens(query))
    resolved = _resolved_sources(
        definitions,
        toolsets=toolsets,
        sources=sources,
    )
    return frozenset(
        source
        for source in set(resolved.values())
        if (source_tokens := set(_tokens(source)))
        and source_tokens.issubset(query_tokens)
    )


def _rank_scoped_definitions(
    definitions: Mapping[str, dict[str, Any]],
    query: str,
    *,
    toolsets: Mapping[str, str] | None = None,
    sources: Mapping[str, str] | None = None,
) -> list[tuple[float, str, dict[str, Any]]]:
    """Rank tools, narrowing to explicitly named connected sources when present."""
    resolved = _resolved_sources(
        definitions,
        toolsets=toolsets,
        sources=sources,
    )
    named_sources = _named_sources(
        definitions,
        query,
        toolsets=toolsets,
        sources=sources,
    )
    eligible = (
        {
            name: definition
            for name, definition in definitions.items()
            if resolved.get(name) in named_sources
        }
        if named_sources
        else definitions
    )
    ranking_text = (
        " ".join(token for token in _tokens(query) if token not in named_sources)
        if named_sources
        else query
    )
    return _rank_definitions(eligible, ranking_text)


def _has_specific_promotion_evidence(
    definition: Mapping[str, Any],
    intent_text: str,
) -> bool:
    """Require a concrete capability signal for provider-agnostic promotion."""
    intent_tokens = set(_tokens(intent_text)) - _GENERIC_PROMOTION_TOKENS
    if not intent_tokens:
        return False
    name_tokens = (
        set(_tokens(_definition_name(definition)))
        - _GENERIC_PROMOTION_TOKENS
        - {"mcp"}
    )
    if name_tokens.intersection(intent_tokens):
        return True
    descriptive_tokens = (
        set(_tokens(
            _definition_description(definition, limit=1_000)
            + " "
            + " ".join(_parameter_properties(definition))
        ))
        - _GENERIC_PROMOTION_TOKENS
    )
    return len(descriptive_tokens.intersection(intent_tokens)) >= 2


def select_intent_promotions(
    definitions: Mapping[str, dict[str, Any]],
    intent_text: str,
    *,
    toolsets: Mapping[str, str] | None = None,
    sources: Mapping[str, str] | None = None,
    max_tools: int = 4,
    minimum_score: float = 4.0,
) -> tuple[str, ...]:
    """Select external schemas relevant enough to advertise eagerly."""
    if not definitions or not str(intent_text or "").strip() or max_tools <= 0:
        return ()

    resolved_sources = _resolved_sources(
        definitions,
        toolsets=toolsets,
        sources=sources,
    )
    named_sources = _named_sources(
        definitions,
        intent_text,
        toolsets=toolsets,
        sources=sources,
    )
    promoted: list[str] = []
    for score, name, definition in _rank_scoped_definitions(
        definitions,
        intent_text,
        toolsets=toolsets,
        sources=sources,
    ):
        if score < float(minimum_score):
            continue
        if not named_sources and not _has_specific_promotion_evidence(
            definition, intent_text
        ):
            continue
        promoted.append(name)
        if len(promoted) >= max_tools:
            break
    if promoted:
        return tuple(promoted)

    if named_sources:
        named_workflow = tuple(sorted(
            name
            for name in definitions
            if resolved_sources.get(name) in named_sources
        ))
        # If lexical ranking found nothing because the user's task language
        # differs from the tools' schema language, expose a small explicitly
        # named provider workflow together (for example Context7 resolve +
        # query). Large providers still stay behind search.
        if len(named_workflow) <= max_tools:
            return named_workflow

    # Provider-independent semantic fast path.  Only one uniquely matching
    # small source is promoted as a workflow.  When two providers can satisfy
    # the same request, the host stays neutral and leaves the choice to the
    # model through the connected-source catalog/search bridge.
    intent_capabilities = _intent_capabilities(intent_text)
    if not intent_capabilities:
        return ()
    source_tools: dict[str, list[str]] = {}
    source_capabilities: dict[str, set[str]] = {}
    for name, definition in definitions.items():
        source = resolved_sources[name]
        source_tools.setdefault(source, []).append(name)
        source_capabilities.setdefault(source, set()).update(
            _definition_capabilities(definition)
        )
    scored_sources = [
        (len(intent_capabilities.intersection(capabilities)), source)
        for source, capabilities in source_capabilities.items()
        if intent_capabilities.intersection(capabilities)
        and len(source_tools[source]) <= max_tools
    ]
    if not scored_sources:
        return ()
    best_score = max(score for score, _source in scored_sources)
    winners = sorted(
        source for score, source in scored_sources if score == best_score
    )
    if len(winners) != 1:
        return ()
    return tuple(sorted(source_tools[winners[0]]))


def _schema_type(schema: Any) -> str:
    if not isinstance(schema, Mapping):
        return "any"
    enum = schema.get("enum")
    if isinstance(enum, list) and enum:
        values = "|".join(str(value) for value in enum[:4])
        return f"enum({values}{'|…' if len(enum) > 4 else ''})"
    value = schema.get("type")
    if isinstance(value, list):
        return "|".join(str(item) for item in value)
    if isinstance(value, str) and value:
        if value == "array" and isinstance(schema.get("items"), Mapping):
            return f"array<{_schema_type(schema['items'])}>"
        return value
    variants = schema.get("anyOf") or schema.get("oneOf")
    if isinstance(variants, list):
        types = list(dict.fromkeys(_schema_type(item) for item in variants))
        return "|".join(types[:4]) or "any"
    return "any"


def _compact_signature(definition: Mapping[str, Any], *, max_parameters: int = 12) -> str:
    function = definition.get("function")
    if not isinstance(function, Mapping):
        return "()"
    parameters = function.get("parameters")
    if not isinstance(parameters, Mapping):
        return "()"
    properties = _parameter_properties(definition)
    required = {
        str(name)
        for name in parameters.get("required", [])
        if isinstance(name, str)
    }
    rendered = []
    for index, (name, schema) in enumerate(properties.items()):
        if index >= max_parameters:
            rendered.append("…")
            break
        marker = "*" if str(name) in required else "?"
        rendered.append(f"{name}{marker}:{_schema_type(schema)}")
    return "(" + ", ".join(rendered) + ")"


@dataclass(frozen=True)
class ToolDisclosure:
    """The exact model-visible and bridge-reachable surface for one route."""

    definitions: tuple[dict[str, Any], ...]
    direct_names: frozenset[str]
    promoted_names: frozenset[str]
    deferred: Mapping[str, dict[str, Any]]
    external: Mapping[str, dict[str, Any]]
    toolsets: Mapping[str, str]
    sources: Mapping[str, str]
    all_names: frozenset[str]
    original_schema_tokens: int
    disclosed_schema_tokens: int
    search_max_limit: int

    @property
    def enabled(self) -> bool:
        return bool(self.deferred)

    def resolve_call(self, name: str, arguments: Any) -> tuple[str, dict[str, Any]] | str:
        target = str(name or "").strip()
        if target not in self.external:
            return f"Error: External tool '{target}' is not available in this route."
        if isinstance(arguments, str):
            try:
                parsed = json.loads(arguments)
            except (TypeError, ValueError):
                parsed = None
            if isinstance(parsed, dict):
                arguments = parsed
        if not isinstance(arguments, dict):
            return "Error: 'arguments' must be a JSON object."
        return target, dict(arguments)

    def search(
        self,
        query: str = "",
        toolset: str = "",
        limit: int = 5,
        offset: int = 0,
    ) -> str:
        """Search every route-enabled external tool with stable pagination."""
        wanted_toolset = str(toolset or "").strip().lower()
        candidates = {
            name: definition
            for name, definition in self.external.items()
            if not wanted_toolset
            or self.toolsets.get(name, infer_toolset(name)) == wanted_toolset
        }
        bounded_limit = max(1, min(int(limit), self.search_max_limit))
        bounded_offset = max(0, int(offset))
        ranked = _rank_scoped_definitions(
            candidates,
            query,
            toolsets=self.toolsets,
            sources=self.sources,
        )
        selected = ranked[bounded_offset:bounded_offset + bounded_limit]
        matches: list[dict[str, Any]] = []
        for score, name, definition in selected:
            function = definition.get("function") or {}
            direct = name in self.direct_names
            matches.append({
                "name": name,
                "toolset": self.toolsets.get(name, infer_toolset(name)),
                "source": self.sources.get(name, "external"),
                "capabilities": sorted(_definition_capabilities(definition)),
                "visibility": "direct" if direct else "deferred",
                "invoke_via": [name, TOOL_CALL_NAME] if direct else [TOOL_CALL_NAME],
                "description": _definition_description(definition, limit=400),
                "input_schema": function.get("parameters") or {
                    "type": "object", "properties": {},
                },
                "score": round(score, 3),
            })
        next_offset = bounded_offset + len(matches)
        if next_offset >= len(ranked):
            next_offset = None
        return json.dumps({
            "scope": "route_enabled_external_tools",
            "matches": matches,
            "offset": bounded_offset,
            "next_offset": next_offset,
            "total_candidates": len(ranked),
            "remaining": max(0, len(ranked) - bounded_offset - len(matches)),
            "usage": (
                "Each match includes its complete input_schema. A direct match may "
                "be invoked by its exact name or through tool_call; a deferred match "
                "is invoked through tool_call. No describe step is needed."
            ),
        }, ensure_ascii=False, separators=(",", ":"))

    def describe(self, name: str) -> str:
        """Legacy compatibility for trajectories created by the old bridge."""
        target = str(name or "").strip()
        definition = self.external.get(target)
        if definition is None:
            return f"Error: External tool '{target}' is not available in this route."
        return json.dumps(definition, ensure_ascii=False, separators=(",", ":"))


def _build_catalog(
    deferred: Mapping[str, dict[str, Any]],
    *,
    toolsets: Mapping[str, str],
    sources: Mapping[str, str],
    max_chars: int,
) -> str:
    groups: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    for name, definition in deferred.items():
        source = sources.get(name) or _mcp_source_token(name)
        if not source:
            source = toolsets.get(name, infer_toolset(name))
        groups.setdefault(_normalize_source(source) or "external", []).append(
            (name, definition)
        )

    header = (
        "Connected external sources below are available now. Exact hidden tool "
        "schemas come from tool_search; do not treat a summarized source as missing."
    )

    def capabilities_for(items: list[tuple[str, dict[str, Any]]]) -> str:
        capabilities = sorted({
            capability
            for _name, definition in items
            for capability in _definition_capabilities(definition)
        })
        return ", ".join(capabilities) if capabilities else "other"

    def summary(source: str) -> str:
        items = groups[source]
        return (
            f"- {source}: {len(items)} tools; capabilities: "
            f"{capabilities_for(items)}; names via tool_search"
        )

    def details(source: str) -> str:
        items = sorted(groups[source], key=lambda item: item[0])
        lines = [
            f"{source} ({len(items)} tools; capabilities: {capabilities_for(items)}):"
        ]
        for name, definition in items:
            item_toolset = toolsets.get(name, infer_toolset(name))
            lines.append(
                f"- {name} [{item_toolset}] {_compact_signature(definition)} — "
                f"{_definition_description(definition, limit=120)}"
            )
        return "\n".join(lines)

    ordered_sources = sorted(groups)
    if not ordered_sources:
        return header

    full_blocks = {source: details(source) for source in ordered_sources}
    full = "\n".join([header, *(full_blocks[source] for source in ordered_sources)])
    if len(full) <= max_chars:
        return full

    # Every source starts represented.  Spend the remaining catalog budget on
    # the smallest detailed groups first, so one huge provider cannot hide a
    # small provider's exact workflow.  If source summaries alone exceed the
    # configured budget we keep them anyway: source reachability is a harder
    # correctness requirement than this soft prompt-size target.
    selected: dict[str, str] = {
        source: summary(source) for source in ordered_sources
    }
    by_upgrade_cost = sorted(
        ordered_sources,
        key=lambda source: (
            len(full_blocks[source]) - len(selected[source]),
            source,
        ),
    )
    for source in by_upgrade_cost:
        candidate = dict(selected)
        candidate[source] = full_blocks[source]
        rendered = "\n".join(
            [header, *(candidate[item] for item in ordered_sources)]
        )
        if len(rendered) <= max_chars:
            selected = candidate
    return "\n".join([header, *(selected[source] for source in ordered_sources)])


def _bridge_definitions(
    deferred: Mapping[str, dict[str, Any]],
    *,
    toolsets: Mapping[str, str],
    sources: Mapping[str, str],
    catalog_max_chars: int,
    search_default_limit: int,
    search_max_limit: int,
) -> list[dict[str, Any]]:
    catalog = _build_catalog(
        deferred,
        toolsets=toolsets,
        sources=sources,
        max_chars=catalog_max_chars,
    )
    return [
        {
            "type": "function",
            "function": {
                "name": TOOL_SEARCH_NAME,
                "description": (
                    "Search every route-enabled external tool by capability, provider, "
                    "name, or toolset, including tools whose full schema is already "
                    "visible. Results report direct/deferred visibility, valid invocation "
                    "options, and complete input_schema. Search is optional when a visible "
                    "schema or compact signature is already clear. The complete connected-"
                    "source index and any compact hidden signatures are:\n" + catalog
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Capability or name."},
                        "toolset": {"type": "string", "description": "Optional exact toolset."},
                        "limit": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": search_max_limit,
                            "default": search_default_limit,
                        },
                        "offset": {
                            "type": "integer",
                            "minimum": 0,
                            "default": 0,
                            "description": "Pagination offset from a previous result.",
                        },
                    },
                    "required": ["query"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": TOOL_CALL_NAME,
                "description": (
                    "Invoke any route-enabled external tool by exact name, whether its "
                    "schema is direct or deferred. This is an alternative to a direct "
                    "call. tool_search is available when the name or schema is unclear."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "Exact external tool name."},
                        "arguments": {
                            "type": "object",
                            "description": "Arguments accepted by the target tool.",
                            "additionalProperties": True,
                        },
                    },
                    "required": ["name", "arguments"],
                    "additionalProperties": False,
                },
            },
        },
    ]


def build_tool_disclosure(
    definitions: Iterable[dict[str, Any]],
    *,
    toolsets: Mapping[str, str] | None = None,
    sources: Mapping[str, str] | None = None,
    enabled: bool = True,
    deferred_toolsets: Iterable[str] = DEFAULT_DEFERRED_TOOLSETS,
    always_visible_tools: Iterable[str] = DEFAULT_ALWAYS_VISIBLE_TOOLS,
    minimum_deferred_schema_tokens: int = 750,
    catalog_max_chars: int = 6_000,
    search_default_limit: int = 5,
    search_max_limit: int = 20,
    intent_text: str = "",
    intent_routing_enabled: bool = True,
    intent_max_promoted_tools: int = 4,
    intent_min_score: float = 4.0,
    semantic_promoted_tools: Iterable[str] | None = None,
) -> ToolDisclosure:
    """Build a compact, lossless, intent-aware view of routed definitions."""
    raw = [dict(definition) for definition in definitions]
    original_tokens = estimate_schema_tokens(raw) if raw else 0
    declared_toolsets = {
        str(name).strip(): str(toolset).strip().lower()
        for name, toolset in (toolsets or {}).items()
        if str(name).strip() and str(toolset).strip()
    }
    declared_sources = {
        str(name).strip(): _normalize_source(str(source))
        for name, source in (sources or {}).items()
        if str(name).strip() and _normalize_source(str(source))
    }
    routed_toolsets: dict[str, str] = {}
    deferred_sets = {str(value).strip().lower() for value in deferred_toolsets}
    always = {str(value).strip() for value in always_visible_tools}
    external: dict[str, dict[str, Any]] = {}
    candidates: dict[str, dict[str, Any]] = {}

    for definition in raw:
        name = _definition_name(definition)
        if not name:
            continue
        item_toolset = declared_toolsets.get(name, infer_toolset(name))
        routed_toolsets[name] = item_toolset
        if item_toolset in deferred_sets:
            external[name] = definition
            if name not in always:
                candidates[name] = definition

    named_definitions = {
        name: definition
        for definition in raw
        if (name := _definition_name(definition))
    }
    routed_sources = _resolved_sources(
        named_definitions,
        toolsets=routed_toolsets,
        sources=declared_sources,
    )

    bounded_search_max = max(1, int(search_max_limit))

    bounded_promotion_limit = max(0, int(intent_max_promoted_tools))
    deterministic_promoted = (
        select_intent_promotions(
            candidates,
            intent_text,
            toolsets=routed_toolsets,
            sources=routed_sources,
            max_tools=bounded_promotion_limit,
            minimum_score=float(intent_min_score),
        )
        if enabled and intent_routing_enabled
        else ()
    )
    if enabled and intent_routing_enabled and semantic_promoted_tools is not None:
        # Semantic retrieval augments the proven deterministic fast path; it
        # never suppresses an exact provider/tool/capability match. The routed
        # candidate set remains authoritative, so stale or malicious names
        # cannot re-enable a direct/core/blocked tool.
        semantic_promoted = (
            name
            for name in dict.fromkeys(
                str(value).strip() for value in semantic_promoted_tools
            )
            if name in candidates
        )
        promoted = tuple(dict.fromkeys((
            *deterministic_promoted,
            *semantic_promoted,
        )))[:bounded_promotion_limit]
    else:
        promoted = deterministic_promoted
    promoted_names = frozenset(promoted)
    direct = [
        definition
        for definition in raw
        if _definition_name(definition) not in candidates
        or _definition_name(definition) in promoted_names
    ]
    deferred = {
        name: definition
        for name, definition in candidates.items()
        if name not in promoted_names
    }

    deferred_tokens = estimate_schema_tokens(deferred.values()) if deferred else 0
    if enabled and candidates and not deferred:
        names = frozenset(filter(None, (_definition_name(item) for item in raw)))
        return ToolDisclosure(
            definitions=tuple(raw),
            direct_names=names,
            promoted_names=promoted_names,
            deferred={},
            external=external,
            toolsets=routed_toolsets,
            sources=routed_sources,
            all_names=names,
            original_schema_tokens=original_tokens,
            disclosed_schema_tokens=original_tokens,
            search_max_limit=bounded_search_max,
        )
    if not enabled or not deferred or deferred_tokens < max(0, minimum_deferred_schema_tokens):
        names = frozenset(filter(None, (_definition_name(item) for item in raw)))
        return ToolDisclosure(
            definitions=tuple(raw),
            direct_names=names,
            promoted_names=frozenset(),
            deferred={},
            external=external,
            toolsets=routed_toolsets,
            sources=routed_sources,
            all_names=names,
            original_schema_tokens=original_tokens,
            disclosed_schema_tokens=original_tokens,
            search_max_limit=bounded_search_max,
        )

    visible = direct + _bridge_definitions(
        deferred,
        toolsets=routed_toolsets,
        sources=routed_sources,
        catalog_max_chars=max(500, int(catalog_max_chars)),
        search_default_limit=max(1, min(int(search_default_limit), bounded_search_max)),
        search_max_limit=bounded_search_max,
    )
    disclosed_tokens = estimate_schema_tokens(visible)
    # Never pay more per model round merely to install the bridge.  This also
    # keeps tiny one-tool MCP servers eager, where progressive disclosure has
    # no economic benefit.
    if disclosed_tokens >= original_tokens:
        names = frozenset(filter(None, (_definition_name(item) for item in raw)))
        return ToolDisclosure(
            definitions=tuple(raw),
            direct_names=names,
            promoted_names=frozenset(),
            deferred={},
            external=external,
            toolsets=routed_toolsets,
            sources=routed_sources,
            all_names=names,
            original_schema_tokens=original_tokens,
            disclosed_schema_tokens=original_tokens,
            search_max_limit=bounded_search_max,
        )

    direct_names = frozenset(filter(None, (_definition_name(item) for item in direct)))
    all_names = frozenset({*direct_names, *deferred})
    return ToolDisclosure(
        definitions=tuple(visible),
        direct_names=direct_names,
        promoted_names=promoted_names,
        deferred=deferred,
        external=external,
        toolsets=routed_toolsets,
        sources=routed_sources,
        all_names=all_names,
        original_schema_tokens=original_tokens,
        disclosed_schema_tokens=disclosed_tokens,
        search_max_limit=bounded_search_max,
    )
