"""Artifacts gallery + viewer modal."""

from __future__ import annotations

from typing import Any

from rich.markdown import Markdown as RichMarkdown
from rich.syntax import Syntax
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
    if atype in ("python", "py"):
        return Syntax(body, "python", theme=code_theme, line_numbers=True)
    if atype in ("javascript", "js", "ts", "typescript"):
        return Syntax(body, "javascript", theme=code_theme, line_numbers=True)
    if atype in ("html",):
        return Syntax(body, "html", theme=code_theme)
    if atype in ("json",):
        return Syntax(body, "json", theme=code_theme)
    if atype in ("markdown", "md", "doc"):
        return RichMarkdown(body, code_theme=code_theme)
    return body or "(empty)"


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
    ]

    def __init__(self, artifacts: list[dict[str, Any]]) -> None:
        super().__init__()
        self._artifacts = artifacts

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(f"Artifacts ({len(self._artifacts)})", classes="title")
            yield Label(
                "↑/↓ navigate · Enter view full · Esc close",
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
        return _render_artifact(self._artifacts[0])

    @on(ListView.Highlighted, "#artifact-list")
    def _on_highlight(self, event: ListView.Highlighted) -> None:
        try:
            idx = self.query_one("#artifact-list", ListView).index
        except Exception:
            return
        if idx is None or idx >= len(self._artifacts):
            return
        body = self.query_one("#art-body", Static)
        body.update(_render_artifact(self._artifacts[idx]))
