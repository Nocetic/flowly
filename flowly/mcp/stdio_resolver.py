"""Resolve stdio command paths for MCP subprocesses.

In a normal shell, bare ``npx``, ``npm``, ``node`` etc. resolve via
``$PATH``. Flowly ships as a Nuitka-bundled binary where the launcher
sanitizes ``PATH`` to a known-safe subset — which often excludes the
user's Homebrew or nvm install. The result: Node-based MCP servers
fail with ``ENOENT`` before they ever try to start.

This module:

1. Resolves the command against ``shutil.which`` using the subprocess
   env's ``PATH`` (not the parent's).
2. For ``npx``/``npm``/``node`` specifically, falls back to a small
   list of candidate install locations covering Flowly's bundled
   Node, the user's ``~/.local/bin``, and ``/usr/local/bin`` (Homebrew
   on Intel + Linux from-source builds).
3. Prepends the resolved command's directory to the subprocess
   ``PATH`` so transitive launches (npx → /usr/bin/env node) succeed.
"""

from __future__ import annotations

import logging
import os
import shutil


logger = logging.getLogger(__name__)


_NODE_TOOLS = {"npx", "npm", "node"}


def _flowly_home() -> str:
    """Resolve ``$FLOWLY_HOME``, fall back to ``~/.flowly``.

    Imported lazily so this module stays import-cheap even if
    ``flowly.profile`` hits a slow path.
    """
    home = os.environ.get("FLOWLY_HOME")
    if home:
        return os.path.expanduser(home)
    return os.path.join(os.path.expanduser("~"), ".flowly")


def resolve_stdio_command(command: str, env: dict[str, str]) -> tuple[str, dict[str, str]]:
    """Return ``(resolved_command, updated_env)`` for an MCP stdio command.

    *command* may be a bare name (``npx``), an absolute path, or a path
    with ``~``. *env* is the subprocess environment built by
    :func:`flowly.mcp.security.build_safe_env`; its ``PATH`` drives
    resolution.

    If resolution fails we return the input unchanged — the subsequent
    ``spawn`` call will surface a clear ``FileNotFoundError``.
    """
    cmd = os.path.expanduser(str(command).strip())
    new_env = dict(env or {})

    if os.sep in cmd:
        # Already qualified — just make sure the directory is on PATH.
        return cmd, _prepend_path(new_env, os.path.dirname(cmd))

    path_arg = new_env.get("PATH")
    which_hit = shutil.which(cmd, path=path_arg)
    if which_hit:
        return which_hit, _prepend_path(new_env, os.path.dirname(which_hit))

    if cmd in _NODE_TOOLS:
        for candidate in _node_candidates(cmd):
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate, _prepend_path(new_env, os.path.dirname(candidate))

    logger.debug("MCP stdio command %r not resolvable; passing through", cmd)
    return cmd, new_env


def _node_candidates(cmd: str) -> list[str]:
    home = os.path.expanduser("~")
    return [
        os.path.join(_flowly_home(), "node", "bin", cmd),
        os.path.join(home, ".local", "bin", cmd),
        os.path.join(home, ".nvm", "versions", "node", "current", "bin", cmd),
        os.path.join(os.sep, "usr", "local", "bin", cmd),
        os.path.join(os.sep, "opt", "homebrew", "bin", cmd),
    ]


def _prepend_path(env: dict[str, str], directory: str) -> dict[str, str]:
    if not directory:
        return env
    out = dict(env)
    existing = out.get("PATH", "")
    parts = [p for p in existing.split(os.pathsep) if p]
    if directory not in parts:
        parts = [directory, *parts]
    out["PATH"] = os.pathsep.join(parts) if parts else directory
    return out
