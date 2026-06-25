"""Load ``$FLOWLY_HOME/.env`` so MCP config can reference user secrets.

Flowly's main config file is camelCase JSON, which is awkward for
storing raw credentials (and is mode 0600 but still readable to the
agent's full plugin set). The MCP integration adopts the dotenv
convention used by most CLI tools: keep secrets in
``$FLOWLY_HOME/.env``, reference them from ``config.json`` via
``${VAR}`` placeholders (see :func:`flowly.mcp.security.interpolate_env_vars`).

This loader is intentionally minimal — we don't need quote handling,
multi-line values, or interpolation between vars. Each line:

    KEY=value          # populates os.environ[KEY] = "value"
    # comment          # ignored
    KEY="quoted value" # quotes stripped
    KEY=               # empty value preserved
    KEY                # malformed, ignored with debug log

Existing ``os.environ`` entries always win — so a value set in the
shell takes precedence over the file. This matches dotenv tradition
and means tests can override values without touching the file.
"""

from __future__ import annotations

import logging
import os


logger = logging.getLogger(__name__)


_loaded_paths: set[str] = set()


def load_flowly_dotenv(force: bool = False) -> int:
    """Read ``$FLOWLY_HOME/.env`` and populate ``os.environ``.

    Returns the number of new variables added (already-set ones are
    not counted). Idempotent: subsequent calls are no-ops unless
    ``force=True``.

    Missing file → 0 added, no error.
    """
    from flowly.profile import get_flowly_home

    path = get_flowly_home() / ".env"
    key = str(path.resolve())
    if not force and key in _loaded_paths:
        return 0
    _loaded_paths.add(key)

    if not path.exists():
        return 0

    added = 0
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("MCP .env read failed at %s: %s", path, exc)
        return 0

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            logger.debug("MCP .env: ignoring malformed line %r", line)
            continue
        name, _, value = line.partition("=")
        name = name.strip()
        value = value.strip()
        if not name:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        if name in os.environ:
            continue
        os.environ[name] = value
        added += 1

    if added:
        logger.debug("MCP .env: loaded %d new env var(s) from %s", added, path)
    return added


def save_env_value(name: str, value: str) -> None:
    """Set ``name=value`` in ``$FLOWLY_HOME/.env`` (create/update, mode 0600).

    Used by ``flowly mcp install`` to persist a catalog server's secrets
    out of config.json. Updates an existing key in place; appends otherwise.
    Also updates ``os.environ`` so the value is usable immediately (e.g. a
    probe right after install).
    """
    import secrets as _secrets

    from flowly.profile import get_flowly_home

    home = get_flowly_home()
    home.mkdir(parents=True, exist_ok=True)
    path = home / ".env"

    lines: list[str] = []
    if path.exists():
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            lines = []

    rendered = f"{name}={value}"
    replaced = False
    for i, raw in enumerate(lines):
        stripped = raw.strip()
        if stripped.startswith("#") or "=" not in stripped:
            continue
        key = stripped.split("=", 1)[0].strip()
        if key == name:
            lines[i] = rendered
            replaced = True
            break
    if not replaced:
        lines.append(rendered)

    tmp = path.with_suffix(f".tmp.{_secrets.token_hex(4)}")
    try:
        tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
        os.replace(str(tmp), str(path))
        from flowly.utils.file_security import secure_file
        secure_file(path)  # POSIX chmod; real owner-only ACL on Windows
    except OSError as exc:
        logger.warning("MCP .env write failed for %s: %s", name, exc)
        tmp.unlink(missing_ok=True)
        return
    os.environ[name] = value
