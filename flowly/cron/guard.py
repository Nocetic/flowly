"""Prompt-injection scanning.

Two use cases share the same threat catalog:

  1. **Cron job creation** — scans the prompt an agent is about to
     persist as a scheduled task. Originally the only caller; the old
     `scan_cron_prompt` name is kept as an alias for callers.

  2. **Context file ingestion** — scans AGENTS.md / SOUL.md / USER.md /
     TOOLS.md / IDENTITY.md before they're injected into the main system
     prompt. If one of these files is poisoned (sync conflict, skill
     accidentally writing an instruction, user pasting untrusted snippet)
     the agent would otherwise follow the embedded directive.

Both modes share `scan_content(text, label, mode)`. Regex + invisible-
unicode detection; no LLM cost.
"""

from __future__ import annotations

import re
from typing import Literal


# Critical-severity patterns — block both prompt stores and context
# files. Covers known prompt-injection payloads plus cron-specific
# shell escalations (ssh_backdoor, sudoers_mod, destructive_root_rm).
_THREAT_PATTERNS: list[tuple[str, str]] = [
    (r'ignore\s+(?:\w+\s+)*(?:previous|all|above|prior)\s+(?:\w+\s+)*instructions', "prompt_injection"),
    (r'do\s+not\s+tell\s+the\s+user', "deception_hide"),
    (r'system\s+prompt\s+override', "sys_prompt_override"),
    (r'disregard\s+(your|all|any)\s+(instructions|rules|guidelines)', "disregard_rules"),
    (r"act\s+as\s+(if|though)\s+you\s+(have\s+no|don'?t\s+have)\s+(restrictions|limits|rules)", "bypass_restrictions"),
    (r'<!--[^>]*(?:ignore|override|system|secret|hidden)[^>]*-->', "html_comment_injection"),
    (r'<\s*div\s+style\s*=\s*["\'][\s\S]*?display\s*:\s*none', "hidden_div"),
    (r'translate\s+.*\s+into\s+.*\s+and\s+(execute|run|eval)', "translate_execute"),
    (r'curl\s+[^\n]*\$\{?\w*(KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|API)', "exfil_curl"),
    (r'wget\s+[^\n]*\$\{?\w*(KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|API)', "exfil_wget"),
    (r'cat\s+[^\n]*(\.env|credentials|\.netrc|\.pgpass)', "read_secrets"),
    (r'authorized_keys', "ssh_backdoor"),
    (r'/etc/sudoers|visudo', "sudoers_mod"),
    (r'rm\s+-rf\s+/', "destructive_root_rm"),
]

# Zero-width + bidi override characters used to hide payloads from
# human review while still being read by the model.
_INVISIBLE_CHARS: set[str] = {
    '\u200b', '\u200c', '\u200d', '\u2060', '\ufeff',
    '\u202a', '\u202b', '\u202c', '\u202d', '\u202e',
}


# Backwards-compat aliases for any external callers.
_CRON_THREAT_PATTERNS = _THREAT_PATTERNS
_CRON_INVISIBLE_CHARS = _INVISIBLE_CHARS


def _find_threats(text: str) -> list[str]:
    """Return the ids of every threat signal found in `text`.

    Empty/None text → []. Runs the regex catalog + invisible-char scan.
    Caller decides what to do with the findings (reject vs. placeholder).
    """
    if not text:
        return []
    findings: list[str] = []
    for char in _INVISIBLE_CHARS:
        if char in text:
            findings.append(f"invisible_unicode_U+{ord(char):04X}")
            break  # one invisible-char hit is enough to signal tampering
    for pattern, pid in _THREAT_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            findings.append(pid)
    return findings


def scan_cron_prompt(prompt: str) -> str:
    """Scan a cron prompt for critical threats.

    Returns an error message if the prompt should be blocked, or an
    empty string if it is safe. Empty/None prompts always pass (they
    are handled by caller-side required-field validation).
    """
    findings = _find_threats(prompt)
    if not findings:
        return ""
    first = findings[0]
    if first.startswith("invisible_unicode_"):
        return (
            f"Blocked: prompt contains {first} (possible injection)."
        )
    return (
        f"Blocked: prompt matches threat pattern '{first}'. "
        "Cron prompts must not contain injection or exfiltration payloads."
    )


def scan_context_file(content: str, filename: str) -> str | None:
    """Scan a bootstrap/context file for prompt injection.

    Returns:
      * `None` if the content is safe to inject as-is.
      * A `[BLOCKED: ...]` placeholder string if threats were detected —
        the caller should inject this placeholder in place of the real
        content so the agent sees WHY the file was suppressed without
        following the embedded directives.

    Context-file scan semantics — scanned files
    are not silently dropped, they're replaced with an explanatory
    marker so debugging stays easy.
    """
    findings = _find_threats(content)
    if not findings:
        return None
    return (
        f"[BLOCKED: {filename} contained potential prompt injection "
        f"({', '.join(findings)}). Content not loaded.]"
    )
