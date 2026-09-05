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

import json
import logging
import os
import re
from typing import Any
from urllib.parse import parse_qsl, quote, quote_plus, unquote, urlsplit

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
MAX_DIAGNOSTIC_INPUT = 64 * 1024
MAX_DIAGNOSTIC_OUTPUT = 4096
_OMITTED = "[Diagnostic omitted: size or complexity limit exceeded]"
_REDACTED = "[REDACTED]"
_CREDENTIAL_PATTERN = re.compile(
    r"(?:ghp_|github_pat_|sk-|xox[baprs]-|xapp-)[A-Za-z0-9_\-]+"
    r"|\b(?:Bearer|Basic)\s+[^\s,;\"'<>]+", re.I,
)
_PRIVATE_KEY = re.compile(
    r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----.*?"
    r"(?:-----END (?:[A-Z0-9]+ )*PRIVATE KEY-----|\Z)", re.S,
)
_USERINFO = re.compile(r"(\b[a-z][a-z0-9+.-]*://)[^\s/\"'<>]*@", re.I)
_HEADER = re.compile(r"\b(?:cookie|set-cookie|authorization)\s*:\s*[^\r\n]+", re.I)
_FIELD = re.compile(
    r'''(?P<key>"(?:[^"\\]|\\.){1,256}"|'(?:[^'\\]|\\.){1,256}'|[\w.%+-]{1,256})'''
    r'''\s*[:=]\s*''',
)
_VALUE = re.compile(
    r'''"(?:[^"\\]|\\.)*(?:"|\Z)|'(?:[^'\\]|\\.)*(?:'|\Z)|[^\s&,;\}\]]+''',
)
_CONTROLS = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]")


def _sensitive_key(key: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", unquote(key).lower())
    return normalized in {"key", "auth", "authorization", "cookie", "setcookie", "credentials"} or normalized.endswith((
        "token", "secret", "password", "passwd", "apikey", "accesskey", "privatekey", "credential",
    ))


def _secret_variants(secrets: Any) -> tuple[str, ...]:
    """Bound work even if a caller supplies pathological configuration."""
    found: set[str] = set()
    total = 0
    for index, value in enumerate(secrets):
        if index >= 256:
            raise ValueError("Too many diagnostic secrets")
        if not isinstance(value, str) or not value:
            continue
        total += len(value)
        if total > MAX_DIAGNOSTIC_INPUT:
            raise ValueError("Diagnostic secrets exceed the work limit")
        encoded = quote(value, safe="")
        found.update((
            value, encoded, quote_plus(value, safe=""),
            re.sub(r"%[0-9A-F]{2}", lambda match: match[0].lower(), encoded),
            json.dumps(value, ensure_ascii=True)[1:-1], json.dumps(value, ensure_ascii=False)[1:-1],
        ))
        if value.lower().startswith(("bearer ", "basic ")):
            found.add(value.split(None, 1)[1])
    # Replacing a prefix first must not leave the remainder of a longer key.
    return tuple(sorted(found, key=len, reverse=True))


def _redact_text(text: str, variants: tuple[str, ...]) -> str:
    if len(text) > MAX_DIAGNOSTIC_INPUT:
        return _OMITTED
    if variants:
        # One substitution pass; never redact inside a replacement marker.
        text = re.sub("|".join(re.escape(secret) for secret in variants), lambda _: _REDACTED, text)
    text = _PRIVATE_KEY.sub(_REDACTED, text)
    text = _USERINFO.sub(lambda match: match[1] + _REDACTED + "@", text)
    text = _CREDENTIAL_PATTERN.sub(_REDACTED, text)
    text = _HEADER.sub(_REDACTED, text)

    pieces = []
    position = emitted = count = 0
    while match := _FIELD.search(text, position):
        count += 1
        if count > 256:
            return _OMITTED
        key = match["key"]
        if key.startswith('"'):
            try:
                key = json.loads(key)
            except ValueError:
                pass
        position = match.end()
        if not _sensitive_key(key):
            # A non-secret outer field must not swallow a nested secret.
            continue
        pieces.append(text[emitted:match.start()])
        pieces.append(_REDACTED)
        if text[position:position + 1] in ("{", "["):
            # Unstructured exception text is not necessarily valid JSON. Do
            # not try to balance a secret-valued object with a regex.
            return "".join(pieces)
        value = _VALUE.match(text, position)
        if value:
            position = value.end()
        emitted = position
    pieces.append(text[emitted:])
    return "".join(pieces)


def _scrub(value: Any, variants: tuple[str, ...], budget: list[int], depth: int = 0) -> Any:
    budget[0] -= 1
    if budget[0] < 0 or depth > 12:
        return _OMITTED
    if isinstance(value, str):
        if len(value) > MAX_DIAGNOSTIC_INPUT:
            return _OMITTED
        # Decode complete JSON (including escaped keys) before looking at
        # labels. The shared depth/node budget also covers JSON-in-JSON.
        if value.lstrip().startswith(("{", "[")):
            try:
                parsed = json.loads(value)
            except (ValueError, RecursionError):
                pass
            else:
                return json.dumps(_scrub(parsed, variants, budget, depth + 1), ensure_ascii=False)
        return _redact_text(value, variants)
    if value is None or isinstance(value, (bool, float)):
        return value
    if isinstance(value, int):
        return value if value.bit_length() <= 256 else _OMITTED
    if isinstance(value, dict):
        result = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= 128 or budget[0] <= 0:
                result["omitted"] = _OMITTED
                break
            if not isinstance(key, str) or len(key) > 256:
                result["omitted-key"] = _OMITTED
                continue
            result[_redact_text(key, variants)] = _REDACTED if _sensitive_key(key) else _scrub(item, variants, budget, depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        return [_scrub(item, variants, budget, depth + 1) for item in value[:128]] + ([_OMITTED] if len(value) > 128 else [])
    # Diagnostics are JSON-like data, never an excuse to call an arbitrary
    # object's potentially expensive or credential-bearing __str__/__repr__.
    return "[Unsupported diagnostic value]"


def safe_diagnostic(value: Any, *, secrets: Any = (), limit: int = MAX_DIAGNOSTIC_OUTPUT) -> str:
    """Redact and bound untrusted text/JSON for error or log surfaces only.

    This is defense in depth, not detection of every arbitrary secret. Known
    connection values supplement common credential labels and token formats.
    Oversized inputs are omitted whole: slicing raw text could expose a partial
    credential. Successful tool payloads must not go through this function.
    """
    limit = max(1, min(limit, MAX_DIAGNOSTIC_INPUT))
    try:
        scrubbed = _scrub(value, _secret_variants(secrets), [256])
        text = scrubbed if isinstance(scrubbed, str) else json.dumps(scrubbed, ensure_ascii=False)
    except (ValueError, TypeError, RecursionError):
        text = _OMITTED
    text = _CONTROLS.sub(lambda match: f"\\u{ord(match[0]):04x}", text)
    return text if len(text) <= limit else text[:max(0, limit - 14)] + "...[truncated]"


def diagnostic_secrets(config: dict | None) -> tuple[str, ...]:
    """Only this connection's supplied credentials, never global env scanning.

    Explicit env values and custom HTTP header values are conservatively
    treated as secret; common routing/runtime values are excluded. A sentinel
    over the redactor's budget fails closed for oversized configurations.
    """
    if not isinstance(config, dict):
        return ()
    result: set[str] = set()
    total = 0

    def add(value: Any):
        nonlocal total
        if isinstance(value, str) and value:
            value = interpolate_env_vars(value)
            if value not in result:
                total += len(value)
                result.add(value)
            if len(result) > 256 or total > MAX_DIAGNOSTIC_INPUT:
                raise ValueError("Diagnostic secret budget exceeded")

    try:
        for kind in ("env", "headers"):
            values = config.get(kind)
            if isinstance(values, dict):
                if len(values) > 256:
                    raise ValueError("Too many diagnostic configuration values")
                for key, value in values.items():
                    public = key in _SAFE_ENV_KEYS or str(key).startswith("XDG_") if kind == "env" else str(key).lower() in {
                        "accept", "content-type", "user-agent", "mcp-protocol-version",
                    }
                    if not public:
                        add(value)
        args = config.get("args") or []
        if not isinstance(args, (list, tuple)) or len(args) > 256:
            raise ValueError("Too many diagnostic arguments")
        previous_sensitive = False
        for argument in args:
            if not isinstance(argument, str):
                continue
            if previous_sensitive:
                add(argument)
            label, separator, value = argument.lstrip("-").partition("=")
            previous_sensitive = argument.startswith("-") and _sensitive_key(label) and not separator
            if separator and _sensitive_key(label):
                add(value)
        raw_url = config.get("url") or ""
        if isinstance(raw_url, str):
            if len(raw_url) > MAX_DIAGNOSTIC_INPUT:
                raise ValueError("Diagnostic URL exceeds the work limit")
            url = urlsplit(interpolate_env_vars(raw_url))
            if url.password:
                add(unquote(url.password))
            if url.username:
                add(unquote(url.username))
            for key, value in parse_qsl(url.query, max_num_fields=256):
                if _sensitive_key(key):
                    add(value)
        return tuple(result)
    except (ValueError, TypeError):
        return ("x" * (MAX_DIAGNOSTIC_INPUT + 1),)


def exception_diagnostic(exc: BaseException, *, secrets: Any = ()) -> str:
    """Iteratively inspect a bounded number of ExceptionGroup leaves."""
    pending = [exc]
    parts: list[str] = []
    visited: set[int] = set()
    while pending and len(visited) < 128 and len(parts) < 6:
        current = pending.pop()
        if id(current) in visited:
            continue
        visited.add(id(current))
        nested = getattr(current, "exceptions", None)
        if isinstance(nested, tuple):
            pending.extend(child for child in reversed(nested[:128]) if isinstance(child, BaseException))
            continue
        try:
            raw = str(current).strip() or type(current).__name__
        except Exception:
            raw = "Unprintable exception"
        rendered = sanitize_error(raw, secrets=secrets)
        if rendered not in parts:
            parts.append(rendered)
    if pending:
        parts.append(_OMITTED)
    return sanitize_error("; ".join(parts) or "Unspecified transport failure", secrets=secrets)


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


def sanitize_error(text: str, *, secrets: Any = (), limit: int = MAX_DIAGNOSTIC_OUTPUT) -> str:
    """Credential-safe, bounded error text (never raw prefix truncation)."""
    return safe_diagnostic(text, secrets=secrets, limit=limit)


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


def scan_description(server_name: str, tool_name: str, description: str, *, secrets: Any = ()) -> list[str]:
    """Log a warning for each injection pattern matched in *description*.

    Returns the list of finding labels (empty when clean) — useful for
    tests and for any future "explain why this tool looks suspicious"
    surface.
    """
    if not description:
        return []
    findings = [reason for pattern, reason in _INJECTION_PATTERNS if pattern.search(description[:MAX_DIAGNOSTIC_INPUT])]
    if findings:
        logger.warning(
            "MCP server '%s' tool '%s': suspicious description — %s. "
            "Description: %.200s",
            sanitize_error(server_name, secrets=secrets, limit=200), sanitize_error(tool_name, secrets=secrets, limit=200),
            "; ".join(findings), sanitize_error(description, secrets=secrets, limit=200),
        )
    return findings
