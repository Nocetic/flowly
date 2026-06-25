"""CLI-side plugin discovery + enable/disable.

Mirrors the desktop app's ``pluginsList`` / ``pluginEnable`` /
``pluginDisable`` methods (see ``flowly-desktop/src/main/local/
flowlyai-service.ts:2204+``) so the TUI's ``/plugins`` modal can manage
plugins without a roundtrip to the gateway. Single source of truth:
``~/.flowly/config.json`` ``plugins.enabled`` / ``plugins.disabled``
arrays — same fields the Python ``PluginManager`` reads at boot.

Discovery semantics (matches ``flowly.plugins.manager._scan_dir``):
- **bundled** — under ``flowly/plugins_bundled/`` shipped with the
  package. Default-on, opt-out via ``plugins.disabled``.
- **user** — under ``$FLOWLY_HOME/plugins/``. Opt-in via
  ``plugins.enabled``.

Each plugin directory needs a ``plugin.yaml`` (or ``.json``) manifest.
Failed parses surface as ``status="error"`` with the reason — gives
the user something actionable instead of silently hiding the plugin.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal


@dataclass
class PluginEntry:
    """One row in the ``/plugins`` modal list."""
    key: str
    name: str
    version: str
    description: str
    source: Literal["bundled", "user"]
    kind: str
    enabled: bool
    error: str | None
    status: Literal["enabled", "disabled", "available", "error"]


def list_plugins() -> list[PluginEntry]:
    """Discover + classify every plugin currently installed.

    Walk bundled + user dirs once each; later sources (user) override
    earlier ones (bundled) on key collision — mirrors PluginManager's
    behaviour. Status is derived from the per-source enable rules + the
    config flags:

    - bundled & not in disabled → ``enabled``
    - bundled & in disabled    → ``disabled``
    - user    & in enabled     → ``enabled``
    - user    & not in enabled → ``available`` (installed but inert)
    - parse failed             → ``error`` (reason in ``.error``)
    """
    from flowly.config.loader import load_config
    from flowly.plugins.manifest import find_manifest, parse_manifest
    from flowly.profile import get_flowly_home

    try:
        cfg = load_config()
        enabled_set = set(cfg.plugins.enabled or [])
        disabled_set = set(cfg.plugins.disabled or [])
    except Exception:
        enabled_set = set()
        disabled_set = set()

    bundled_dir = Path(__file__).resolve().parent.parent / "plugins_bundled"
    user_dir = get_flowly_home() / "plugins"

    by_key: dict[str, PluginEntry] = {}
    for root, source in ((bundled_dir, "bundled"), (user_dir, "user")):
        if not root.is_dir():
            continue
        for child in sorted(root.iterdir()):
            if not child.is_dir() or child.name.startswith((".", "__")):
                continue
            manifest_file = find_manifest(child)
            if manifest_file is None:
                # No manifest → treat as parse error so user knows the
                # directory was found but ignored.
                by_key[child.name] = PluginEntry(
                    key=child.name, name=child.name, version="", description="",
                    source=source, kind="",
                    enabled=False, error="no plugin.yaml / plugin.json",
                    status="error",
                )
                continue
            manifest = parse_manifest(manifest_file, child, source=source)
            if manifest is None:
                by_key[child.name] = PluginEntry(
                    key=child.name, name=child.name, version="", description="",
                    source=source, kind="",
                    enabled=False, error="manifest parse failed",
                    status="error",
                )
                continue
            key = manifest.key

            # Status derivation mirrors PluginManager.discover_and_load
            # AND desktop's pluginsList — keep all three aligned or the
            # UI shows different things than the agent actually loads.
            if key in disabled_set or manifest.name in disabled_set:
                enabled, status = False, "disabled"
                error: str | None = "disabled in config"
            elif source == "bundled":
                enabled, status = True, "enabled"
                error = None
            elif key in enabled_set or manifest.name in enabled_set:
                enabled, status = True, "enabled"
                error = None
            else:
                enabled, status = False, "available"
                error = None
            by_key[key] = PluginEntry(
                key=key, name=manifest.name,
                version=manifest.version or "",
                description=manifest.description or "",
                source=source, kind=manifest.kind,
                enabled=enabled, error=error, status=status,
            )

    return sorted(by_key.values(), key=lambda p: p.key)


def set_plugin_enabled(key: str, enabled: bool) -> None:
    """Flip a plugin's effective state by mutating the config flags.

    Three states to handle — each writes a different combination:

    - **bundled enabling**: remove from ``disabled``. No-op for
      ``enabled`` list (bundled is default-on).
    - **bundled disabling**: add to ``disabled``. (Removing from
      ``enabled`` is also fine but unnecessary.)
    - **user enabling**: add to ``enabled``, remove from ``disabled``.
    - **user disabling**: remove from ``enabled``, add to ``disabled``.

    We touch both lists symmetrically so the final config is
    self-consistent regardless of the user's prior manual edits.
    """
    from flowly.config.loader import get_config_path
    from flowly.integrations.config_io import (
        _atomic_write_json, _load_raw, _set_path,
    )
    raw = _load_raw()
    plugins_cfg = (raw.get("plugins") or {})
    enabled_list = list(plugins_cfg.get("enabled") or [])
    disabled_list = list(plugins_cfg.get("disabled") or [])

    if enabled:
        if key not in enabled_list:
            enabled_list.append(key)
        if key in disabled_list:
            disabled_list.remove(key)
    else:
        if key in enabled_list:
            enabled_list.remove(key)
        if key not in disabled_list:
            disabled_list.append(key)

    _set_path(raw, "plugins", {
        "enabled": enabled_list,
        "disabled": disabled_list,
    }, merge=True)
    _atomic_write_json(get_config_path(), raw)


def get_plugin(key: str) -> PluginEntry | None:
    """Return the entry for ``key`` or ``None`` if not installed."""
    for p in list_plugins():
        if p.key == key:
            return p
    return None
