"""Activity / audit-log modal — recent LLM + tool calls with stats."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from textual import on
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import DataTable, Label


def _fmt_ts(iso: str) -> str:
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt.strftime("%H:%M:%S")
    except Exception:
        return iso[:8] if iso else ""


def _fmt_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


class ActivityPanel(Vertical):
    can_focus = True

    class Dismissed(Message):
        pass

    DEFAULT_CSS = """
    ActivityPanel {
        width: 100%;
        max-width: 100%;
        height: auto;
        max-height: 24;
        border: none;
        background: transparent;
        padding: 0;
    }
    ActivityPanel .title  { text-style: bold; color: #00a6c8; height: 1; }
    ActivityPanel .meta   { color: #83b8c2; height: 1; margin-bottom: 1; }
    ActivityPanel DataTable { height: 18; background: transparent; }
    ActivityPanel .hint   { color: #83b8c2; text-style: italic; height: 1; margin-top: 1; }
    """

    BINDINGS = [
        ("escape", "cancel", "Close"),
        ("q", "cancel", "Close"),
    ]

    def __init__(self, entries: list[dict[str, Any]], stats: dict[str, Any]) -> None:
        super().__init__()
        self._entries = entries
        self._stats = stats

    def compose(self) -> ComposeResult:
        yield Label("Activity log", classes="title")
        stats = self._stats
        files = stats.get("files", "?")
        size = _fmt_bytes(int(stats.get("total_bytes") or 0))
        span = f"{stats.get('oldest_date', '?')} → {stats.get('newest_date', '?')}"
        retention = stats.get("retention_days", "?")
        yield Label(
            f"{len(self._entries)} entries · {files} files · {size} on disk · "
            f"{span} · retention {retention}d",
            classes="meta",
        )
        tbl = DataTable(zebra_stripes=False)
        tbl.add_columns("time", "type", "session", "what", "dur", "tokens")
        for e in self._entries:
            t = e.get("type", "?")
            if t == "llm_call":
                what = f"{e.get('model', '?')}  {e.get('finish_reason', '')}"
                tokens = f"{e.get('prompt_tokens', 0)}↑ {e.get('completion_tokens', 0)}↓"
            elif t == "tool_call":
                what = f"{e.get('tool', '?')}  {'✓' if e.get('success', True) else '✗'}"
                tokens = ""
            else:
                what = str(e.get("message", ""))[:40]
                tokens = ""
            dur = e.get("duration_ms")
            dur_str = f"{dur}ms" if dur and dur < 1000 else (f"{dur / 1000:.1f}s" if dur else "")
            session = (e.get("session") or "")[:18]
            tbl.add_row(_fmt_ts(e.get("ts", "")), t, session, what[:50], dur_str, tokens)
        yield tbl
        yield Label("Esc to close · table scrolls with ↑/↓ and ←/→", classes="hint")

    def on_mount(self) -> None:
        self.query_one(DataTable).focus()

    def on_focus(self) -> None:
        try:
            self.query_one(DataTable).focus()
        except Exception:
            pass

    def action_cancel(self) -> None:
        self.post_message(self.Dismissed())


class ActivityModal(ModalScreen[None]):
    """Compatibility wrapper; the chat TUI mounts :class:`ActivityPanel`."""

    BINDINGS = ActivityPanel.BINDINGS

    DEFAULT_CSS = """
    ActivityModal { align: center middle; }
    ActivityModal > ActivityPanel {
        width: 95%;
        max-width: 140;
        padding: 1 2;
        border: thick #00a6c8;
        background: #050505;
    }
    """

    def __init__(self, entries: list[dict[str, Any]], stats: dict[str, Any]) -> None:
        super().__init__()
        self._entries = entries
        self._stats = stats

    def compose(self) -> ComposeResult:
        yield ActivityPanel(self._entries, self._stats)

    @on(ActivityPanel.Dismissed)
    def _on_dismissed(self, event: ActivityPanel.Dismissed) -> None:
        event.stop()
        self.dismiss(None)

    def action_cancel(self) -> None:
        self.query_one(ActivityPanel).action_cancel()
