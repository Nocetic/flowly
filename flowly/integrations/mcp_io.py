"""CLI/TUI-side MCP server management — list, toggle, remove, install.

Backs the TUI ``/mcp`` modal the same way :mod:`flowly.integrations.plugins_io`
backs ``/plugins``. Single source of truth: ``~/.flowly/config.json``
``mcpServers`` — the same map the agent reads at boot via ``Config.mcp_servers``.

We mutate the raw on-disk JSON (camelCase) directly through
:mod:`flowly.integrations.config_io` so server names and env/header map keys
survive verbatim (they are free-form, case-sensitive). New entries are
rendered with ``convert_to_camel`` (which preserves those keys) so they
match exactly what ``save_config`` would write.

A unified list shows two groups:
- **configured** servers (``enabled`` / ``disabled`` / ``invalid``)
- **catalog** entries not yet configured (``available``) — inline-installable
  when they need no secret, otherwise flagged for the CLI installer.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass
class MCPSecretField:
    """One secret/value the modal must collect before installing a catalog entry."""
    name: str
    prompt: str
    secret: bool
    default: str


@dataclass
class MCPServerEntry:
    """One row in the ``/mcp`` modal list."""
    name: str
    transport: str           # "stdio: npx …" | "http: https://…" | "invalid"
    enabled: bool
    auth: str                # "" | "oauth"
    tool_filter: str         # "all" | "N selected" | "-N excluded"
    source: Literal["configured", "catalog"]
    description: str
    status: Literal["enabled", "disabled", "available", "invalid"]
    needs_oauth: bool = False         # catalog entry that needs `mcp login` after install
    secret_fields: list[MCPSecretField] | None = None  # values to collect before install
    error: str | None = None
    # For configured OAuth servers: whether a cached token exists. None when not
    # applicable (non-oauth server, or a catalog row). A configured OAuth server
    # with ``authorized is False`` is enabled but not yet signed in — it will not
    # connect until the user completes ``mcp login``. This is what lets the UI
    # avoid showing "enabled" (config flag) as if it meant "connected".
    authorized: bool | None = None

    @property
    def needs_secrets(self) -> bool:
        return bool(self.secret_fields)


def _transport_summary_from_cfg(cfg: dict) -> str:
    url = cfg.get("url") or ""
    command = cfg.get("command") or ""
    if url:
        short = url if len(url) <= 40 else url[:37] + "…"
        return f"http: {short}"
    if command:
        args = " ".join(str(a) for a in (cfg.get("args") or [])[:3])
        return f"stdio: {command} {args}".strip()
    return "invalid"


def _tool_filter_summary(tools: dict) -> str:
    include = tools.get("include") or []
    exclude = tools.get("exclude") or []
    if include:
        return f"{len(include)} selected"
    if exclude:
        return f"-{len(exclude)} excluded"
    return "all"


def list_mcp_servers() -> list[MCPServerEntry]:
    """Return configured servers + not-yet-installed catalog entries.

    Configured first (sorted by name), then catalog "available" rows for
    entries whose name isn't already configured.
    """
    from flowly.integrations.config_io import _load_raw

    try:
        raw = _load_raw()
    except Exception:
        raw = {}
    servers = raw.get("mcpServers") or {}
    if not isinstance(servers, dict):
        servers = {}

    out: list[MCPServerEntry] = []
    for name in sorted(servers):
        cfg = servers[name] if isinstance(servers[name], dict) else {}
        has_transport = bool(cfg.get("url") or cfg.get("command"))
        enabled = bool(cfg.get("enabled", True))
        if not has_transport:
            status: str = "invalid"
        elif enabled:
            status = "enabled"
        else:
            status = "disabled"
        auth = str(cfg.get("auth") or "")
        authorized: bool | None = None
        if auth == "oauth":
            try:
                from flowly.mcp.oauth import has_tokens
                authorized = has_tokens(name)
            except Exception:
                authorized = None
        out.append(MCPServerEntry(
            name=name,
            transport=_transport_summary_from_cfg(cfg),
            enabled=enabled,
            auth=auth,
            tool_filter=_tool_filter_summary(cfg.get("tools") or {}),
            source="configured",
            description="",
            status=status,  # type: ignore[arg-type]
            needs_oauth=False,
            secret_fields=None,
            error=None if has_transport else "no command or url",
            authorized=authorized,
        ))

    # Catalog rows for entries not already configured.
    try:
        from flowly.mcp.catalog import load_catalog
        catalog = load_catalog()
    except Exception:
        catalog = {}
    for cname, entry in catalog.items():
        if cname in servers:
            continue
        fields = [
            MCPSecretField(name=e.name, prompt=e.prompt, secret=e.secret, default=e.default)
            for e in entry.env
        ]
        out.append(MCPServerEntry(
            name=cname,
            transport=entry.transport_summary(),
            enabled=False,
            auth=entry.auth_type if entry.auth_type == "oauth" else "",
            tool_filter="all",
            source="catalog",
            description=entry.description,
            status="available",
            needs_oauth=(entry.auth_type == "oauth"),
            secret_fields=fields or None,
            error=None,
        ))
    return out


def set_mcp_enabled(name: str, enabled: bool) -> None:
    """Flip a configured server's ``enabled`` flag (atomic, verbatim keys)."""
    from flowly.integrations.config_io import _load_raw, _atomic_write_json
    from flowly.config.loader import get_config_path

    raw = _load_raw()
    servers = raw.get("mcpServers")
    if not isinstance(servers, dict) or name not in servers:
        raise KeyError(name)
    if not isinstance(servers[name], dict):
        servers[name] = {}
    servers[name]["enabled"] = bool(enabled)
    _atomic_write_json(get_config_path(), raw)


def remove_mcp_server(name: str) -> bool:
    """Delete a configured server + its cached OAuth tokens. Returns removed?"""
    from flowly.integrations.config_io import _load_raw, _atomic_write_json
    from flowly.config.loader import get_config_path

    raw = _load_raw()
    servers = raw.get("mcpServers")
    if not isinstance(servers, dict) or name not in servers:
        return False
    del servers[name]
    if not servers:
        raw.pop("mcpServers", None)
    _atomic_write_json(get_config_path(), raw)
    try:
        from flowly.mcp.oauth import clear_tokens
        clear_tokens(name)
    except Exception:
        pass
    return True


def install_catalog_server(
    name: str, env_values: dict[str, str] | None = None,
) -> tuple[bool, str]:
    """Install a catalog entry into config. Returns ``(ok, message)``.

    ``env_values`` carries any secrets/values the caller collected for the
    entry's declared env vars; they are written to ``$FLOWLY_HOME/.env``
    (so config.json only holds ``${VAR}`` references) before the server
    config is written. OAuth entries install fine here (no secret) — the
    browser authorization happens later via ``flowly mcp login``.
    """
    from flowly.integrations.config_io import _load_raw, _atomic_write_json
    from flowly.config.loader import get_config_path, convert_to_camel
    from flowly.config.schema import MCPServerConfig
    from flowly.mcp.catalog import get_entry, build_server_config

    entry = get_entry(name)
    if entry is None:
        return False, f"unknown catalog entry {name!r}"

    # Persist collected secrets/values to $FLOWLY_HOME/.env first.
    if env_values:
        from flowly.mcp.env_loader import save_env_value
        declared = {e.name for e in entry.env}
        for key, value in env_values.items():
            if key in declared and value != "":
                save_env_value(key, value)

    cfg_obj = MCPServerConfig(**build_server_config(entry))
    rendered = convert_to_camel(cfg_obj.model_dump())

    raw = _load_raw()
    raw.setdefault("mcpServers", {})[name] = rendered
    _atomic_write_json(get_config_path(), raw)

    if entry.auth_type == "oauth":
        return True, f"installed {name} — authorize with: flowly mcp login {name}"
    return True, f"installed {name}"


def catalog_secret_fields(name: str) -> list[MCPSecretField]:
    """Return the env values the modal must collect before installing *name*."""
    from flowly.mcp.catalog import get_entry
    entry = get_entry(name)
    if entry is None:
        return []
    return [
        MCPSecretField(name=e.name, prompt=e.prompt, secret=e.secret, default=e.default)
        for e in entry.env
    ]


def upsert_mcp_server(name: str, config: dict) -> tuple[bool, str]:
    """Add or replace a manually-configured server. Returns ``(ok, message)``.

    ``config`` is a camelCase server config (``command``/``args``/``env`` for
    stdio, ``url``/``headers`` for http/sse, plus ``transport``/``auth``/
    ``enabled``/``tools``). It is validated through :class:`MCPServerConfig`
    (defaults filled, unknown keys rejected) and written back as camelCase so it
    matches exactly what ``save_config`` would emit. Server names and env/header
    map keys survive verbatim.
    """
    from flowly.integrations.config_io import _load_raw, _atomic_write_json
    from flowly.config.loader import (
        get_config_path, convert_to_camel, convert_keys,
    )
    from flowly.config.schema import MCPServerConfig

    name = (name or "").strip()
    if not name:
        return False, "server name is required"
    if not isinstance(config, dict):
        return False, "config must be an object"

    snake = convert_keys(config)
    snake.pop("name", None)  # name is the map key, never a field
    try:
        cfg_obj = MCPServerConfig(**snake)
    except Exception as exc:  # pydantic validation
        return False, f"invalid server config: {exc}"
    if not (cfg_obj.command or cfg_obj.url):
        return False, "a stdio command or an http/sse url is required"

    rendered = convert_to_camel(cfg_obj.model_dump())

    raw = _load_raw()
    raw.setdefault("mcpServers", {})[name] = rendered
    _atomic_write_json(get_config_path(), raw)
    return True, f"saved {name}"


def _server_config_dump(name: str) -> dict | None:
    """Return a configured server's snake_case config dump, or ``None``."""
    from flowly.integrations.config_io import _load_raw
    from flowly.config.loader import convert_keys
    from flowly.config.schema import MCPServerConfig

    try:
        raw = _load_raw()
    except Exception:
        return None
    servers = raw.get("mcpServers") or {}
    if not isinstance(servers, dict) or name not in servers:
        return None
    cfg = servers[name] if isinstance(servers[name], dict) else {}
    try:
        return MCPServerConfig(**convert_keys(cfg)).model_dump()
    except Exception:
        return None


def probe_mcp_server(name: str, *, interactive: bool = False) -> tuple[bool, list[str], str]:
    """Connect once to a configured server and return ``(ok, tool_names, error)``."""
    dump = _server_config_dump(name)
    if dump is None:
        return False, [], f"unknown MCP server {name!r}"
    from flowly.mcp.probe import probe_tool_names
    return probe_tool_names(name, dump, interactive=interactive)
