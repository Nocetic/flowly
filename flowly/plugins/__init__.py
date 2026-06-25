"""Flowly plugin system.

Plugins extend Flowly with custom tools, lifecycle hooks, slash
commands, and skills.  Each plugin is a directory containing a
``plugin.yaml`` manifest and an ``__init__.py`` exposing a
``register(ctx)`` function.

Discovery sources (later overrides earlier on key collision):

1. ``flowly/plugins_bundled/`` — ships with the package (default-on).
2. ``$FLOWLY_HOME/plugins/`` — per-profile user plugins
   (opt-in via ``plugins.enabled`` in ``config.json``).
3. ``./.flowly/plugins/`` — project-scoped, opt-in via the
   ``FLOWLY_ENABLE_PROJECT_PLUGINS=1`` env var.

Public API
----------

* :func:`get_plugin_manager` — singleton accessor.  Pass the live
  registries the first time it is called (typically from
  :class:`AgentLoop`).  Subsequent calls without arguments return the
  same instance.
* :func:`discover_plugins` — convenience wrapper that calls
  :meth:`PluginManager.discover_and_load` on the singleton.

Plugin authors interact with the system exclusively through the
:class:`PluginContext` object passed to ``register(ctx)``.  See
``docs/plugins.md`` for the authoring guide.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from flowly.plugins.adapter import FunctionToolAdapter
from flowly.plugins.context import PluginContext
from flowly.plugins.manager import PluginManager
from flowly.plugins.manifest import PluginManifest, parse_manifest

if TYPE_CHECKING:
    from flowly.agent.hooks import HookRegistry
    from flowly.agent.tools.registry import ToolRegistry

__all__ = [
    "PluginManager",
    "PluginContext",
    "PluginManifest",
    "FunctionToolAdapter",
    "parse_manifest",
    "get_plugin_manager",
    "discover_plugins",
]


_singleton: PluginManager | None = None


def get_plugin_manager(
    *,
    tool_registry: "ToolRegistry | None" = None,
    hook_registry: "HookRegistry | None" = None,
) -> PluginManager:
    """Return the global :class:`PluginManager`, creating it on first call.

    The first call MUST pass *tool_registry* and *hook_registry*; later
    calls may omit them and will return the existing instance.
    """
    global _singleton
    if _singleton is None:
        if tool_registry is None or hook_registry is None:
            raise RuntimeError(
                "PluginManager not yet initialised — first call must pass "
                "tool_registry and hook_registry"
            )
        _singleton = PluginManager(
            tool_registry=tool_registry,
            hook_registry=hook_registry,
        )
    return _singleton


def discover_plugins(*, force: bool = False) -> None:
    """Run :meth:`PluginManager.discover_and_load` on the singleton."""
    if _singleton is None:
        raise RuntimeError(
            "PluginManager not yet initialised — call get_plugin_manager() first"
        )
    _singleton.discover_and_load(force=force)


def _reset_for_tests() -> None:
    """Reset the singleton — test-only escape hatch."""
    global _singleton
    _singleton = None
