"""memory_import tool — governed import from ChatGPT/Gemini memory dumps."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from flowly.agent.tools.base import Tool
from flowly.memory.dreamer import read_user_profile
from flowly.memory.importer import (
    MemoryDumpExtractor,
    memory_export_prompt,
    normalize_source,
    run_import,
)


class MemoryImportTool(Tool):
    def __init__(self, *, facade, provider, model: str, workspace: Path):
        self._facade = facade
        self._provider = provider
        self._model = model
        self._workspace = Path(workspace)

    @property
    def name(self) -> str:
        return "memory_import"

    @property
    def description(self) -> str:
        return (
            "Import a copied ChatGPT or Gemini memory/profile dump into Flowly's "
            "governed memory review queue. If text is empty, returns the prompt "
            "the user should paste into ChatGPT/Gemini first. Imported memories "
            "are never auto-activated; they await user review."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "source": {
                    "type": "string",
                    "enum": ["chatgpt", "gemini"],
                    "description": "External source of the memory dump.",
                    "default": "chatgpt",
                },
                "text": {
                    "type": "string",
                    "description": "The copied memory dump. Leave empty to get the export prompt.",
                },
                "force": {
                    "type": "boolean",
                    "description": "Re-import even if this exact dump was imported before.",
                    "default": False,
                },
            },
            "required": [],
        }

    async def execute(
        self,
        source: str = "chatgpt",
        text: str = "",
        force: bool = False,
        **kwargs: Any,
    ) -> str:
        try:
            source = normalize_source(source)
        except ValueError as exc:
            return f"Error: {exc}"

        if not (text or "").strip():
            return memory_export_prompt(source)

        loop = asyncio.get_running_loop()
        extractor = MemoryDumpExtractor(
            provider=self._provider,
            model=self._model,
            loop=loop,
        )
        res = await asyncio.to_thread(
            run_import,
            self._facade.gov,
            provider=self._provider,
            model=self._model,
            text=text,
            source=source,
            extractor=extractor,
            force=bool(force),
            on_committed=self._facade.refresh,
            profile_fn=lambda: read_user_profile(self._workspace),
        )
        if not res.ran and res.reason == "already_imported":
            return "This exact memory dump was already imported."
        if not res.ran:
            return f"Import skipped: {res.reason}."
        if res.candidates == 0:
            return "No durable memories were found in the import."
        return (
            f"Imported {res.candidates} memory candidates from {source}: "
            f"{res.needs_review} for review, {res.duplicates} duplicates, "
            f"{res.rejected} rejected."
        )
