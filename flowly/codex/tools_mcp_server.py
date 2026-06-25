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

Why hand-rolled (no ``mcp`` SDK)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Flowly ships no MCP *server* dependency (the ``hub`` package is an HTTP
skill-registry client, not MCP). Rather than pull in the ``mcp`` SDK
just for this callback, we speak the protocol directly: newline-
delimited JSON-RPC 2.0 over stdio, the same framing Flowly's Codex
transport already speaks on the other side. The surface we implement is
the minimum Codex's MCP client drives:

  * ``initialize``            → serverInfo + capabilities (echo the
                                client's requested protocolVersion)
  * ``notifications/initialized`` (client notification — ack, no reply)
  * ``tools/list``            → the curated tool schemas
  * ``tools/call``            → dispatch to the Flowly tool's execute()
  * ``ping``                  → ``{}``

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
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger("flowly.codex.tools_mcp_server")

# Highest MCP protocol version we understand. We echo the client's
# requested version when it's a string (servers are expected to accept
# the client's version or negotiate down); this constant is the fallback
# when the client omits it.
_FALLBACK_PROTOCOL_VERSION = "2025-06-18"

# Curated tool names exposed through the callback. Each MUST map to a
# Flowly tool constructible WITHOUT a live AgentLoop (stateless).
EXPOSED_TOOLS: tuple[str, ...] = (
    "web_search",
    "web_fetch",
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
# MCP stdio JSON-RPC server
# ---------------------------------------------------------------------------


class _StdioMCPServer:
    """Minimal newline-delimited JSON-RPC 2.0 MCP server over stdio."""

    def __init__(self, tools: dict[str, Any]) -> None:
        self._tools = tools

    def _tool_schema(self, tool: Any) -> dict[str, Any]:
        return {
            "name": tool.name,
            "description": tool.description,
            "inputSchema": tool.parameters
            or {"type": "object", "properties": {}},
        }

    async def _handle(self, msg: dict[str, Any]) -> dict[str, Any] | None:
        """Dispatch one request; return a JSON-RPC reply dict or None
        (for notifications, which get no reply)."""
        method = msg.get("method", "")
        rid = msg.get("id")
        params = msg.get("params") or {}

        # Notifications (no id) — ack silently.
        if rid is None:
            return None

        if method == "initialize":
            client_pv = params.get("protocolVersion")
            pv = client_pv if isinstance(client_pv, str) and client_pv else _FALLBACK_PROTOCOL_VERSION
            return _ok(rid, {
                "protocolVersion": pv,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "flowly-tools", "version": "1.0.0"},
            })

        if method == "ping":
            return _ok(rid, {})

        if method == "tools/list":
            return _ok(rid, {
                "tools": [self._tool_schema(t) for t in self._tools.values()],
            })

        if method == "tools/call":
            name = params.get("name") or ""
            arguments = params.get("arguments") or {}
            tool = self._tools.get(name)
            if tool is None:
                return _ok(rid, {
                    "content": [{"type": "text", "text": f"Unknown tool: {name}"}],
                    "isError": True,
                })
            try:
                result = await tool.execute(**arguments)
            except Exception as exc:
                logger.exception("tool %s raised", name)
                return _ok(rid, {
                    "content": [{"type": "text", "text": f"Error: {exc}"}],
                    "isError": True,
                })
            text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
            is_error = isinstance(text, str) and text.startswith("Error")
            return _ok(rid, {
                "content": [{"type": "text", "text": text}],
                "isError": is_error,
            })

        # Unknown method.
        return {
            "jsonrpc": "2.0",
            "id": rid,
            "error": {"code": -32601, "message": f"Method not found: {method}"},
        }

    async def run(self) -> None:
        """Read stdin line-by-line, dispatch, write replies to stdout."""
        loop = asyncio.get_event_loop()
        reader = asyncio.StreamReader()
        protocol = asyncio.StreamReaderProtocol(reader)
        await loop.connect_read_pipe(lambda: protocol, sys.stdin)

        # stdout writer — line-buffered JSON, flushed per message.
        def _write(obj: dict[str, Any]) -> None:
            sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
            sys.stdout.flush()

        while True:
            line = await reader.readline()
            if not line:
                break
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                logger.debug("non-JSON line on stdin: %r", line[:200])
                continue
            try:
                reply = await self._handle(msg)
            except Exception:
                logger.exception("handler crashed on %r", msg.get("method"))
                reply = None
            if reply is not None:
                _write(reply)


def _ok(rid: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": rid, "result": result}


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
