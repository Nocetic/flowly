"""Subagent activity sidebar — toggleable with Ctrl+A.

Listens to ``subagent.started`` / ``subagent.completed`` events that the
gateway broadcasts (see ``_broadcast_subagent_event`` in server.py and
``agent.subagents._on_event`` wiring in gateway_cmd.py).
"""

from __future__ import annotations

import time
from typing import Any

from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.reactive import reactive
from textual.widgets import Label, Static


class SubagentRow(Static):
    DEFAULT_CSS = """
    SubagentRow {
        height: auto;
        padding: 0 1;
        margin-bottom: 1;
    }
    SubagentRow.running { color: $warning; }
    SubagentRow.ok      { color: $success; }
    SubagentRow.fail    { color: $error; }
    """

    SPINNER = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]

    # Two reserved-name collisions Textual surfaced when a model swap
    # triggered a re-layout under load:
    #   • ``_render`` shadows Widget._render() which must return a Visual
    #     — our String-returning override broke get_content_height().
    #   • ``_task`` was getting clobbered by Textual's internal async
    #     task machinery (Widget keeps a ``_task`` reference for some
    #     of its lifecycle work). When that happened, our string was
    #     replaced by an asyncio.Task and ``.replace("\n", " ")`` blew
    #     up. Rename both to private, app-specific names.
    def __init__(self, run_id: str, label: str, task: str, model: str) -> None:
        super().__init__("", classes="running", markup=True)
        self.run_id = run_id
        self._row_label = label
        self._task_text = task
        self._row_model = model
        self._start = time.monotonic()
        self._done = False
        self._frame = 0
        self._status: str = "running"
        self._timer = None

    def on_mount(self) -> None:
        self._refresh_view()
        self._timer = self.set_interval(0.15, self._tick)

    def _tick(self) -> None:
        if self._done:
            return
        self._frame = (self._frame + 1) % len(self.SPINNER)
        self._refresh_view()

    def _refresh_view(self) -> None:
        elapsed = time.monotonic() - self._start
        elapsed_str = (
            f"{elapsed:.1f}s" if elapsed < 60 else f"{int(elapsed) // 60}m{int(elapsed) % 60}s"
        )
        icon = (
            self.SPINNER[self._frame]
            if not self._done
            else ("✓" if self._status == "ok" else "✗")
        )
        task_preview = str(self._task_text or "").replace("\n", " ")
        if len(task_preview) > 60:
            task_preview = task_preview[:58] + "…"
        self.update(
            f"{icon}  [b]{self._row_label}[/b]  [dim]· {elapsed_str}[/dim]\n"
            f"   [dim]{task_preview}[/dim]\n"
            f"   [dim]{self._row_model}[/dim]"
        )

    def complete(self, status: str, error: str | None = None) -> None:
        self._done = True
        self._status = "ok" if status in ("ok", "completed") else "fail"
        if self._timer:
            self._timer.stop()
        self.remove_class("running")
        self.add_class(self._status)
        self._refresh_view()

    @property
    def running(self) -> bool:
        return not self._done


class SubagentPane(VerticalScroll):
    """Sidebar pane that lists running + recent subagents."""

    AUTO_HIDE_DELAY = 2.5

    DEFAULT_CSS = """
    SubagentPane {
        dock: right;
        width: 36;
        height: 1fr;
        background: #050505;
        border-left: solid #0f4c5c;
        padding: 1 0;
        display: none;
    }
    SubagentPane.visible {
        display: block;
    }
    SubagentPane > .pane-title {
        height: 1;
        padding: 0 1;
        text-style: bold;
        color: $accent;
    }
    """

    visible: reactive[bool] = reactive(False)

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._pinned = False
        self._hide_timer: Any | None = None

    def compose(self) -> ComposeResult:
        yield Label("Subagents", classes="pane-title")

    def watch_visible(self, _old: bool, new: bool) -> None:
        if new:
            self.add_class("visible")
        else:
            self.remove_class("visible")

    def toggle(self) -> None:
        if self.visible:
            self.hide()
        else:
            self.show(pinned=True)

    def show(self, *, pinned: bool = False) -> None:
        self._cancel_auto_hide()
        self._pinned = pinned
        self.visible = True

    def hide(self) -> None:
        self._cancel_auto_hide()
        self._pinned = False
        self.visible = False

    def add_started(self, data: dict[str, Any]) -> None:
        rid = str(data.get("runId") or data.get("run_id") or "")
        if not rid:
            return
        if self._find(rid):
            return
        self._cancel_auto_hide()
        row = SubagentRow(
            rid,
            str(data.get("label", "?")),
            str(data.get("task", "")),
            str(data.get("model", "")),
        )
        self.mount(row)
        # Auto-show for live work, but don't pin it; idle completion can close it.
        if not self.visible:
            self.show(pinned=False)

    def mark_completed(self, data: dict[str, Any]) -> None:
        rid = str(data.get("runId") or data.get("run_id") or "")
        if not rid:
            return
        row = self._find(rid)
        if row is None:
            return
        status = str(data.get("status") or data.get("outcome") or "ok")
        row.complete(status, error=data.get("error"))
        self._schedule_auto_hide_if_idle()

    def running_count(self) -> int:
        return sum(1 for child in self.children if isinstance(child, SubagentRow) and child.running)

    def _schedule_auto_hide_if_idle(self) -> None:
        if self._pinned or self.running_count() > 0:
            return
        self._cancel_auto_hide()
        self._hide_timer = self.set_timer(self.AUTO_HIDE_DELAY, self._auto_hide_if_idle)

    def _auto_hide_if_idle(self) -> None:
        self._hide_timer = None
        if not self._pinned and self.running_count() == 0:
            self.visible = False

    def _cancel_auto_hide(self) -> None:
        if self._hide_timer is not None:
            self._hide_timer.stop()
            self._hide_timer = None

    def _find(self, run_id: str) -> SubagentRow | None:
        for child in self.children:
            if isinstance(child, SubagentRow) and child.run_id == run_id:
                return child
        return None
