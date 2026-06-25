"""Skill/plugin-declared env var passthrough registry.

Companion to :mod:`flowly.exec.env_scrub`. Default behaviour is
*strip-by-default* for the Flowly-managed credential blocklist. This
module provides the **opt-in passthrough** mechanism for the rare cases
where a skill or plugin legitimately needs the agent to forward a
normally-stripped variable to a child process.

Two passthrough sources:

1. **Skill / plugin declarations** — when a skill is loaded that
   declares ``required_environment_variables`` in its frontmatter, those
   names get added to a context-scoped allowlist.

2. **User config** — ``tools.exec.env_passthrough`` in
   ``~/.flowly/config.json`` is a static operator-managed allowlist.

The two are unioned. Both are consulted from
``sanitize_subprocess_env`` before stripping a variable.

**CVE-grade guard**: ``register_env_passthrough`` refuses to register
anything in the Flowly provider blocklist. Without this, a malicious
plugin's manifest could declare ``OPENAI_API_KEY`` as required and
defeat the scrub. See GHSA-rhgp-j443-p4rf for the upstream precedent.

ContextVar backing is intentional: a multi-channel gateway (telegram +
web + desktop, all sharing the same process) must not bleed one
session's allowlist into another's subprocess spawns.
"""

from __future__ import annotations

import logging
from contextvars import ContextVar
from typing import Iterable

logger = logging.getLogger(__name__)


# Per-context allowlist. The gateway pipeline pushes a fresh
# ContextVar value per inbound, so cross-session bleed is impossible.
_allowed_env_vars_var: ContextVar[set[str]] = ContextVar("_flowly_allowed_env_vars")


def _get_allowed() -> set[str]:
    """Return the live allowlist for the current context, creating it lazily."""
    try:
        return _allowed_env_vars_var.get()
    except LookupError:
        val: set[str] = set()
        _allowed_env_vars_var.set(val)
        return val


# Static user-config allowlist. Loaded once per process; the operator
# restarts the agent if they edit config.exec.env_passthrough.
_config_passthrough: frozenset[str] | None = None


def register_env_passthrough(var_names: Iterable[str]) -> None:
    """Add ``var_names`` to the context-scoped passthrough allowlist.

    Called by the skill loader when a skill declares
    ``required_environment_variables`` in its frontmatter, and by any
    plugin manifest field that wires through to passthrough.

    **Provider credentials are not registerable.** The check exists
    specifically so a malicious skill manifest can't pull
    ``OPENAI_API_KEY`` (etc.) through the scrub — see
    GHSA-rhgp-j443-p4rf for the original upstream advisory. Skills
    that need to talk to a Flowly-managed provider should do so via
    the agent's in-
    process tools (the LLM call infrastructure already has the
    credential safely), not by forwarding env to a subprocess.
    """
    # Late import to avoid circular dep with env_scrub.
    from flowly.exec.env_scrub import is_flowly_credential

    for raw in var_names:
        name = (raw or "").strip()
        if not name:
            continue
        if is_flowly_credential(name):
            logger.warning(
                "env passthrough: refusing to register Flowly-managed credential "
                "%r — skills must not bypass the subprocess scrub (see "
                "GHSA-rhgp-j443-p4rf for the precedent).",
                name,
            )
            continue
        _get_allowed().add(name)
        logger.debug("env passthrough: registered %s", name)


def _load_config_passthrough() -> frozenset[str]:
    """Read ``tools.exec.env_passthrough`` from config (cached)."""
    global _config_passthrough
    if _config_passthrough is not None:
        return _config_passthrough

    result: set[str] = set()
    try:
        from flowly.config.loader import load_config
        cfg = load_config()
        exec_cfg = getattr(cfg.tools, "exec", None) if hasattr(cfg, "tools") else None
        passthrough = getattr(exec_cfg, "env_passthrough", None) if exec_cfg else None
        if isinstance(passthrough, list):
            for item in passthrough:
                if isinstance(item, str) and item.strip():
                    result.add(item.strip())
    except Exception as exc:
        logger.debug("could not read tools.exec.env_passthrough from config: %s", exc)

    _config_passthrough = frozenset(result)
    return _config_passthrough


def is_env_passthrough(var_name: str) -> bool:
    """True if *var_name* is allowlisted by a skill or by user config."""
    if var_name in _get_allowed():
        return True
    return var_name in _load_config_passthrough()


def clear_env_passthrough() -> None:
    """Reset the context-scoped allowlist (e.g. on session reset)."""
    _get_allowed().clear()


def get_all_passthrough() -> frozenset[str]:
    """Diagnostic / test helper — the union of both sources."""
    return frozenset(_get_allowed()) | _load_config_passthrough()
