"""Deterministic, provider-neutral routing for large capability guides.

The structured tool schema remains visible to the model.  This module only
decides which *long-form operating playbooks* are useful for the current turn.
It intentionally performs no LLM or embedding call: routing must save tokens
without adding latency, cost, or a discovery round.

The policy is behaviour-first:

* explicit capability intent activates the matching guide;
* short continuation turns inherit a recent capability from history;
* ambiguous UI action requests fail open to every available UI guide;
* ordinary conversational turns activate nothing.

Runtime safety still belongs to the tools and approval layer.  A guide is
advice, never an authorization boundary.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Iterable

COMPUTER_CAPABILITY = "computer"
BROWSER_CAPABILITY = "browser"


@dataclass(frozen=True)
class CapabilityGuidanceDecision:
    """Result of one deterministic guidance-routing pass."""

    active: frozenset[str]
    confidence: str
    reason: str
    fail_open: bool = False


_CONTINUATION_RE = re.compile(
    r"(?:^|\b)(?:continue|go on|keep going|proceed|resume|try again|retry|"
    r"do it|do that|one more time|devam(?:\s+et)?|sürdür|ilerle|tekrar|"
    r"yeniden|bir daha|onu yap|bunu yap|tamam devam)(?:\b|$)",
    re.IGNORECASE,
)

# Deliberately specific.  Generic words such as "web", "search", "page" or
# Turkish "ara" belong to web_search as often as they belong to a visible
# browser and would make greetings/research turns pay the browser playbook.
_BROWSER_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\bbrowser_tab\b",
        r"\b(?:browser|chrome|safari|firefox|edge)\b",
        r"\b(?:browser tab|new tab|current tab|active tab)\b",
        r"\b(?:website|web site)\b",
        r"\bhttps?://",
        r"\b(?:open|visit|navigate to|go to)\s+(?:the\s+)?(?:site|website|url)\b",
        r"\b(?:click|fill|type|select|drag|upload)\b.{0,80}\b(?:site|website|browser|tab)\b",
        r"\b(?:tarayıcı|sekme|internet sitesi|web sitesi)\b",
        r"\bsiteye\s+(?:gir|git|aç)\b",
        r"\b(?:tıkla|doldur|yaz|seç|sürükle|yükle)\b.{0,80}\b(?:site|tarayıcı|sekme)\b",
        r"\b(?:google sheets|google docs|notion|figma)\b",
    )
)

_COMPUTER_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\bcomputer(?:\s+use)?\b",
        r"\bcomputer\s+tool\b",
        r"\b(?:desktop|screen|mouse|cursor|keyboard|window|menu bar|dock)\b",
        r"\b(?:click|double[- ]click|right[- ]click|drag|scroll|type|press)\b.{0,80}\b(?:app|window|screen|button|menu|dialog)\b",
        r"\b(?:open|close|quit|launch|switch to)\b.{0,60}\b(?:app|application)\b",
        r"\b(?:ekran|fare|imleç|klavye|pencere|masaüstü|menü çubuğu)\b",
        r"\b(?:tıkla|çift tıkla|sağ tıkla|sürükle|kaydır|tuşuna bas)\b",
        r"\b(?:uygulamayı|programı|pencereyi)\s+(?:aç|kapat|değiştir)\b",
        r"\b(?:finder|terminal|system settings|sistem ayarları|preview|textedit)\b",
    )
)

_AMBIGUOUS_UI_ACTION_RE = re.compile(
    # ``type`` / Turkish ``yaz`` are intentionally absent here: in a chat
    # request, "type/write only OK" asks for a textual response far more often
    # than desktop automation.  Targeted typing still matches the browser and
    # computer patterns above, and continuation turns inherit recent UI tools.
    r"(?:^|\b)(?:click|double[- ]click|right[- ]click|drag|scroll|press|"
    r"open|close|quit|launch|select|fill|upload|download|do it|do that|"
    r"tıkla|sürükle|kaydır|bas|aç|kapat|seç|doldur|yükle|indir|"
    r"bunu yap|onu yap)(?:\b|$)",
    re.IGNORECASE,
)


def _matches_any(text: str, patterns: Iterable[re.Pattern[str]]) -> bool:
    return any(pattern.search(text) for pattern in patterns)


def _is_short_continuation(text: str) -> bool:
    """Return whether *text* is principally a continuation instruction.

    Continuation words can occur inside unrelated full tasks (for example,
    ``gereksiz tekrar arama yapma``).  Only a compact turn whose continuation
    phrase appears near the beginning may inherit a prior UI capability.
    """

    match = _CONTINUATION_RE.search(text)
    if match is None:
        return False
    words = re.findall(r"\w+", text, flags=re.UNICODE)
    prefix_words = re.findall(r"\w+", text[:match.start()], flags=re.UNICODE)
    return len(words) <= 12 and len(prefix_words) <= 3


def _tool_names_from_history(history: list[dict[str, Any]]) -> list[str]:
    """Return recent effective tool names, newest first.

    Both direct calls and the deferred ``tool_call`` bridge are understood.
    Parsing is best-effort; malformed historical payloads are simply ignored.
    """

    names: list[str] = []
    for message in reversed(history[-16:]):
        if not isinstance(message, dict):
            continue
        if message.get("role") == "tool":
            name = message.get("name")
            if isinstance(name, str) and name:
                names.append(name)
        for call in reversed(message.get("tool_calls") or []):
            if not isinstance(call, dict):
                continue
            function = call.get("function")
            if not isinstance(function, dict):
                continue
            name = function.get("name")
            if not isinstance(name, str) or not name:
                continue
            if name == "tool_call":
                args = function.get("arguments")
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except (TypeError, ValueError, json.JSONDecodeError):
                        args = {}
                if isinstance(args, dict) and isinstance(args.get("name"), str):
                    names.append(args["name"])
            else:
                names.append(name)
    return names


def _sticky_capabilities(
    history: list[dict[str, Any]],
    available: frozenset[str],
) -> frozenset[str]:
    """Infer a continuation capability from recent calls/text."""

    for name in _tool_names_from_history(history):
        lowered = name.lower()
        if lowered in {"browser_tab", "browser_plan"} and BROWSER_CAPABILITY in available:
            return frozenset({BROWSER_CAPABILITY})
        if lowered in {"computer", "screenshot"} and COMPUTER_CAPABILITY in available:
            return frozenset({COMPUTER_CAPABILITY})

    # No tool has run yet (for example, the prior turn asked for clarification).
    # Consult only the last few user turns, newest first, so an old browser task
    # does not make an unrelated "continue" weeks later sticky.
    recent_user_texts = [
        str(message.get("content") or "")
        for message in history[-8:]
        if isinstance(message, dict)
        and message.get("role") == "user"
        and isinstance(message.get("content"), str)
    ]
    for text in reversed(recent_user_texts):
        browser = (
            BROWSER_CAPABILITY in available
            and _matches_any(text, _BROWSER_PATTERNS)
        )
        computer = (
            COMPUTER_CAPABILITY in available
            and _matches_any(text, _COMPUTER_PATTERNS)
        )
        if browser:
            return frozenset({BROWSER_CAPABILITY})
        if computer:
            return frozenset({COMPUTER_CAPABILITY})
    return frozenset()


def route_capability_guidance(
    current_message: str,
    history: list[dict[str, Any]],
    available_tools: set[str] | frozenset[str] | None,
) -> CapabilityGuidanceDecision:
    """Choose large UI playbooks for this turn without a model call."""

    tool_names = frozenset(available_tools or ())
    available_capabilities = frozenset(
        capability
        for capability, required_tool in (
            (COMPUTER_CAPABILITY, "computer"),
            (BROWSER_CAPABILITY, "browser_tab"),
        )
        if required_tool in tool_names
    )
    if not available_capabilities:
        return CapabilityGuidanceDecision(
            active=frozenset(),
            confidence="certain",
            reason="no routed UI capability tools",
        )

    text = current_message or ""
    browser_match = (
        BROWSER_CAPABILITY in available_capabilities
        and _matches_any(text, _BROWSER_PATTERNS)
    )
    computer_match = (
        COMPUTER_CAPABILITY in available_capabilities
        and _matches_any(text, _COMPUTER_PATTERNS)
    )

    if browser_match:
        # Prefer the purpose-built structured browser surface.  A browser task
        # should not also pay the OS-level guide unless it explicitly mentions
        # both capabilities.
        active = {BROWSER_CAPABILITY}
        if computer_match and re.search(r"\bcomputer(?:\s+use|\s+tool)?\b", text, re.I):
            active.add(COMPUTER_CAPABILITY)
        return CapabilityGuidanceDecision(
            active=frozenset(active),
            confidence="high",
            reason="explicit browser capability intent",
        )

    if computer_match:
        return CapabilityGuidanceDecision(
            active=frozenset({COMPUTER_CAPABILITY}),
            confidence="high",
            reason="explicit computer capability intent",
        )

    if _is_short_continuation(text):
        sticky = _sticky_capabilities(history, available_capabilities)
        if sticky:
            return CapabilityGuidanceDecision(
                active=sticky,
                confidence="high",
                reason="continuation inherited recent capability",
            )
        # A context-free "do it/continue" is exactly where guessing lean can
        # break a task.  Preserve the legacy eager guides until context exists.
        return CapabilityGuidanceDecision(
            active=available_capabilities,
            confidence="low",
            reason="ambiguous continuation without capability history",
            fail_open=True,
        )

    if _AMBIGUOUS_UI_ACTION_RE.search(text):
        return CapabilityGuidanceDecision(
            active=available_capabilities,
            confidence="low",
            reason="ambiguous UI action",
            fail_open=True,
        )

    return CapabilityGuidanceDecision(
        active=frozenset(),
        confidence="high",
        reason="no UI capability intent",
    )
