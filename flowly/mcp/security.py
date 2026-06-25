"""MCP-specific security helpers.

Four responsibilities:

1. :func:`build_safe_env` — return an env dict for stdio MCP subprocesses
   that excludes Flowly-managed secrets. Without this, every MCP server
   we spawn inherits ``OPENROUTER_API_KEY``, provider tokens, etc.

2. :func:`interpolate_env_vars` — recursively resolve ``${VAR}``
   placeholders in config strings (env values, headers, args). Lets
   users keep secrets out of ``config.json`` and in ``$FLOWLY_HOME/.env``
   instead.

3. :func:`sanitize_error` — redact credential-shaped substrings from
   text before it lands in agent-visible error messages or audit logs.

4. :func:`scan_description` — pattern-match MCP tool descriptions for
   prompt-injection attempts. **Log-only**: real-world MCP servers
   sometimes legitimately include strings that match these patterns
   (security tooling, documentation snippets), so blocking would break
   correct servers. Logging gives operators a way to spot a hostile
   server post-hoc.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any


logger = logging.getLogger(__name__)


# Env vars safe to pass through to MCP subprocesses unconditionally.
# Subprocesses additionally receive every ``XDG_*`` var and whatever the
# user explicitly listed under the server's ``env`` config.
_SAFE_ENV_KEYS = frozenset({
    "PATH", "HOME", "USER", "LANG", "LC_ALL", "TERM", "SHELL", "TMPDIR",
})


# Credential patterns redacted from error messages and stderr leakage.
# Order matters: more specific patterns first so they don't get
# swallowed by broader ones.
_CREDENTIAL_PATTERN = re.compile(
    r"(?:"
    r"ghp_[A-Za-z0-9_]{1,255}"
    r"|github_pat_[A-Za-z0-9_]{1,255}"
    r"|sk-[A-Za-z0-9_\-]{1,255}"
    r"|xoxb-[A-Za-z0-9-]{1,255}"
    r"|xapp-[A-Za-z0-9-]{1,255}"
    r"|Bearer\s+[A-Za-z0-9_\-\.]+"
    r"|token=[^\s&,;\"']{1,255}"
    r"|key=[^\s&,;\"']{1,255}"
    r"|API_KEY=[^\s&,;\"']{1,255}"
    r"|password=[^\s&,;\"']{1,255}"
    r"|secret=[^\s&,;\"']{1,255}"
    r")",
    re.IGNORECASE,
)


# ``${VAR_NAME}`` style env var interpolation. ``VAR_NAME`` may contain
# any non-} character so dotted/dashed env names work.
_ENV_VAR_PATTERN = re.compile(r"\$\{([^}]+)\}")


def build_safe_env(user_env: dict[str, str] | None) -> dict[str, str]:
    """Build a stdio subprocess env that excludes Flowly secrets.

    Pass-through rules:
    1. Every key in :data:`_SAFE_ENV_KEYS` is copied from ``os.environ``.
    2. Every ``XDG_*`` env var is copied (theme/runtime hints).
    3. Every entry in ``user_env`` is added last and overrides 1/2 on
       key collision.
    """
    env: dict[str, str] = {}
    for key, value in os.environ.items():
        if key in _SAFE_ENV_KEYS or key.startswith("XDG_"):
            env[key] = value
    if user_env:
        env.update(user_env)
    return env


def interpolate_env_vars(value: Any) -> Any:
    """Recursively substitute ``${VAR}`` placeholders from ``os.environ``.

    Unresolved placeholders are left as-is so misconfiguration is visible
    in error messages (rather than silently becoming empty strings).
    Supports nested dicts and lists; non-string scalars pass through
    unchanged.
    """
    if isinstance(value, str):
        return _ENV_VAR_PATTERN.sub(
            lambda m: os.environ.get(m.group(1), m.group(0)),
            value,
        )
    if isinstance(value, dict):
        return {k: interpolate_env_vars(v) for k, v in value.items()}
    if isinstance(value, list):
        return [interpolate_env_vars(item) for item in value]
    return value


def sanitize_error(text: str) -> str:
    """Replace credential-shaped substrings in *text* with ``[REDACTED]``."""
    if not text:
        return text
    return _CREDENTIAL_PATTERN.sub("[REDACTED]", text)


# Prompt-injection patterns. Each pattern fires a WARNING log when matched
# against an MCP tool description. We log, we never block.
_INJECTION_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"ignore\s+(all\s+)?previous\s+instructions", re.I),
     "prompt override attempt"),
    (re.compile(r"you\s+are\s+now\s+a", re.I),
     "identity override attempt"),
    (re.compile(r"your\s+new\s+(task|role|instructions?)\s+(is|are)", re.I),
     "task override attempt"),
    (re.compile(r"system\s*:\s*", re.I),
     "system prompt injection attempt"),
    (re.compile(r"<\s*(system|human|assistant)\s*>", re.I),
     "role tag injection attempt"),
    (re.compile(r"do\s+not\s+(tell|inform|mention|reveal)", re.I),
     "concealment instruction"),
    (re.compile(r"(curl|wget|fetch)\s+https?://", re.I),
     "network command in description"),
    (re.compile(r"base64\.(b64decode|decodebytes)", re.I),
     "base64 decode reference"),
    (re.compile(r"exec\s*\(|eval\s*\(", re.I),
     "code execution reference"),
    (re.compile(r"import\s+(subprocess|os|shutil|socket)", re.I),
     "dangerous import reference"),
]


def scan_description(server_name: str, tool_name: str, description: str) -> list[str]:
    """Log a warning for each injection pattern matched in *description*.

    Returns the list of finding labels (empty when clean) — useful for
    tests and for any future "explain why this tool looks suspicious"
    surface.
    """
    if not description:
        return []
    findings = [reason for pattern, reason in _INJECTION_PATTERNS if pattern.search(description)]
    if findings:
        logger.warning(
            "MCP server '%s' tool '%s': suspicious description — %s. "
            "Description: %.200s",
            server_name, tool_name, "; ".join(findings), description,
        )
    return findings
