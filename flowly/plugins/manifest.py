"""Plugin manifest parsing.

A plugin manifest is a small declarative file (``plugin.yaml`` or
``plugin.json``) at the root of a plugin directory.  It states the
plugin's name, version, kind, declared tools/hooks, and any required
environment variables.

The full manifest schema includes ``kind: backend`` (pluggable
backends like web search providers) and ``kind: exclusive``
(single-active providers like memory).  ``standalone`` and ``backend``
plugins both load; ``exclusive`` parses without error but the manager
skips loading it (recorded with a reason) until that path is wired.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

try:
    import yaml
    _HAS_YAML = True
except ImportError:  # pragma: no cover — yaml is in deps but stay defensive
    yaml = None  # type: ignore[assignment]
    _HAS_YAML = False


_VALID_KINDS = {"standalone", "backend", "exclusive"}
_SUPPORTED_MANIFEST_VERSION = 1


@dataclass
class PluginManifest:
    """Parsed representation of a ``plugin.yaml`` or ``plugin.json`` file."""

    name: str
    version: str = ""
    description: str = ""
    author: str = ""
    kind: str = "standalone"
    manifest_version: int = 1
    requires_env: list[str | dict[str, Any]] = field(default_factory=list)
    provides_tools: list[str] = field(default_factory=list)
    provides_hooks: list[str] = field(default_factory=list)
    provides_web_providers: list[str] = field(default_factory=list)
    # Resolved at parse time:
    source: str = ""           # bundled | user | project
    path: Path | None = None
    key: str = ""              # registry key, falls back to name


def _coerce_kind(raw: Any, key: str) -> str:
    """Normalize ``kind`` to lowercase and validate."""
    if not isinstance(raw, str):
        return "standalone"
    kind = raw.strip().lower()
    if kind not in _VALID_KINDS:
        logger.warning(
            "plugin %s: unknown kind %r (valid: %s); treating as standalone",
            key, raw, ", ".join(sorted(_VALID_KINDS)),
        )
        return "standalone"
    return kind


def parse_manifest(
    manifest_path: Path,
    plugin_dir: Path,
    *,
    source: str,
    prefix: str = "",
) -> PluginManifest | None:
    """Parse a single manifest file into a :class:`PluginManifest`.

    Returns ``None`` and logs a warning on parse failure (malformed YAML,
    missing required fields, unsupported manifest_version).

    *prefix* is the parent category path for nested plugins (e.g.
    ``image_gen`` for ``plugins/image_gen/openai/plugin.yaml``).  Empty
    for flat layouts.
    """
    try:
        text = manifest_path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("cannot read %s: %s", manifest_path, exc)
        return None

    suffix = manifest_path.suffix.lower()
    try:
        if suffix == ".json":
            data = json.loads(text) or {}
        else:
            if not _HAS_YAML:
                logger.warning(
                    "PyYAML not installed — cannot parse %s", manifest_path,
                )
                return None
            data = yaml.safe_load(text) or {}
    except Exception as exc:
        logger.warning("failed to parse %s: %s", manifest_path, exc)
        return None

    if not isinstance(data, dict):
        logger.warning("%s: manifest root must be a mapping", manifest_path)
        return None

    name = str(data.get("name") or plugin_dir.name)
    key = f"{prefix}/{plugin_dir.name}" if prefix else name

    manifest_version = int(data.get("manifest_version", 1))
    if manifest_version > _SUPPORTED_MANIFEST_VERSION:
        logger.warning(
            "%s: manifest_version %d exceeds supported %d — upgrade flowly",
            manifest_path, manifest_version, _SUPPORTED_MANIFEST_VERSION,
        )
        return None

    return PluginManifest(
        name=name,
        version=str(data.get("version", "")),
        description=str(data.get("description", "")),
        author=str(data.get("author", "")),
        kind=_coerce_kind(data.get("kind", "standalone"), key),
        manifest_version=manifest_version,
        requires_env=list(data.get("requires_env", []) or []),
        provides_tools=list(data.get("provides_tools", []) or []),
        provides_hooks=list(data.get("provides_hooks", []) or []),
        provides_web_providers=list(data.get("provides_web_providers", []) or []),
        source=source,
        path=plugin_dir,
        key=key,
    )


def find_manifest(plugin_dir: Path) -> Path | None:
    """Locate the manifest file inside *plugin_dir*.

    Search order: ``plugin.yaml`` → ``plugin.yml`` → ``plugin.json``.
    Returns ``None`` if no manifest is found.
    """
    for name in ("plugin.yaml", "plugin.yml", "plugin.json"):
        candidate = plugin_dir / name
        if candidate.exists():
            return candidate
    return None
