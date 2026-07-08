"""Register Flowly's tool-callback MCP server in ``~/.codex/config.toml``.

When the ``codex_session`` runtime is enabled with
``tools.codex_session.expose_flowly_tools = True``, the Codex subprocess
needs to know how to spawn Flowly's tool-callback MCP server
(:mod:`flowly.codex.tools_mcp_server`) so a Codex turn can reach back
into Flowly's web/skills tools.

Codex reads MCP servers from ``[mcp_servers.<name>]`` tables in
``~/.codex/config.toml``. This module writes a single
``[mcp_servers.flowly-tools]`` entry inside a *managed block* delimited
by marker comments, idempotently:

  * Re-running replaces the managed block in place.
  * Everything OUTSIDE the markers (the user's own Codex config — model,
    other MCP servers, permission profiles) is preserved verbatim.
  * The managed block is inserted BEFORE the first table header so its
    root-level keys (if any) stay root-scoped (TOML has no syntax to
    return to document root after a table header).

Writes are atomic (temp file + rename) so a crash mid-write never
leaves Codex a half-written config it would refuse to load.
"""

from __future__ import annotations

import logging
import os
import sys
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

_MARKER = "# managed by flowly — regenerated when the codex_session runtime is enabled"
_END_MARKER = "# end flowly managed section"

_MCP_SERVER_NAME = "flowly-tools"


def _toml_str(value: str) -> str:
    """Format a Python string as a TOML basic string."""
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\b", "\\b")
        .replace("\t", "\\t")
        .replace("\n", "\\n")
        .replace("\f", "\\f")
        .replace("\r", "\\r")
    )
    return f'"{escaped}"'


def _toml_inline_env(env: dict[str, str]) -> str:
    items = ", ".join(f"{k} = {_toml_str(v)}" for k, v in env.items())
    return "{ " + items + " }" if items else "{}"


def _toml_value(value) -> str:
    """Format a Python value as a TOML scalar / inline-table / array."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        return _toml_str(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    if isinstance(value, dict):
        items = ", ".join(f"{_quote_key(k)} = {_toml_value(v)}" for k, v in value.items())
        return "{ " + items + " }" if items else "{}"
    raise ValueError(f"unsupported TOML value type: {type(value).__name__}")


def _quote_key(key: str) -> str:
    """Bare key when it's a valid TOML bare key, otherwise quoted."""
    if key and all(c.isalnum() or c in "-_" for c in key):
        return key
    return _toml_str(key)


# Flowly MCPServerConfig keys with no codex equivalent (dropped, warned).
_MCP_DROPPED_KEYS = (
    "ssl_verify", "client_cert", "client_key", "transport", "auth", "scope",
    "supports_parallel_tool_calls", "reap_orphans", "osv_check", "sampling",
    "tools",
)


def _translate_mcp_server(name: str, cfg) -> tuple[dict | None, list[str]]:
    """Translate one Flowly MCP server into a codex inline-table dict.

    Accepts an ``MCPServerConfig`` (pydantic) or a plain dict. Returns
    ``(codex_entry, skipped_keys)``; ``codex_entry`` is None when the
    server has neither a command nor a url (untranslatable).
    """
    def g(attr, default=None):
        if isinstance(cfg, dict):
            return cfg.get(attr, default)
        return getattr(cfg, attr, default)

    skipped: list[str] = []
    out: dict = {}
    command = (g("command") or "").strip()
    url = (g("url") or "").strip()

    if command:
        out["command"] = command
        args = g("args") or []
        if args:
            out["args"] = [str(a) for a in args]
        env = g("env") or {}
        if env:
            out["env"] = {str(k): str(v) for k, v in env.items()}
        if url:
            skipped.append("url (both command and url set; preferring stdio)")
    elif url:
        out["url"] = url
        headers = g("headers") or {}
        if headers:
            out["http_headers"] = {str(k): str(v) for k, v in headers.items()}
    else:
        return None, ["no command or url"]

    # Timeouts → codex's *_sec knobs.
    timeout = g("timeout")
    if isinstance(timeout, (int, float)) and timeout:
        out["tool_timeout_sec"] = float(timeout)
    connect = g("connect_timeout")
    if isinstance(connect, (int, float)) and connect:
        out["startup_timeout_sec"] = float(connect)

    # Codex defaults enabled=true; only emit when explicitly disabled.
    if g("enabled") is False:
        out["enabled"] = False

    # Note keys we drop (only when the user actually set a non-empty value).
    for key in _MCP_DROPPED_KEYS:
        val = g(key)
        if val not in (None, "", [], {}, False, True, 120.0, 60.0):
            skipped.append(f"{key} (no codex equivalent)")

    return out, skipped


async def _query_codex_plugins_async(codex_home: str | None) -> tuple[list[dict], str | None]:
    """Query codex's ``plugin/list`` for installed curated plugins.

    Spawns a short-lived ``codex app-server``, runs initialize + plugin/list,
    and returns ``([{name, marketplace, enabled}], error)``. Best-effort:
    any failure (codex missing, RPC error, timeout) returns ``([], error)``.
    """
    try:
        from flowly.codex.app_server import CodexAppServerClient
    except Exception as exc:  # pragma: no cover
        return [], f"transport unavailable: {exc}"

    client = None
    try:
        client = await CodexAppServerClient.spawn(
            codex_home=codex_home, client_name="flowly-migration",
        )
        resp = await client.request("plugin/list", {}, timeout=8.0)
    except Exception as exc:
        return [], f"plugin/list failed: {exc}"
    finally:
        if client is not None:
            try:
                await client.close()
            except Exception:
                pass

    out: list[dict] = []
    seen: set[tuple[str, str]] = set()
    marketplaces = resp.get("marketplaces") if isinstance(resp, dict) else None
    if not isinstance(marketplaces, list):
        return [], "plugin/list response missing 'marketplaces'"
    for market in marketplaces:
        if not isinstance(market, dict):
            continue
        market_name = str(market.get("name") or "openai-curated")
        for plugin in market.get("plugins") or []:
            if not isinstance(plugin, dict) or not plugin.get("installed"):
                continue
            availability = str(plugin.get("availability") or "").upper()
            if availability and availability != "AVAILABLE":
                continue
            pname = str(plugin.get("name") or "")
            if not pname:
                continue
            key = (pname, market_name)
            if key in seen:
                continue
            seen.add(key)
            out.append({
                "name": pname,
                "marketplace": market_name,
                "enabled": bool(plugin.get("enabled", True)),
            })
    return out, None


def _discover_codex_plugins(codex_home: str | None) -> tuple[list[dict], str | None]:
    """Sync wrapper around the async plugin query. Best-effort.

    Skips cleanly when called from inside a running event loop (e.g. the
    gateway boot path) so it never blocks or raises — plugin discovery then
    happens on the next explicit ``flowly codex enable``.
    """
    import asyncio
    try:
        asyncio.get_running_loop()
        return [], "skipped (running event loop)"
    except RuntimeError:
        pass  # no running loop — safe to drive our own
    try:
        return asyncio.run(_query_codex_plugins_async(codex_home))
    except Exception as exc:  # pragma: no cover - defensive
        return [], f"discovery error: {exc}"


def render_managed_block(
    *,
    python_bin: str,
    env: dict[str, str],
    servers: dict[str, dict] | None = None,
    plugins: list[dict] | None = None,
    default_permissions: str | None = None,
    ask_for_approval: str | None = None,
    include_callback: bool = True,
) -> str:
    """Render the managed codex config block.

    Root-level policy keys (emitted before any table so they stay
    document-root scoped) when provided:

      * ``default_permissions`` — codex sandbox profile
      * ``ask_for_approval`` — codex approval policy

    With ``include_callback`` (the default), also writes the
    ``[mcp_servers.flowly-tools]`` callback entry plus, when provided, the
    user's translated ``[mcp_servers.<name>]`` servers and any installed
    ``[plugins."<name>@<marketplace>"]``. With ``include_callback=False`` the
    block is policy-only — sandbox/approval get written even when the runtime
    is kept fully isolated (``expose_flowly_tools=False``).
    """
    lines = [_MARKER, ""]

    # Root-level keys first so they stay document-root scoped (they precede all
    # table headers within the block, and the whole block is inserted before
    # the user's first table).
    if default_permissions:
        norm = (
            default_permissions if default_permissions.startswith(":")
            else f":{default_permissions}"
        )
        lines.append(f"default_permissions = {_toml_str(norm)}")
        lines.append("")
    if ask_for_approval:
        lines.append(f"ask_for_approval = {_toml_str(ask_for_approval)}")
        lines.append("")

    if include_callback:
        # The flowly-tools callback.
        lines.append(f"[mcp_servers.{_MCP_SERVER_NAME}]")
        lines.append(f"command = {_toml_str(python_bin)}")
        lines.append('args = ["-m", "flowly.codex.tools_mcp_server"]')
        if env:
            lines.append(f"env = {_toml_inline_env(env)}")
        lines.append("startup_timeout_sec = 30.0")
        lines.append("tool_timeout_sec = 600.0")

        # The user's own flowly MCP servers, translated.
        for name in sorted(servers or {}):
            cfg = servers[name]
            lines.append("")
            lines.append(f"[mcp_servers.{_quote_key(name)}]")
            for k, v in cfg.items():
                lines.append(f"{_quote_key(k)} = {_toml_value(v)}")

        # Installed codex plugins.
        for plugin in sorted(
            plugins or [],
            key=lambda p: f"{p.get('name', '')}@{p.get('marketplace', '')}",
        ):
            qualified = f"{plugin.get('name', '')}@{plugin.get('marketplace', 'openai-curated')}"
            lines.append("")
            lines.append(f"[plugins.{_quote_key(qualified)}]")
            lines.append(f"enabled = {_toml_value(bool(plugin.get('enabled', True)))}")

    # Exactly one blank line before the end marker, whichever sections ran.
    while lines and lines[-1] == "":
        lines.pop()
    lines.append("")
    lines.append(_END_MARKER)
    return "\n".join(lines) + "\n"


def _strip_existing_managed_block(text: str) -> str:
    """Remove any prior managed section so re-runs replace it idempotently."""
    out: list[str] = []
    in_managed = False
    for line in text.splitlines(keepends=True):
        stripped = line.rstrip("\n")
        if stripped == _MARKER:
            in_managed = True
            continue
        if in_managed:
            if stripped == _END_MARKER:
                in_managed = False
            continue
        out.append(line)
    return "".join(out)


def _insert_block_before_first_table(user_text: str, block: str) -> str:
    """Insert the managed block before the first TOML table header.

    Keeps the managed block's content table-scoped correctly and the
    user's content verbatim. When the user file has no table header, the
    block is appended.
    """
    if not user_text.strip():
        return block
    lines = user_text.splitlines(keepends=True)
    first_table_idx: int | None = None
    for idx, line in enumerate(lines):
        if line.lstrip().startswith("["):
            first_table_idx = idx
            break
    if first_table_idx is None:
        prefix = user_text.rstrip("\n")
        return f"{prefix}\n\n{block}" if prefix else block
    prefix = "".join(lines[:first_table_idx]).rstrip("\n")
    suffix = "".join(lines[first_table_idx:]).lstrip("\n")
    if prefix:
        return f"{prefix}\n\n{block}\n{suffix}"
    return f"{block}\n{suffix}"


def _flowly_package_root() -> str | None:
    """Return the directory that should be on PYTHONPATH so an importer
    resolves the SAME ``flowly`` package that's running this migration.

    Codex spawns the MCP callback with its own cwd and (often) no
    PYTHONPATH. If Flowly is running from a git worktree whose venv
    editable-install still points at another checkout (a common worktree
    footgun), the subprocess would import the wrong ``flowly`` — one that
    may not even contain ``flowly.codex``. Pinning the running package's
    parent dir on PYTHONPATH makes the callback deterministic regardless
    of cwd / editable target.
    """
    try:
        import flowly
        pkg_file = getattr(flowly, "__file__", None)
        if not pkg_file:
            return None
        # <root>/flowly/__init__.py → <root>
        return str(Path(pkg_file).resolve().parent.parent)
    except Exception:
        return None


def _callback_env() -> dict[str, str]:
    """Environment the Codex-spawned MCP subprocess needs.

    * ``PYTHONPATH`` pinned to the running Flowly's package root so the
      subprocess imports the same ``flowly`` (incl. ``flowly.codex``)
      regardless of cwd or a mismatched worktree editable-install.
      Any pre-existing PYTHONPATH is appended after it.
    * ``FLOWLY_HOME`` (if set) so the subprocess resolves the same
      config / workspace as the parent.
    * ``FLOWLY_QUIET`` so banners stay off the MCP stdout wire.
    """
    env: dict[str, str] = {"FLOWLY_QUIET": "1"}
    parts: list[str] = []
    root = _flowly_package_root()
    if root:
        parts.append(root)
    existing = os.environ.get("PYTHONPATH")
    if existing:
        parts.append(existing)
    if parts:
        env["PYTHONPATH"] = os.pathsep.join(parts)
    flowly_home = os.environ.get("FLOWLY_HOME")
    if flowly_home:
        env["FLOWLY_HOME"] = flowly_home
    return env


def _looks_like_table_header(stripped: str) -> bool:
    if not stripped.startswith("["):
        return False
    head = stripped.split("#", 1)[0].rstrip()
    if not head.endswith("]"):
        return False
    return "=" not in head[: head.index("]") + 1]


def _strip_unmanaged_plugin_tables(text: str) -> str:
    """Remove ``[plugins."x@y"]`` tables OUTSIDE the managed block.

    Codex itself writes these when the user runs ``codex plugins enable``.
    Once we discover plugins authoritatively via plugin/list and re-emit them
    inside the managed block, the pre-existing ones would collide (duplicate
    table headers → codex refuses to load). plugin/list is the source of
    truth, so dropping the unmanaged ones is safe. Only call this when the
    plugin query actually succeeded.
    """
    out: list[str] = []
    in_plugin = False
    for line in text.splitlines(keepends=True):
        stripped = line.lstrip()
        if _looks_like_table_header(stripped):
            in_plugin = stripped.startswith("[plugins.")
            if in_plugin:
                continue
        if in_plugin:
            continue
        out.append(line)
    return "".join(out)


def _sandbox_to_permission(sandbox: str | None) -> str:
    """Map a Flowly sandbox level to a codex built-in permission profile.

    Profile names verified against codex 0.131: ``:read-only``, ``:workspace``
    and ``:danger-full-access`` load; ``:workspace-write``, ``:full-access``
    and ``:danger-no-sandbox`` are NOT recognized and make codex refuse the
    whole config — so we only ever emit the valid three.
    """
    return {
        "read-only": ":read-only",
        "workspace-write": ":workspace",
        "full-access": ":danger-full-access",
    }.get((sandbox or "").strip(), ":workspace")


def _approval_to_codex(policy: str | None) -> str:
    """Map a Flowly ``codex_session`` approval policy to codex's
    ``ask_for_approval`` config value.

    Codex CLI accepts ``untrusted``, ``on-request`` and ``never``
    (``on-failure`` is deprecated). Flowly exposes ``on-request`` / ``never`` /
    ``auto-review`` / ``granular``; ``granular`` has no 1:1 codex equivalent, so
    it maps to the safest prompt-first policy. Unknown values fall back to
    ``on-request``.
    """
    return {
        "on-request": "on-request",
        "never": "never",
        "auto-review": "untrusted",
        "granular": "on-request",
    }.get((policy or "").strip(), "on-request")


def migrate_flowly_tools_to_codex(
    *,
    codex_home: str | None = None,
    python_bin: str | None = None,
    config=None,
    default_permissions: str | None = ":workspace",
    ask_for_approval: str | None = None,
    discover_plugins: bool = False,
    include_callback: bool = True,
) -> Path:
    """Write the managed codex config block to ``config.toml``.

    With ``include_callback`` (the default), registers the ``flowly-tools``
    callback and migrates the user's Flowly MCP servers (and, when
    ``discover_plugins``, installed codex plugins). Always writes the policy
    keys — ``default_permissions`` (sandbox) and ``ask_for_approval`` — when
    they are provided. With ``include_callback=False`` the write is policy-only:
    sandbox/approval land even when the runtime is kept fully isolated
    (``expose_flowly_tools=False``), while nothing is exposed to codex.

    Args:
        codex_home: override for ``$CODEX_HOME`` (defaults to env / ~/.codex).
        python_bin: python the callback is spawned with (defaults to current).
        config: a Flowly ``Config`` (loaded if None) — source of mcp_servers.
        default_permissions: codex sandbox profile (``:workspace`` etc.); None
            to skip. Map from a Flowly sandbox with ``_sandbox_to_permission``.
        ask_for_approval: codex approval policy (``on-request`` etc.); None to
            skip. Map from a Flowly policy with ``_approval_to_codex``.
        discover_plugins: query ``plugin/list`` and migrate installed plugins.
            Off by default (boot path); the CLI enables it. Auto-skips inside
            a running event loop. Ignored when ``include_callback=False``.
        include_callback: expose Flowly's tool callback + MCP servers to codex.
            False writes only the sandbox/approval policy.

    Returns the path to the written ``config.toml``.
    """
    home = Path(
        codex_home
        or os.environ.get("CODEX_HOME")
        or (Path.home() / ".codex")
    )
    target = home / "config.toml"
    python_bin = python_bin or sys.executable or "python3"

    # Translate the user's flowly MCP servers + discover plugins ONLY when the
    # callback is being written; a policy-only pass exposes nothing to codex.
    servers: dict[str, dict] = {}
    plugins: list[dict] = []
    plugin_query_ok = False
    if include_callback:
        # Load config for the user's MCP servers (best-effort).
        if config is None:
            try:
                from flowly.config.loader import load_config
                config = load_config()
            except Exception:
                config = None

        raw_servers = getattr(config, "mcp_servers", None) or {}
        if isinstance(raw_servers, dict):
            for name, scfg in raw_servers.items():
                entry, skipped = _translate_mcp_server(str(name), scfg)
                if entry is None:
                    logger.debug("codex migration: skipping MCP server %s (%s)", name, skipped)
                    continue
                servers[str(name)] = entry
                if skipped:
                    logger.debug("codex migration: %s dropped keys: %s", name, skipped)

        # Discover installed codex plugins (best-effort, off on the boot path).
        if discover_plugins:
            plugins, perr = _discover_codex_plugins(str(home) if codex_home else None)
            if perr:
                logger.debug("codex plugin discovery: %s", perr)
            else:
                plugin_query_ok = True

    block = render_managed_block(
        python_bin=python_bin,
        env=_callback_env(),
        servers=servers,
        plugins=plugins,
        default_permissions=default_permissions,
        ask_for_approval=ask_for_approval,
        include_callback=include_callback,
    )

    if target.exists():
        existing = target.read_text(encoding="utf-8")
        without_managed = _strip_existing_managed_block(existing)
        # When plugin/list ran authoritatively, drop pre-existing [plugins.*]
        # tables so our re-emitted ones don't collide.
        if plugin_query_ok:
            without_managed = _strip_unmanaged_plugin_tables(without_managed)
        new_text = _insert_block_before_first_table(without_managed, block)
    else:
        new_text = block

    home.mkdir(parents=True, exist_ok=True)
    fd, tmp_str = tempfile.mkstemp(prefix=".config.toml.", dir=str(home))
    tmp = Path(tmp_str)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(new_text)
        tmp.replace(target)
    except Exception:
        try:
            if tmp.exists():
                tmp.unlink()
        except Exception:
            pass
        raise
    if include_callback:
        logger.info("registered flowly-tools MCP callback in %s", target)
    else:
        logger.info("wrote codex sandbox/approval policy (policy-only) in %s", target)
    return target


__all__ = [
    "migrate_flowly_tools_to_codex",
    "render_managed_block",
]
