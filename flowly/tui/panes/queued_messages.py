"""Queued messages widget — pending-input stack above the composer.

Sliding 3-item window with optional editing pointer:

    queued (5) · editing 2 · Ctrl+X delete · Esc cancel
     …
    ▸ 2. second queued message preview here
      3. third message
      4. fourth
     …and 1 more
"""

from __future__ import annotations

from textual.reactive import reactive
from textual.widgets import Static

QUEUE_WINDOW = 3   # max visible items at once


def _compact(text: str, width: int) -> str:
    """One-line preview, hard-cap at ``width`` chars with ellipsis."""
    flat = text.replace("\n", " ⏎ ").strip()
    if len(flat) <= width:
        return flat
    return flat[: max(1, width - 1)] + "…"


def _window(queue_len: int, edit_idx: int | None) -> tuple[int, int, bool, bool]:
    """Return (start, end, show_lead, show_tail) for the visible slice."""
    if edit_idx is None:
        start = 0
    else:
        start = max(0, min(edit_idx - 1, max(0, queue_len - QUEUE_WINDOW)))
    end = min(queue_len, start + QUEUE_WINDOW)
    return start, end, start > 0, end < queue_len


class QueuedMessages(Static):
    DEFAULT_CSS = """
    QueuedMessages {
        height: auto;
        padding: 0 2;
        background: #000000;
        color: #83b8c2;
        display: none;
    }
    QueuedMessages.has-queue { display: block; }
    """

    queue:    reactive[list[str]] = reactive(list, layout=True)
    edit_idx: reactive[int | None] = reactive(None, layout=True)

    def __init__(self, *args, **kwargs) -> None:
        super().__init__("", markup=True, *args, **kwargs)

    def watch_queue(self) -> None: self._refresh()
    def watch_edit_idx(self) -> None: self._refresh()

    def _refresh(self) -> None:
        q = self.queue or []
        if not q:
            self.remove_class("has-queue")
            self.update("")
            return
        self.add_class("has-queue")

        cols = max(40, (self.size.width or 80) - 6)
        edit = self.edit_idx
        start, end, lead, tail = _window(len(q), edit)

        lines: list[str] = []
        if edit is None:
            lines.append(f"[dim]queued ({len(q)})[/dim]")
        else:
            lines.append(
                f"[dim]queued ({len(q)}) · [#00a6c8]editing {edit + 1}[/] · "
                f"[dim]Ctrl+X delete · Esc cancel[/dim][/dim]"
            )

        if lead:
            lines.append("[dim] …[/dim]")

        for i in range(start, end):
            item = q[i]
            preview = _compact(item, cols)
            is_active = edit == i
            marker = "▸" if is_active else " "
            color = "#00a6c8" if is_active else "#83b8c2"
            lines.append(f"[{color}]{marker} {i + 1}. {preview}[/]")

        if tail:
            lines.append(f"[dim]   …and {len(q) - end} more[/dim]")

        self.update("\n".join(lines))
