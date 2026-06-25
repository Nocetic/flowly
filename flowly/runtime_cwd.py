"""Runtime working-directory resolution.

Flowly keeps two distinct notions of "where things live":

* **workspace** — ``config.workspace_path`` (``~/.flowly/workspace`` by
  default). This is Flowly's *internal* root: memory, skills, state,
  sandbox layout. It never moves.
* **runtime cwd** — the directory where shell commands (``exec``) and
  delegated coding subprocesses (``codex_session``) actually run. This
  is the user's *project* directory and may differ from the workspace.

Historically the two were conflated: ``exec`` always ran in the
workspace and long-lived subprocesses inherited the Flowly *process*
cwd — which, under a service install, is whatever directory the service
happened to be installed from. This module makes the runtime cwd an
explicit, deterministic value.

Resolution priority (first match wins)::

    explicit override (tool ``working_dir`` / request cwd)
    > session cwd (set per ``session_key`` by the gateway)
    > FLOWLY_CWD environment variable
    > agents.defaults.cwd config
    > workspace

No global ``os.chdir`` and no process-env mutation: the per-session cwd
lives in an in-process registry keyed by ``session_key`` so concurrent
gateway sessions never clobber one another.
"""

from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from loguru import logger

__all__ = [
    "FLOWLY_CWD_ENV",
    "validate_cwd",
    "resolve_runtime_cwd",
    "set_session_cwd",
    "get_session_cwd",
    "clear_session_cwd",
    "runtime_cwd_context",
]

FLOWLY_CWD_ENV = "FLOWLY_CWD"

# session_key -> validated absolute directory (str). Guarded by a lock
# because the gateway may touch it from background tasks / threads while
# an agent turn reads it.
_session_cwd: dict[str, str] = {}
_lock = threading.Lock()


def validate_cwd(
    value: str | os.PathLike[str] | None, *, require_absolute: bool = False
) -> Path | None:
    """Validate a candidate working directory.

    Returns the expanded, resolved :class:`Path` when *value* is a
    non-empty string that points at an existing directory; otherwise
    ``None``. When *require_absolute* is set, a path that is still
    relative after ``~`` expansion is rejected.

    Never raises — callers treat ``None`` as "not usable, fall through to
    the next source in the priority chain".
    """
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    try:
        path = Path(raw).expanduser()
    except (RuntimeError, ValueError):
        return None
    if require_absolute and not path.is_absolute():
        return None
    try:
        if not path.is_dir():
            return None
        return path.resolve()
    except OSError:
        return None


def _config_default_cwd(config: Any) -> str | None:
    """Pull ``agents.defaults.cwd`` off a Config without hard-coupling."""
    if config is None:
        return None
    try:
        return config.agents.defaults.cwd or None
    except AttributeError:
        return None


def _workspace_fallback(
    workspace: str | os.PathLike[str] | None, config: Any
) -> Path:
    """Terminal fallback: the explicit *workspace*, else config's, else home."""
    if workspace:
        try:
            return Path(workspace).expanduser()
        except (RuntimeError, ValueError):
            pass
    if config is not None:
        try:
            return config.workspace_path
        except AttributeError:
            pass
    return Path.home()


def resolve_runtime_cwd(
    *,
    session_key: str | None = None,
    explicit: str | os.PathLike[str] | None = None,
    config: Any = None,
    workspace: str | os.PathLike[str] | None = None,
) -> Path:
    """Resolve the effective runtime cwd following the priority chain.

    Always returns a usable directory; the terminal fallback is the
    workspace. See the module docstring for the full priority order.
    """
    # 1. Explicit override — the caller named this exact directory.
    #    Honour it verbatim (only ``~``-expanded) even if it does not
    #    exist yet, so the *caller* owns any "no such directory" error
    #    rather than us silently redirecting to the workspace.
    if explicit and str(explicit).strip():
        return Path(str(explicit).strip()).expanduser()

    # 2. Per-session cwd, set by the gateway from a validated chat.send.
    if session_key:
        with _lock:
            sess = _session_cwd.get(session_key)
        validated = validate_cwd(sess)
        if validated is not None:
            return validated

    # 3. FLOWLY_CWD env (service / desktop / manually launched gateway).
    env_cwd = validate_cwd(os.environ.get(FLOWLY_CWD_ENV))
    if env_cwd is not None:
        return env_cwd

    # 4. agents.defaults.cwd config.
    cfg_raw = _config_default_cwd(config)
    cfg_cwd = validate_cwd(cfg_raw)
    if cfg_cwd is not None:
        return cfg_cwd
    if cfg_raw:
        logger.warning(
            "agents.defaults.cwd={!r} is not an existing directory; "
            "falling back to workspace",
            cfg_raw,
        )

    # 5. Workspace fallback.
    return _workspace_fallback(workspace, config)


def set_session_cwd(session_key: str, cwd: str | os.PathLike[str]) -> Path:
    """Pin a runtime cwd for *session_key*.

    Validates that *cwd* is an existing **absolute** directory and stores
    the resolved path. Raises :class:`ValueError` otherwise — the gateway
    maps that to an ``INVALID_CWD`` RPC error.
    """
    validated = validate_cwd(cwd, require_absolute=True)
    if validated is None:
        raise ValueError(f"not an existing absolute directory: {cwd!r}")
    with _lock:
        _session_cwd[session_key] = str(validated)
    return validated


def get_session_cwd(session_key: str) -> Path | None:
    """Return the pinned cwd for *session_key*, or ``None`` if unset/stale."""
    with _lock:
        val = _session_cwd.get(session_key)
    return validate_cwd(val)


def clear_session_cwd(session_key: str) -> None:
    """Drop any pinned cwd for *session_key* (no-op if none)."""
    with _lock:
        _session_cwd.pop(session_key, None)


@contextmanager
def runtime_cwd_context(
    session_key: str, cwd: str | os.PathLike[str] | None
) -> Iterator[Path | None]:
    """Temporarily pin *cwd* for *session_key* (e.g. a single cron run).

    Restores the previous session pin (or clears it) on exit. Yields the
    validated path, or ``None`` when *cwd* is empty/invalid — in which
    case no pin is applied and resolution falls through normally.
    """
    if not cwd or not str(cwd).strip():
        yield None
        return
    validated = validate_cwd(cwd, require_absolute=True)
    if validated is None:
        logger.warning("runtime_cwd_context: ignoring invalid cwd {!r}", cwd)
        yield None
        return
    with _lock:
        prev = _session_cwd.get(session_key)
        _session_cwd[session_key] = str(validated)
    try:
        yield validated
    finally:
        with _lock:
            if prev is None:
                _session_cwd.pop(session_key, None)
            else:
                _session_cwd[session_key] = prev
