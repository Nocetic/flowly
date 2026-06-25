"""Codex App Server integration.

Lets Flowly delegate coding-heavy tasks to OpenAI's ``codex app-server``
subprocess via JSON-RPC 2.0 over stdio. The integration is split into:

  * :mod:`flowly.codex.app_server` — wire-level transport: subprocess
    spawn, initialize handshake, request/response futures, notification
    + server-request queues, graceful close.
  * :mod:`flowly.codex.projector` — translates Codex's
    ``item`` event stream into Flowly's flat message format
    (``role: assistant``, ``role: tool``).
  * :mod:`flowly.codex.session` — per-Flowly-session Codex thread
    lifecycle: spawn, resume, archive, wedge detection, OAuth refresh
    classification.

Flowly never replaces its own agent loop with the Codex daemon — Codex
is exposed as a single ``codex_session`` tool that the main agent
calls when the user explicitly asks for code-heavy work. See
``docs/codex-integration.md`` for the design rationale.
"""

from flowly.codex.app_server import (
    CodexAppServerClient,
    CodexRPCError,
    CodexSpawnError,
)

__all__ = [
    "CodexAppServerClient",
    "CodexRPCError",
    "CodexSpawnError",
]
