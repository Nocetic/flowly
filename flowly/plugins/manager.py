"""PluginManager — discovers, loads, and tracks installed plugins.

Discovery scans three sources, in order, with later sources overriding
earlier ones on key collision:

1. **Bundled** — ``flowly/plugins_bundled/<name>/`` shipped with the
   package.  Default-on (loaded unless explicitly listed in
   ``plugins.disabled``).
2. **User** — ``$FLOWLY_HOME/plugins/<name>/`` under the active profile.
   Opt-in via ``plugins.enabled``.
3. **Project** — ``./.flowly/plugins/<name>/`` opt-in via
   ``FLOWLY_ENABLE_PROJECT_PLUGINS=1``.  Treated like user plugins for
   enable/disable.

A plugin directory must contain a ``plugin.yaml`` (or ``plugin.json``)
manifest and an ``__init__.py`` exposing a ``register(ctx)`` function.

Each plugin's ``register(ctx)`` is wrapped in try/except so a failing
plugin only disables itself — the agent core keeps running.

Pip entry-points are NOT scanned in v1 (deferred to a later phase).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from flowly.plugins.context import PluginContext
from flowly.plugins.loader import load_plugin_module
from flowly.plugins.manifest import PluginManifest, find_manifest, parse_manifest
from flowly.profile import get_flowly_home

if TYPE_CHECKING:
    from flowly.agent.hooks import HookRegistry
    from flowly.agent.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)


# Bundled plugins ship next to ``flowly/`` package as ``plugins_bundled/``.
_BUNDLED_DIR = Path(__file__).resolve().parent.parent / "plugins_bundled"


@dataclass
class LoadedPlugin:
    """Runtime state for a single discovered plugin."""

    manifest: PluginManifest
    module: Any | None = None
    enabled: bool = False
    error: str | None = None
    tools_registered: list[str] = field(default_factory=list)
    hooks_registered: list[str] = field(default_factory=list)
    commands_registered: list[str] = field(default_factory=list)


class PluginManager:
    """Discovers, loads, and tracks plugins.

    The manager is a singleton (see :func:`get_plugin_manager`).  It is
    constructed once per :class:`AgentLoop` with references to the live
    :class:`ToolRegistry` and :class:`HookRegistry`.
    """

    def __init__(
        self,
        *,
        tool_registry: "ToolRegistry",
        hook_registry: "HookRegistry",
    ) -> None:
        self._tool_registry = tool_registry
        self._hook_registry = hook_registry
        self._plugins: dict[str, LoadedPlugin] = {}
        self._plugin_tool_names: dict[str, set[str]] = {}
        self._plugin_hook_names: dict[str, set[str]] = {}
        self._slash_commands: dict[str, dict[str, Any]] = {}
        self._plugin_skills: dict[str, dict[str, Any]] = {}
        self._discovered = False

    # ── Public API ─────────────────────────────────────────────

    def discover_and_load(
        self,
        *,
        force: bool = False,
        enabled: set[str] | None = None,
        disabled: set[str] | None = None,
    ) -> None:
        """Scan all sources and load matching plugins.

        Args:
            force: clear cache and re-scan; default off (idempotent).
            enabled: explicit allow-list (user plugins).  When ``None``
                read from config; if config has nothing, treat as
                "load nothing user-installed" (opt-in default).
            disabled: explicit deny-list (overrides everything,
                including bundled).
        """
        if self._discovered and not force:
            return
        if force:
            self._reset_state()

        if disabled is None:
            disabled = self._read_disabled_set()
        if enabled is None:
            enabled = self._read_enabled_set()

        manifests: list[PluginManifest] = []
        manifests += self._scan_dir(_BUNDLED_DIR, source="bundled")
        manifests += self._scan_dir(get_flowly_home() / "plugins", source="user")
        if _env_truthy("FLOWLY_ENABLE_PROJECT_PLUGINS"):
            manifests += self._scan_dir(
                Path.cwd() / ".flowly" / "plugins", source="project",
            )

        # Later source wins on key collision (user overrides bundled,
        # project overrides user).
        winners: dict[str, PluginManifest] = {}
        for m in manifests:
            winners[m.key] = m

        for manifest in winners.values():
            key = manifest.key
            if key in disabled or manifest.name in disabled:
                self._plugins[key] = LoadedPlugin(
                    manifest=manifest, enabled=False,
                    error="disabled in config",
                )
                continue

            # v1: only standalone plugins load.  backend/exclusive parse
            # but skip with a recorded reason (so `flowly plugins list`
            # surfaces them).
            if manifest.kind != "standalone":
                self._plugins[key] = LoadedPlugin(
                    manifest=manifest, enabled=False,
                    error=f"kind={manifest.kind} not supported in v1",
                )
                continue

            # Bundled plugins default-on; user/project plugins need to be
            # in plugins.enabled.
            should_load = (
                manifest.source == "bundled"
                or key in enabled
                or manifest.name in enabled
            )
            if not should_load:
                self._plugins[key] = LoadedPlugin(
                    manifest=manifest, enabled=False,
                    error=(
                        f"not in plugins.enabled "
                        f"(run `flowly plugins enable {key}`)"
                    ),
                )
                continue

            self._load_plugin(manifest)

        self._discovered = True
        logger.info(
            "plugin discovery complete: %d found, %d enabled",
            len(self._plugins),
            sum(1 for p in self._plugins.values() if p.enabled),
        )

    def list_plugins(self) -> list[dict[str, Any]]:
        """Return summary info for all discovered plugins."""
        return [
            {
                "key": p.manifest.key,
                "name": p.manifest.name,
                "version": p.manifest.version,
                "description": p.manifest.description,
                "source": p.manifest.source,
                "kind": p.manifest.kind,
                "enabled": p.enabled,
                "error": p.error,
                "tools": p.tools_registered,
                "hooks": p.hooks_registered,
                "commands": p.commands_registered,
            }
            for p in sorted(self._plugins.values(), key=lambda x: x.manifest.key)
        ]

    def get_slash_handler(self, name: str) -> Any | None:
        """Return the handler for plugin slash command ``/name``, or None."""
        clean = name.lstrip("/").lower()
        entry = self._slash_commands.get(clean)
        return entry["handler"] if entry else None

    def find_plugin_skill(self, qualified_name: str) -> Path | None:
        """Return the path to a ``<plugin>:<skill>`` SKILL.md, or None."""
        entry = self._plugin_skills.get(qualified_name)
        return entry["path"] if entry else None

    def list_plugin_skills(self) -> dict[str, dict[str, Any]]:
        """Return the full ``{qualified_name: entry}`` mapping."""
        return dict(self._plugin_skills)

    # ── Internal: scanning ─────────────────────────────────────

    def _scan_dir(
        self, root: Path, *, source: str,
    ) -> list[PluginManifest]:
        """Walk *root* looking for plugin manifests (depth-1 only in v1)."""
        if not root.is_dir():
            return []
        result: list[PluginManifest] = []
        for child in sorted(root.iterdir()):
            if not child.is_dir() or child.name.startswith("."):
                continue
            manifest_file = find_manifest(child)
            if manifest_file is None:
                logger.debug("skip %s (no manifest)", child)
                continue
            manifest = parse_manifest(manifest_file, child, source=source)
            if manifest is not None:
                result.append(manifest)
        return result

    # ── Internal: loading ─────────────────────────────────────

    def _load_plugin(self, manifest: PluginManifest) -> None:
        """Import the plugin module and call its ``register(ctx)``."""
        loaded = LoadedPlugin(manifest=manifest)
        try:
            assert manifest.path is not None
            module = load_plugin_module(manifest.path, manifest.key)
            loaded.module = module

            register_fn = getattr(module, "register", None)
            if register_fn is None:
                loaded.error = "missing register(ctx) function"
                logger.warning(
                    "plugin %s has no register() function", manifest.name,
                )
                self._plugins[manifest.key] = loaded
                return

            ctx = PluginContext(manifest, self)
            register_fn(ctx)

            loaded.tools_registered = sorted(
                self._plugin_tool_names.get(manifest.name, set())
            )
            loaded.hooks_registered = sorted(
                self._plugin_hook_names.get(manifest.name, set())
            )
            loaded.commands_registered = sorted(
                cmd for cmd, entry in self._slash_commands.items()
                if entry.get("plugin") == manifest.name
            )
            loaded.enabled = True
            logger.info(
                "loaded plugin %s (tools=%d hooks=%d commands=%d)",
                manifest.key,
                len(loaded.tools_registered),
                len(loaded.hooks_registered),
                len(loaded.commands_registered),
            )
        except Exception as exc:
            loaded.error = f"{type(exc).__name__}: {exc}"
            logger.exception("failed to load plugin %s", manifest.key)
        self._plugins[manifest.key] = loaded

    # ── Internal: config helpers ───────────────────────────────

    @staticmethod
    def _read_enabled_set() -> set[str]:
        """Read ``plugins.enabled`` from config.json.  Returns empty set
        if missing — v1 default is "no user plugins until explicitly
        enabled" (bundled plugins still load default-on)."""
        try:
            from flowly.config.loader import load_config
            config = load_config()
            plugins_cfg = getattr(config, "plugins", None)
            if plugins_cfg is None:
                return set()
            enabled = getattr(plugins_cfg, "enabled", None)
            if isinstance(enabled, list):
                return {str(x) for x in enabled}
        except Exception:
            logger.debug("could not read plugins.enabled", exc_info=True)
        return set()

    @staticmethod
    def _read_disabled_set() -> set[str]:
        """Read ``plugins.disabled`` from config.json."""
        try:
            from flowly.config.loader import load_config
            config = load_config()
            plugins_cfg = getattr(config, "plugins", None)
            if plugins_cfg is None:
                return set()
            disabled = getattr(plugins_cfg, "disabled", None)
            if isinstance(disabled, list):
                return {str(x) for x in disabled}
        except Exception:
            logger.debug("could not read plugins.disabled", exc_info=True)
        return set()

    # ── Internal: state ────────────────────────────────────────

    def _reset_state(self) -> None:
        self._plugins.clear()
        self._plugin_tool_names.clear()
        self._plugin_hook_names.clear()
        self._slash_commands.clear()
        self._plugin_skills.clear()


def _env_truthy(name: str) -> bool:
    return os.environ.get(name, "").lower() in ("1", "true", "yes", "on")
