"""Structured audit logging for the account/auth subsystem.

What gets logged
----------------
Every auth-relevant event (login start/success/fail, token refresh,
keychain access, server registration, /api calls) emits a structured
record with:

  - timestamp (ISO 8601, UTC)
  - level (DEBUG/INFO/WARN/ERROR)
  - event (snake_case verb, e.g. ``login.started``)
  - correlation_id (UUID4, propagated across the whole login flow)
  - context (free-form dict — never includes secrets)

Why a separate logger?
----------------------
Auth events have higher retention/auditability requirements than chat
logs. Keeping them in a dedicated file means we can ship just that to
an SIEM (Splunk, Datadog) without leaking conversation content.

Storage layout
--------------
``~/.flowly/logs/auth.jsonl`` — newline-delimited JSON, rotated daily.
Symlink ``auth.log`` points at the current file for tail-ability.

Secrets policy
--------------
Never log: id_token, refresh_token, custom_token, gateway_auth_token,
DEK, KEK, raw machine fingerprint (only the hash prefix).
Always OK to log: user_id, email, machine_id (hashed), code (device-code
is single-use + 5min TTL — low risk).
"""

from __future__ import annotations

import contextvars
import os
import uuid
from pathlib import Path
from typing import Any

from loguru import logger

LOG_DIR = Path.home() / ".flowly" / "logs"
LOG_FILE = LOG_DIR / "auth.jsonl"

# Per-task correlation ID (propagates through awaits via contextvars).
_correlation_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "flowly_auth_correlation_id", default=""
)

_configured = False


def _ensure_configured() -> None:
    """Wire up the loguru sink the first time we log anything."""
    global _configured
    if _configured:
        return
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    # Strip default stderr sink — TUI owns the terminal, we never want
    # log output racing with rendered UI. We also avoid stdout entirely.
    logger.remove()

    # Structured JSON file sink with daily rotation and 30-day retention.
    #
    # NOTE: enqueue=True would normally be safer (async sink) but on Python
    # 3.14 loguru's multiprocessing-Queue backing fork()s a worker, and
    # that fork inherits Textual's exotic high-numbered fds → posix
    # validation rejects them ('bad value(s) in fds_to_keep'). Loguru
    # without enqueue is still thread-safe via an internal RLock; the
    # only cost is sink writes happen on the calling thread. For our
    # auth events that's a few writes per login — fine.
    logger.add(
        str(LOG_FILE),
        rotation="00:00",          # midnight UTC
        retention="30 days",
        compression="gz",
        serialize=True,            # JSON output
        enqueue=False,             # avoid multiprocessing fork on Python 3.14
        backtrace=False,
        diagnose=False,            # don't dump local variables (secrets!)
        level="DEBUG",
    )

    # Optional debug overflow to stderr when FLOWLY_AUTH_DEBUG=1
    if os.environ.get("FLOWLY_AUTH_DEBUG") == "1":
        import sys
        logger.add(sys.stderr, level="DEBUG", serialize=False,
                   format="<dim>{time:HH:mm:ss}</dim> <level>{level: <5}</level> "
                          "<cyan>{extra[event]}</cyan> {message} "
                          "<dim>cid={extra[correlation_id]}</dim>")

    _configured = True


def new_correlation_id() -> str:
    """Start a fresh correlation ID for a new top-level flow (e.g. login)."""
    cid = uuid.uuid4().hex[:16]
    _correlation_id.set(cid)
    return cid


def current_correlation_id() -> str:
    """Get the current correlation ID, generating one if absent."""
    cid = _correlation_id.get()
    if not cid:
        cid = new_correlation_id()
    return cid


def log_event(event: str, level: str = "INFO", **fields: Any) -> None:
    """Emit a structured event with the current correlation ID."""
    _ensure_configured()
    cid = current_correlation_id()
    bound = logger.bind(event=event, correlation_id=cid, **fields)
    method = getattr(bound, level.lower(), bound.info)
    method(event)


# ── convenience shortcuts ──────────────────────────────────────────


def info(event: str, **fields: Any) -> None:  log_event(event, "INFO", **fields)
def warn(event: str, **fields: Any) -> None:  log_event(event, "WARNING", **fields)
def error(event: str, **fields: Any) -> None: log_event(event, "ERROR", **fields)
def debug(event: str, **fields: Any) -> None: log_event(event, "DEBUG", **fields)


# ── secrets policy enforcement ─────────────────────────────────────


def safe_token_summary(token: str | None) -> str:
    """Render a token for logs without leaking it. e.g. 'eyJ…abc (912ch)'."""
    if not token:
        return "none"
    if len(token) < 12:
        return f"…({len(token)}ch)"
    return f"{token[:6]}…{token[-3:]} ({len(token)}ch)"
