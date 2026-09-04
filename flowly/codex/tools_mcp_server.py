"""Flowly-tools-as-MCP server for the Codex app-server runtime.

When a turn runs through ``codex app-server`` (the ``codex_session``
tool), Codex owns the loop and builds its own tool list: ``shell``,
``apply_patch``, ``update_plan``, ``view_image``, plus any native Codex
plugins. By default Flowly's own richer tools — web search, web fetch,
the skill library — are unreachable from inside that turn.

This module exposes a *curated, stateless* subset of Flowly's tools to
the spawned Codex subprocess over stdio MCP. Codex registers it as a
normal MCP server (``~/.codex/config.toml [mcp_servers.flowly-tools]``,
written by :mod:`flowly.codex.tool_migration`) and calls back into it
for capabilities its built-ins don't cover.

Protocol implementation
~~~~~~~~~~~~~~~~~~~~~~~

The callback uses the official MCP low-level server. This provides both
modern discovery and legacy initialization, protocol validation, concurrent
request handling, cancellation, and clean stdio framing without maintaining a
second protocol implementation inside Flowly.

What we expose (stateless only)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

  * ``web_search``  — Brave/relay-backed search
  * ``web_fetch``   — fetch + extract a URL
  * ``skill_view``  — read a skill from the workspace skill library
  * ``skills_list`` — list available skills

What we deliberately do NOT expose: ``exec`` / ``read_file`` /
``write_file`` (Codex's own ``shell`` + ``apply_patch`` cover these and
route through Codex's sandbox + approval), and anything that needs the
live AgentLoop (delegate, memory, cron, voice) — a stateless callback
can't drive those.

Run with: ``python -m flowly.codex.tools_mcp_server``
Spawned by: Codex (stdio MCP) when the runtime is active and
``tools.codex_session.expose_flowly_tools`` is True.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import mimetypes
import os
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger("flowly.codex.tools_mcp_server")

_MAX_MEDIA_BYTES = 25 * 1024 * 1024

# Curated tool names exposed through the callback. Each MUST map to a
# Flowly tool constructible WITHOUT a live AgentLoop (stateless).
EXPOSED_TOOLS: tuple[str, ...] = (
    "web_search",
    "web_fetch",
    "web_extract",
    "video_analyze",
    "skill_view",
    "skills_list",
)


def _build_tools() -> dict[str, Any]:
    """Construct the curated stateless Flowly tools, keyed by name.

    Best-effort: a tool that can't be built (missing config / import
    error) is simply omitted, so the callback degrades to whatever
    subset is constructible rather than failing to start.
    """
    tools: dict[str, Any] = {}

    # Load config + workspace once. Failures here are non-fatal — we
    # fall back to env-only tools (web_fetch).
    cfg = None
    workspace: Path | None = None
    try:
        from flowly.config.loader import load_config
        cfg = load_config()
    except Exception:
        logger.debug("config load failed in MCP callback", exc_info=True)
    try:
        from flowly.profile import get_flowly_home
        workspace = get_flowly_home() / "workspace"
    except Exception:
        workspace = Path.cwd()

    # web_fetch — fully standalone.
    try:
        from flowly.agent.tools.web import WebFetchTool
        tools["web_fetch"] = WebFetchTool()
    except Exception:
        logger.debug("WebFetchTool unavailable", exc_info=True)

    # web_extract — standalone; falls back to local readability when no paid
    # extract backend is configured.
    try:
        from flowly.agent.tools.web import WebExtractTool
        tools["web_extract"] = WebExtractTool()
    except Exception:
        logger.debug("WebExtractTool unavailable", exc_info=True)

    # web_search — needs the web tool config (api key) or the web
    # channel relay creds (proxy). Build from config when present.
    try:
        from flowly.agent.tools.web import WebSearchTool
        web_cfg = getattr(getattr(cfg, "tools", None), "web", None)
        chan = getattr(getattr(cfg, "channels", None), "web", None)
        tools["web_search"] = WebSearchTool(
            api_key=getattr(web_cfg, "api_key", "") or None,
            max_results=getattr(web_cfg, "max_results", 5) or 5,
            proxy_url=getattr(web_cfg, "proxy_url", "") or None,
            server_id=getattr(chan, "server_id", "") or None,
            auth_token=getattr(chan, "auth_token", "") or None,
        )
    except Exception:
        logger.debug("WebSearchTool unavailable", exc_info=True)

    # skill_view — needs the workspace path.
    try:
        from flowly.agent.tools.skill_view import SkillViewTool
        tools["skill_view"] = SkillViewTool(workspace=workspace)
    except Exception:
        logger.debug("SkillViewTool unavailable", exc_info=True)

    # skills_list — a thin wrapper around the skills loader.
    try:
        tools["skills_list"] = _SkillsListTool(workspace=workspace)
    except Exception:
        logger.debug("skills_list unavailable", exc_info=True)

    # video_analyze — hands a video (URL/path) to the active provider for
    # summarisation / transcription / Q&A. Stateless: build the provider
    # from config the same way the gateway does. Best-effort — skipped when
    # no provider is configured.
    try:
        from flowly.agent.tools.video_analyze import VideoAnalyzeTool
        from flowly.integrations.active_provider import resolve_active_provider
        from flowly.providers.factory import build_provider

        active = resolve_active_provider(cfg) if cfg is not None else None
        if active is not None:
            provider = build_provider(
                active,
                default_model=getattr(
                    getattr(getattr(cfg, "agents", None), "defaults", None),
                    "model", "",
                ) or "",
                config=cfg,
            )
            tools["video_analyze"] = VideoAnalyzeTool(provider=provider)
    except Exception:
        logger.debug("VideoAnalyzeTool unavailable", exc_info=True)

    return tools


class _SkillsListTool:
    """Minimal stateless 'list available skills' tool for the callback.

    Mirrors the Tool ABC surface (name / description / parameters /
    execute) so it dispatches through the same code path as the real
    tools, but reads the skill index directly so it needs no AgentLoop.
    """

    def __init__(self, *, workspace: Path) -> None:
        self._workspace = workspace

    @property
    def name(self) -> str:
        return "skills_list"

    @property
    def description(self) -> str:
        return (
            "List the skills available in the Flowly skill library "
            "(name + one-line description). Use skill_view to read one."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "filter": {
                    "type": "string",
                    "description": "Optional case-insensitive substring filter.",
                }
            },
        }

    async def execute(self, **kwargs: Any) -> str:
        filt = (kwargs.get("filter") or "").lower()
        try:
            from flowly.agent.skills import SkillsLoader
            loader = SkillsLoader(self._workspace)
            entries = loader.list_skills()
        except Exception as exc:
            return f"Error listing skills: {exc}"
        lines: list[str] = []
        for e in entries:
            name = e.get("name", "") if isinstance(e, dict) else str(e)
            source = e.get("source", "") if isinstance(e, dict) else ""
            if filt and filt not in f"{name} {source}".lower():
                continue
            lines.append(f"- {name} ({source})" if source else f"- {name}")
        return "\n".join(lines) if lines else "(no skills available)"


# ---------------------------------------------------------------------------
# Official MCP stdio server
# ---------------------------------------------------------------------------


class _StdioMCPServer:
    """Official dual-era MCP server over stdio for Flowly's curated tools."""

    def __init__(self, tools: dict[str, Any]) -> None:
        self._tools = tools
        from flowly import __version__
        from mcp.server.lowlevel import Server

        self._server = Server(
            "flowly-tools",
            title="Flowly Tools",
            version=__version__,
            description="Curated stateless Flowly tools for coding agents.",
            instructions=(
                "Use these tools for web retrieval, video analysis, and the "
                "Flowly skill library. Tool calls are stateless."
            ),
            on_list_tools=self._list_tools,
            on_call_tool=self._call_tool,
        )

    @staticmethod
    def _annotations(name: str) -> Any:
        from mcp import types

        return types.ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=name.startswith("web_") or name == "video_analyze",
        )

    def _tool_schema(self, tool: Any) -> Any:
        from mcp import types

        output_schema = getattr(tool, "output_schema", None)
        return types.Tool(
            name=tool.name,
            title=getattr(tool, "title", None),
            description=tool.description,
            inputSchema=tool.parameters or {"type": "object", "properties": {}},
            **({"outputSchema": output_schema} if isinstance(output_schema, dict) else {}),
            annotations=self._annotations(tool.name),
            _meta={"source": "flowly", "stateless": True},
        )

    async def _list_tools(self, _context: Any, _params: Any) -> Any:
        from mcp import types

        return types.ListToolsResult(
            tools=[self._tool_schema(tool) for tool in self._tools.values()],
            cacheScope="private",
        )

    @staticmethod
    def _error(message: str) -> Any:
        from mcp import types

        return types.CallToolResult(
            content=[types.TextContent(text=message)],
            isError=True,
        )

    @staticmethod
    def _media_content(paths: list[str]) -> list[Any]:
        from mcp import types

        content: list[Any] = []
        for raw_path in paths:
            path = Path(raw_path)
            try:
                size = path.stat().st_size
            except OSError:
                continue
            mime_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            if size <= _MAX_MEDIA_BYTES and mime_type.startswith(("image/", "audio/")):
                try:
                    with path.open("rb") as handle:
                        raw = handle.read(_MAX_MEDIA_BYTES + 1)
                except OSError:
                    continue
                if len(raw) > _MAX_MEDIA_BYTES:
                    continue
                encoded = base64.b64encode(raw).decode("ascii")
                if mime_type.startswith("image/"):
                    content.append(types.ImageContent(data=encoded, mimeType=mime_type))
                else:
                    content.append(types.AudioContent(data=encoded, mimeType=mime_type))
                continue
            try:
                uri = path.resolve().as_uri()
            except (OSError, ValueError):
                continue
            content.append(types.ResourceLink(
                name=path.name,
                uri=uri,
                mimeType=mime_type,
                size=size,
            ))
        return content

    @staticmethod
    def _format_success(result: Any) -> Any:
        from flowly.agent.reply_media import extract_reply_media
        from mcp import types

        text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
        media_paths, summary = extract_reply_media(text)
        visible_text = summary if summary is not None else text
        structured: Any = None
        if not media_paths:
            try:
                parsed = json.loads(text)
                if isinstance(parsed, (dict, list)):
                    structured = parsed
            except (json.JSONDecodeError, TypeError, ValueError):
                pass

        is_error = visible_text.lstrip().lower().startswith("error")
        if isinstance(structured, dict) and isinstance(structured.get("error"), str):
            is_error = True
        content: list[Any] = [types.TextContent(text=visible_text)]
        content.extend(_StdioMCPServer._media_content(media_paths))
        return types.CallToolResult(
            content=content,
            structuredContent=structured,
            isError=is_error,
        )

    async def _call_tool(self, _context: Any, params: Any) -> Any:
        name = params.name
        tool = self._tools.get(name)
        if tool is None:
            return self._error(f"Unknown tool: {name}")
        arguments = params.arguments or {}
        if not isinstance(arguments, dict):
            return self._error("Tool arguments must be an object")
        try:
            result = await tool.execute(**arguments)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            from flowly.mcp.security import sanitize_error

            logger.exception("tool %s raised", name)
            return self._error(sanitize_error(f"Error executing {name}: {type(exc).__name__}"))
        return self._format_success(result)

    async def list_tools(self) -> Any:
        """Test/introspection helper using the production list handler."""
        return await self._list_tools(None, None)

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        """Test/introspection helper using the production call handler."""
        from mcp import types

        return await self._call_tool(
            None,
            types.CallToolRequestParams(name=name, arguments=arguments or {}),
        )

    async def run(self) -> None:
        """Serve stdio with the SDK's validated, cancellation-aware transport."""
        from mcp.server.stdio import stdio_server

        async with stdio_server() as (read_stream, write_stream):
            await self._server.run(
                read_stream,
                write_stream,
                self._server.create_initialization_options(),
            )


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    verbose = "--verbose" in argv or "-v" in argv
    logging.basicConfig(
        level=logging.INFO if verbose else logging.WARNING,
        # MCP uses stdout for the protocol — logs MUST go to stderr.
        stream=sys.stderr,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    # Keep Flowly's own banners off stdout (which is the MCP wire).
    os.environ.setdefault("FLOWLY_QUIET", "1")

    tools = _build_tools()
    logger.info("flowly-tools MCP server exposing %d tool(s): %s",
                len(tools), ", ".join(tools.keys()))
    server = _StdioMCPServer(tools)
    try:
        asyncio.run(server.run())
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        logger.exception("flowly-tools MCP server crashed")
        sys.stderr.write(f"flowly-tools MCP server error: {exc}\n")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
