"""memory_search and memory_get tools."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from flowly.agent.tools.base import Tool


class MemorySearchTool(Tool):
    """
    Semantic + keyword search over memory files.

    Searches MEMORY.md and memory/*.md using hybrid BM25 + vector search.
    Falls back to keyword-only if no embedding provider is configured.
    """

    def __init__(self, manager: Any):  # MemoryIndexManager
        self._manager = manager

    @property
    def name(self) -> str:
        return "memory_search"

    @property
    def description(self) -> str:
        return (
            "Search MEMORY.md and memory/*.md for relevant information. "
            "ALWAYS call this before answering questions about prior conversations, "
            "past decisions, user preferences, names, dates, or ongoing projects. "
            "Returns the most relevant snippets ranked by relevance."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query — describe what you're looking for",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum number of results to return (default 6)",
                    "default": 6,
                },
            },
            "required": ["query"],
        }

    async def execute(self, query: str, max_results: int = 6, **_: Any) -> str:
        try:
            results = await self._manager.search(query, max_results=max_results)

            if not results:
                return json.dumps({
                    "results": [],
                    "message": "No relevant memory found for this query.",
                    "provider": self._manager.status()["provider"],
                })

            items = []
            for r in results:
                items.append({
                    "path": r.path,
                    "lines": f"L{r.start_line}-{r.end_line}",
                    "score": r.score,
                    "snippet": r.snippet,
                })

            return json.dumps({
                "results": items,
                "provider": self._manager.status()["provider"],
                "vector_enabled": self._manager.status()["vector_enabled"],
            }, ensure_ascii=False)

        except Exception as e:
            return json.dumps({"results": [], "error": str(e), "disabled": True})


class MemoryGetTool(Tool):
    """
    Read a specific section of a memory file by line range.

    Use after memory_search to pull exact lines for a result.
    """

    def __init__(self, manager: Any):  # MemoryIndexManager
        self._manager = manager

    @property
    def name(self) -> str:
        return "memory_get"

    @property
    def description(self) -> str:
        return (
            "Read a specific section of a memory file. "
            "Use the path and line numbers from memory_search results to pull exact content."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Relative path to the memory file (e.g. 'memory/MEMORY.md')",
                },
                "from_line": {
                    "type": "integer",
                    "description": "Start line number (1-based)",
                    "default": 1,
                },
                "lines": {
                    "type": "integer",
                    "description": "Number of lines to read (default 30)",
                    "default": 30,
                },
            },
            "required": ["path"],
        }

    async def execute(
        self,
        path: str,
        from_line: int = 1,
        lines: int = 30,
        **_: Any,
    ) -> str:
        try:
            snippet = self._manager.get_snippet(path, from_line, lines)
            if snippet is None:
                return json.dumps({"error": f"Path not found or not indexed: {path}"})
            return json.dumps({
                "path": path,
                "from_line": from_line,
                "text": snippet,
            }, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"error": str(e)})
