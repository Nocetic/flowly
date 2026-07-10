"""Artifacts gallery + viewer modal."""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Awaitable, Callable
from typing import Any

from pygments.lexers import get_lexer_by_name
from pygments.util import ClassNotFound
from rich.markdown import Markdown as RichMarkdown
from rich.syntax import Syntax
from rich.table import Table
from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Label, ListItem, ListView, Static

from flowly.tui.theme import get_code_theme


def _render_artifact(art: dict[str, Any]):
    body = str(art.get("content", ""))
    atype = str(art.get("type", "")).lower()
    code_theme = get_code_theme()
    if atype == "code":
        language = str((art.get("metadata") or {}).get("language") or "text")
        try:
            get_lexer_by_name(language)
        except ClassNotFound:
            language = "text"
        return Syntax(body, language, theme=code_theme, line_numbers=True)
    if atype in ("python", "py", "javascript", "js", "ts", "typescript"):
        language = {
            "py": "python",
            "js": "javascript",
            "ts": "typescript",
        }.get(atype, atype)
        return Syntax(body, language, theme=code_theme, line_numbers=True)
    if atype == "html":
        return Syntax(body, "html", theme=code_theme)
    if atype == "svg":
        return Syntax(body, "xml", theme=code_theme)
    if atype in ("json", "chart", "form"):
        try:
            body = json.dumps(json.loads(body), indent=2, ensure_ascii=False)
        except (json.JSONDecodeError, TypeError, ValueError):
            pass
        return Syntax(body, "json", theme=code_theme)
    if atype == "csv":
        return _render_csv(body)
    if atype == "mermaid":
        return Syntax(body, "text", theme=code_theme)
    if atype == "latex":
        return Syntax(body, "tex", theme=code_theme)
    if atype in ("markdown", "md", "doc"):
        return RichMarkdown(body, code_theme=code_theme)
    return body or "(empty)"


def _render_csv(body: str) -> Table | str:
    rows: list[list[str]] = []
    truncated = False
    for idx, row in enumerate(csv.reader(io.StringIO(body))):
        if idx > 100:
            truncated = True
            break
        rows.append(row)
    if not rows:
        return "(empty)"
    column_count = min(max(len(row) for row in rows), 12)
    table = Table(show_header=True, header_style="bold cyan")
    header = rows[0]
    for idx in range(column_count):
        table.add_column(header[idx] if idx < len(header) else f"column {idx + 1}")
    for row in rows[1:101]:
        cells = [row[idx] if idx < len(row) else "" for idx in range(column_count)]
        table.add_row(*(cell[:80] for cell in cells))
    if truncated:
        table.caption = "Showing the first 100 rows"
    if max(len(row) for row in rows) > column_count:
        table.caption = (table.caption + " · " if table.caption else "") + "12 column limit"
    return table


class ArtifactsModal(ModalScreen[None]):
    DEFAULT_CSS = """
    ArtifactsModal { align: center middle; }
    ArtifactsModal > Vertical {
        width: 95%;
        max-width: 140;
        height: 90%;
        max-height: 40;
        border: thick #00a6c8;
        background: #050505;
    }
    ArtifactsModal .title { text-style: bold; color: #00a6c8; height: 1; padding: 1 2 0 2; }
    ArtifactsModal .hint  { color: #83b8c2; text-style: italic; height: 1; padding: 0 2 1 2; }
    ArtifactsModal > Vertical > Horizontal {
        height: 1fr;
    }
    ArtifactsModal ListView {
        width: 38;
        border-right: solid #0f4c5c;
        background: #050505;
    }
    ArtifactsModal ListItem { padding: 0 1; }
    ArtifactsModal VerticalScroll {
        width: 1fr;
        background: #0a0a0a;
        padding: 1 2;
    }
    ArtifactsModal Static { background: transparent; }
    """

    BINDINGS = [
        ("escape", "dismiss(None)", "Close"),
        ("q", "dismiss(None)", "Close"),
        ("left", "cycle(-1)", "Previous"),
        ("right", "cycle(1)", "Next"),
    ]

    def __init__(
        self,
        artifacts: list[dict[str, Any]],
        *,
        initial_index: int = 0,
        fetcher: Callable[[str], Awaitable[dict[str, Any] | None]] | None = None,
    ) -> None:
        super().__init__()
        self._artifacts = artifacts
        self._initial_index = (
            max(0, min(initial_index, len(artifacts) - 1)) if artifacts else 0
        )
        # Optional async loader for summary rows that carry no content
        # (the composer hint hands us lightweight session summaries).
        self._fetcher = fetcher

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(f"Artifacts ({len(self._artifacts)})", classes="title")
            yield Label(
                "↑/↓ · ←/→ navigate · Esc close",
                classes="hint",
            )
            with Horizontal():
                items: list[ListItem] = []
                for idx, a in enumerate(self._artifacts):
                    title = str(a.get("title") or a.get("id", "?"))
                    atype = str(a.get("type", "?"))
                    pinned = "★ " if a.get("pinned") else "  "
                    items.append(
                        ListItem(
                            Static(f"{pinned}[b]{title[:30]}[/b]\n   [dim]{atype}[/dim]"),
                            id=f"art-{idx}",
                        )
                    )
                yield ListView(*items, id="artifact-list")
                with VerticalScroll(id="artifact-view"):
                    yield Static(self._initial_preview(), id="art-body")

    def _initial_preview(self):
        if not self._artifacts:
            return "[dim]No artifacts yet. The agent creates artifacts via the `artifact` tool.[/dim]"
        return _render_artifact(self._artifacts[self._initial_index])

    def on_mount(self) -> None:
        if self._initial_index:
            try:
                self.query_one("#artifact-list", ListView).index = self._initial_index
            except Exception:
                pass

    def action_cycle(self, direction: int) -> None:
        if not self._artifacts:
            return
        try:
            list_view = self.query_one("#artifact-list", ListView)
        except Exception:
            return
        idx = list_view.index if list_view.index is not None else 0
        list_view.index = (idx + direction) % len(self._artifacts)

    def _current_index(self) -> int | None:
        try:
            return self.query_one("#artifact-list", ListView).index
        except Exception:
            return None

    @on(ListView.Highlighted, "#artifact-list")
    async def _on_highlight(self, event: ListView.Highlighted) -> None:
        idx = self._current_index()
        if idx is None or idx >= len(self._artifacts):
            return
        body = self.query_one("#art-body", Static)
        artifact = self._artifacts[idx]
        if "content" not in artifact and self._fetcher is not None:
            body.update("[dim]loading…[/dim]")
            artifact_id = str(artifact.get("id") or "")
            full: dict[str, Any] | None = None
            if artifact_id:
                try:
                    full = await self._fetcher(artifact_id)
                except Exception:
                    full = None
            if full:
                # Cache in place so revisits render without a refetch.
                artifact.update(full)
            elif self._current_index() == idx:
                body.update("[red]could not load artifact content[/red]")
                return
            # A newer highlight may have landed while we were fetching;
            # let its own handler own the preview in that case.
            if self._current_index() != idx:
                return
        body.update(_render_artifact(artifact))
