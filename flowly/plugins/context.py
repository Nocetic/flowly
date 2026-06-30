"""PluginContext — the facade handed to each plugin's ``register()``.

A plugin's entry point is a top-level ``register(ctx)`` function in its
package ``__init__.py``.  The *ctx* argument is a :class:`PluginContext`
that exposes the v1 surface area:

* :meth:`register_tool` — function-based tool registration (wrapped as
  a Flowly :class:`Tool` via :class:`FunctionToolAdapter`).
* :meth:`register_hook` — subscribe to any of the 14 lifecycle events.
* :meth:`register_command` — register an in-session slash command
  (``/foo``) usable across all channels (Telegram, Web, Desktop, …).
* :meth:`register_skill` — register a plugin-namespaced skill that can
  be loaded via ``skill_view("<plugin>:<name>")``.
* :meth:`register_web_search_provider` — register a ``kind: backend``
  web search/extract backend selectable by the ``web_search`` /
  ``web_extract`` tools.

Methods deferred to v1.1+ (not yet wired):

* ``register_image_gen_provider`` / ``register_context_engine`` —
  Flowly does not yet have pluggable image_gen / context_engine
  abstractions in core.
* ``register_cli_command`` — terminal sub-command registration.
* ``inject_message`` / ``dispatch_tool`` — plugin-initiated message
  injection and tool dispatch.

These are intentionally absent — the surface is small and tested.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from flowly.agent.hooks import VALID_EVENTS
from flowly.plugins.adapter import FunctionToolAdapter
from flowly.plugins.manifest import PluginManifest

if TYPE_CHECKING:
    from flowly.plugins.manager import PluginManager

logger = logging.getLogger(__name__)


# Slash command names that are reserved by core flowly (defined in
# loop.py:_process_message_inner).  Plugins cannot override these.
RESERVED_SLASH_COMMANDS: set[str] = {"new", "clear", "compact", "help"}


class PluginContext:
    """Facade given to a plugin's ``register()`` function."""

    def __init__(
        self, manifest: PluginManifest, manager: "PluginManager",
    ) -> None:
        self.manifest = manifest
        self._manager = manager

    # ── Tool registration ──────────────────────────────────────

    def register_tool(
        self,
        *,
        name: str,
        schema: dict[str, Any],
        handler: Callable[..., Any],
        check_fn: Callable[[], bool] | None = None,
        description: str = "",
        toolset: str = "",  # accepted for upstream compat, currently unused
        requires_env: list[str] | None = None,  # ditto
        is_async: bool = False,  # ditto — adapter detects awaitables
        emoji: str = "",  # ditto
    ) -> None:
        """Register a function-based tool.

        Wraps the handler in a :class:`FunctionToolAdapter` and inserts it
        into the active :class:`ToolRegistry`.  Plugin tool names are
        tracked so ``manager.list_plugins()`` can report them.
        """
        adapter = FunctionToolAdapter(
            name=name,
            schema=schema,
            handler=handler,
            check_fn=check_fn,
            description=description,
        )
        self._manager._tool_registry.register(adapter)
        self._manager._plugin_tool_names.setdefault(self.manifest.name, set()).add(name)
        logger.debug("plugin %s registered tool %s", self.manifest.name, name)

    # ── Hook registration ──────────────────────────────────────

    def register_hook(self, hook_name: str, callback: Callable[..., Any]) -> None:
        """Subscribe *callback* to *hook_name*.

        Unknown hook names produce a warning but are still accepted so
        forward-compatible plugins don't break.
        """
        if hook_name not in VALID_EVENTS:
            logger.warning(
                "plugin %s: unknown hook event %r (valid: %s)",
                self.manifest.name, hook_name,
                ", ".join(sorted(VALID_EVENTS)),
            )
        self._manager._hook_registry.register(hook_name, callback)
        self._manager._plugin_hook_names.setdefault(
            self.manifest.name, set()
        ).add(hook_name)
        logger.debug("plugin %s registered hook %s", self.manifest.name, hook_name)

    # ── Slash command registration ─────────────────────────────

    def register_command(
        self,
        name: str,
        handler: Callable[[str], Any],
        description: str = "",
        args_hint: str = "",
    ) -> None:
        """Register an in-session slash command (e.g. ``/disk-cleanup``).

        The handler signature is ``fn(raw_args: str) -> str | None``
        (sync or async).  A return value is sent as the response;
        ``None`` means "no response, fire-and-forget".

        Names conflicting with built-in commands (``/new``, ``/clear``,
        ``/compact``, ``/help``) are rejected with a warning.
        """
        clean = name.strip().lstrip("/").lower().replace(" ", "-")
        if not clean:
            logger.warning(
                "plugin %s: empty slash command name", self.manifest.name,
            )
            return
        if clean in RESERVED_SLASH_COMMANDS:
            logger.warning(
                "plugin %s tried to override reserved /%s — skipping",
                self.manifest.name, clean,
            )
            return
        existing = self._manager._slash_commands.get(clean)
        if existing and existing["plugin"] != self.manifest.name:
            logger.warning(
                "plugin %s: /%s already registered by %s — overriding",
                self.manifest.name, clean, existing["plugin"],
            )
        self._manager._slash_commands[clean] = {
            "handler": handler,
            "description": description or "",
            "plugin": self.manifest.name,
            "args_hint": (args_hint or "").strip(),
        }
        logger.debug(
            "plugin %s registered /%s", self.manifest.name, clean,
        )

    # ── Skill registration ─────────────────────────────────────

    def register_skill(
        self, name: str, path: Path, description: str = "",
    ) -> None:
        """Register a plugin-namespaced skill.

        The skill becomes resolvable as ``"<plugin>:<name>"`` via
        :func:`skill_view`.  It does NOT enter the flat
        ``~/.flowly/skills/`` tree and is NOT shown in the system
        prompt's available-skills index — plugin skills are explicit
        loads only.
        """
        if ":" in name:
            raise ValueError(
                f"skill name {name!r} must not contain ':' — namespace is "
                f"derived from plugin name {self.manifest.name!r} automatically"
            )
        if not name or not name.replace("-", "").replace("_", "").isalnum():
            raise ValueError(
                f"invalid skill name {name!r}; must match [a-zA-Z0-9_-]+"
            )
        if not path.exists():
            raise FileNotFoundError(f"SKILL.md not found at {path}")

        qualified = f"{self.manifest.name}:{name}"
        self._manager._plugin_skills[qualified] = {
            "path": path,
            "plugin": self.manifest.name,
            "bare_name": name,
            "description": description,
        }
        logger.debug(
            "plugin %s registered skill %s",
            self.manifest.name, qualified,
        )

    # ── Web search provider registration ───────────────────────

    def register_web_search_provider(self, provider: Any) -> None:
        """Register a pluggable web search/extract backend.

        *provider* must be a
        :class:`flowly.agent.tools.web_providers.WebSearchProvider`
        instance.  Once registered it becomes selectable as the active
        ``web_search`` / ``web_extract`` backend via
        ``tools.web.search.backend`` (and the per-capability
        ``searchBackend`` / ``extractBackend`` keys).  Only plugins
        declaring ``kind: backend`` should call this.
        """
        from flowly.agent.tools.web_providers.registry import register_provider

        register_provider(provider)
        self._manager._plugin_web_providers.setdefault(
            self.manifest.name, set()
        ).add(provider.name)
        logger.debug(
            "plugin %s registered web provider %s",
            self.manifest.name, provider.name,
        )
