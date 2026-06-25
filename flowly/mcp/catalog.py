"""MCP catalog — curated, version-pinned servers shipped in-repo (Faz 3, M2/M3).

Each entry lives at ``flowly/mcp_catalog/<name>/manifest.yaml``. Users discover
them with ``flowly mcp catalog``, install with ``flowly mcp install <name>``,
or browse interactively with ``flowly mcp picker``.

Install flow: resolve manifest → prompt for any declared env vars → write
secrets to ``$FLOWLY_HOME/.env`` (the Faz 1 loader reads them) → write the
``mcpServers`` entry to config.json → (caller probes + enables).

Manifest schema (v1):
    manifest_version: 1
    name: <str>
    description: <str>
    source: <url>
    transport: {type: stdio, command, args} | {type: http, url}
    auth: {type: none|api_key|oauth, env: [{name, prompt, secret, default}]}
    post_install: <str>
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


logger = logging.getLogger(__name__)

MANIFEST_VERSION = 1


@dataclass
class EnvVarSpec:
    name: str
    prompt: str
    secret: bool = True
    default: str = ""


@dataclass
class CatalogEntry:
    name: str
    description: str
    source: str
    transport: dict[str, Any]
    auth_type: str  # none | api_key | oauth
    env: list[EnvVarSpec] = field(default_factory=list)
    post_install: str = ""

    @property
    def transport_type(self) -> str:
        return str(self.transport.get("type", "stdio"))

    def transport_summary(self) -> str:
        if self.transport_type == "http":
            return f"http: {self.transport.get('url', '?')}"
        cmd = self.transport.get("command", "?")
        args = " ".join(str(a) for a in (self.transport.get("args") or [])[:3])
        return f"stdio: {cmd} {args}".strip()


def _catalog_dir() -> Path:
    # Ships as package data next to the flowly package.
    return Path(__file__).resolve().parent.parent / "mcp_catalog"


def _parse_manifest(path: Path) -> CatalogEntry | None:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        logger.warning("MCP catalog: failed to parse %s: %s", path, exc)
        return None
    if not isinstance(data, dict):
        return None
    if data.get("manifest_version") != MANIFEST_VERSION:
        logger.warning(
            "MCP catalog: %s has unsupported manifest_version %r",
            path, data.get("manifest_version"),
        )
        return None

    auth = data.get("auth") or {}
    env_specs = [
        EnvVarSpec(
            name=str(e.get("name", "")),
            prompt=str(e.get("prompt", e.get("name", ""))),
            secret=bool(e.get("secret", True)),
            default=str(e.get("default", "")),
        )
        for e in (auth.get("env") or [])
        if isinstance(e, dict) and e.get("name")
    ]
    try:
        return CatalogEntry(
            name=str(data["name"]),
            description=str(data.get("description", "")),
            source=str(data.get("source", "")),
            transport=dict(data.get("transport") or {}),
            auth_type=str(auth.get("type", "none")),
            env=env_specs,
            post_install=str(data.get("post_install", "")),
        )
    except KeyError as exc:
        logger.warning("MCP catalog: %s missing required field %s", path, exc)
        return None


def load_catalog() -> dict[str, CatalogEntry]:
    """Return ``{name: CatalogEntry}`` for every valid manifest, sorted by name."""
    out: dict[str, CatalogEntry] = {}
    base = _catalog_dir()
    if not base.is_dir():
        return out
    for manifest in sorted(base.glob("*/manifest.yaml")):
        entry = _parse_manifest(manifest)
        if entry:
            out[entry.name] = entry
    return out


def get_entry(name: str) -> CatalogEntry | None:
    return load_catalog().get(name)


def build_server_config(entry: CatalogEntry) -> dict[str, Any]:
    """Translate a catalog entry into an ``mcpServers`` config dict.

    Secrets/env declared by the entry are referenced via ``${VAR}`` so the
    actual values live in ``$FLOWLY_HOME/.env`` (written separately at
    install time), never inline in config.json.
    """
    cfg: dict[str, Any] = {"enabled": True}
    t = entry.transport
    if entry.transport_type == "http":
        cfg["url"] = t.get("url", "")
        if entry.auth_type == "oauth":
            cfg["auth"] = "oauth"
    else:
        cfg["command"] = t.get("command", "")
        cfg["args"] = list(t.get("args") or [])
        # api_key env that ISN'T already substituted into args becomes a
        # subprocess env entry referencing the .env value.
        arg_blob = " ".join(str(a) for a in cfg["args"])
        env_map = {}
        for spec in entry.env:
            if f"${{{spec.name}}}" in arg_blob:
                continue  # already interpolated into args
            env_map[spec.name] = f"${{{spec.name}}}"
        if env_map:
            cfg["env"] = env_map
    return cfg
