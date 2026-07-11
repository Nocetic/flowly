"""Session picker modal — switch / delete saved sessions."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from textual import events, on
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Label, OptionList
from textual.widgets.option_list import Option


def _to_epoch(ts: float | int | str | None) -> float | None:
    """Coerce an epoch number OR an ISO-8601 string to a POSIX timestamp.

    Session timestamps land here as ISO strings (``created_at`` is written
    as ``datetime.isoformat()``), so the old ``float(ts)`` path always threw
    and the age column came up blank/stale.

    Numbers may arrive in **milliseconds**: the gateway serves sessions.list
    through the shared feature_rpc surface, whose ``updatedAt`` is
    ``st_mtime * 1000``. Treating those as seconds put every session in the
    future and the whole list rendered as "0s ago".
    """
    if ts is None or ts == "":
        return None
    if isinstance(ts, (int, float)):
        value = float(ts)
        return value / 1000 if value > 1e11 else value
    if isinstance(ts, str):
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def _fmt_age(ts: float | int | str | None) -> str:
    epoch = _to_epoch(ts)
    if epoch is None:
        return ""
    seconds = max(0, int(datetime.now().timestamp() - epoch))
    if seconds < 60:
        return f"{seconds}s ago"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86_400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86_400}d ago"


class SessionPickerPanel(Vertical):
    """Returns one of:
       {'action': 'switch', 'sessionKey': str}
       {'action': 'delete', 'sessionKey': str}
       None  (cancel)
    """

    can_focus = True

    class Dismissed(Message):
        def __init__(self, result: dict[str, Any] | None) -> None:
            super().__init__()
            self.result = result

    DEFAULT_CSS = """
    SessionPickerPanel {
        width: 100%;
        max-width: 100%;
        height: auto;
        max-height: 24;
        padding: 0;
        border: none;
        background: transparent;
    }
    SessionPickerPanel .title {
        text-style: bold;
        color: $primary;
        height: 1;
    }
    SessionPickerPanel .hint {
        color: $text-muted;
        text-style: italic;
        height: 1;
        margin-bottom: 1;
    }
    SessionPickerPanel OptionList {
        height: 20;
        border: none;
        background: transparent;
    }
    """

    BINDINGS = [
        ("escape", "cancel", "Close"),
        ("q", "cancel", "Close"),
        ("d", "delete", "Delete"),
    ]

    def __init__(self, sessions: list[dict[str, Any]], current: str) -> None:
        super().__init__()
        self._sessions = sessions
        self._visible_sessions = [s for s in sessions if str(s.get("key", ""))]
        self._current = current
        self._pending_delete: str | None = None  # press 'd' twice to confirm

    def compose(self) -> ComposeResult:
        yield Label("Sessions", classes="title")
        yield Label(
            "↑/↓ navigate · Enter switch · D delete (press twice) · Esc close",
            classes="hint",
        )
        ol = OptionList(id="session-list")
        for session in self._visible_sessions:
            key = str(session.get("key", ""))
            name = str(session.get("displayName") or key)
            age = _fmt_age(session.get("updatedAt") or session.get("createdAt"))
            marker = " ★" if key == self._current else "  "
            age_col = f" [dim]{age:>8}[/dim]" if age else ""
            ol.add_option(
                Option(f"{marker} {name:<40}{age_col}  [dim]{key}[/dim]", id=key)
            )
        yield ol

    def on_mount(self) -> None:
        ol = self.query_one(OptionList)
        # focus current session if visible
        for idx, session in enumerate(self._visible_sessions):
            if session.get("key") == self._current:
                ol.highlighted = idx
                break
        if ol.highlighted is None and ol.options:
            ol.highlighted = 0
        ol.focus()

    def on_focus(self) -> None:
        try:
            self.query_one(OptionList).focus()
        except Exception:
            pass

    def on_key(self, event: events.Key) -> None:
        if event.key != "d":
            self._pending_delete = None

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        key = str(event.option.id or "")
        if not key:
            return
        self.post_message(self.Dismissed({"action": "switch", "sessionKey": key}))

    def action_delete(self) -> None:
        ol = self.query_one(OptionList)
        if ol.highlighted is None:
            return
        opt = ol.get_option_at_index(ol.highlighted)
        key = str(opt.id or "")
        if not key:
            return
        if self._pending_delete == key:
            self.post_message(self.Dismissed({"action": "delete", "sessionKey": key}))
        else:
            self._pending_delete = key
            self.notify(f"press 'd' again to delete {key}", severity="warning", timeout=3)

    def action_cancel(self) -> None:
        self.post_message(self.Dismissed(None))


class SessionPicker(ModalScreen[dict[str, Any] | None]):
    """Compatibility wrapper; the chat TUI mounts :class:`SessionPickerPanel`."""

    BINDINGS = SessionPickerPanel.BINDINGS

    DEFAULT_CSS = """
    SessionPicker { align: center middle; }
    SessionPicker > SessionPickerPanel {
        width: 75%;
        max-width: 90;
        padding: 1 2;
        border: thick $primary;
        background: $surface;
    }
    """

    def __init__(self, sessions: list[dict[str, Any]], current: str) -> None:
        super().__init__()
        self._sessions = sessions
        self._current = current

    def compose(self) -> ComposeResult:
        yield SessionPickerPanel(self._sessions, self._current)

    @on(SessionPickerPanel.Dismissed)
    def _on_dismissed(self, event: SessionPickerPanel.Dismissed) -> None:
        event.stop()
        self.dismiss(event.result)

    def action_delete(self) -> None:
        self.query_one(SessionPickerPanel).action_delete()

    def action_cancel(self) -> None:
        self.query_one(SessionPickerPanel).action_cancel()
