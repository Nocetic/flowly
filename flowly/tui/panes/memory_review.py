"""Inline "review new memories" panel — appears directly above the composer
input on TUI open (and via ``/memory``) when the bot's review queue has pending
candidates. One item at a time: keep / discard / skip, advancing as you choose.

Mirrors :class:`ApprovalPrompt`'s inline pattern: a focusable ``Vertical`` mounted
in the Composer, shown/hidden via a CSS class, that posts a ``Decision`` message
the app acts on (accept/reject over the gateway feature RPC, then advance)."""

from __future__ import annotations

from rich.markup import escape
from textual import events
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.message import Message
from textual.widgets import Static

# Fixed actions, in display order: (shortcut key, label, action id).
_ACTIONS: tuple[tuple[str, str, str], ...] = (
    ("a", "Keep", "keep"),
    ("r", "Discard", "discard"),
    ("s", "Skip", "skip"),
)


class _ActionRow(Static):
    """One keep/discard/skip choice row."""

    def __init__(self, index: int, key: str, label: str, action: str) -> None:
        super().__init__("", classes="review-option", markup=False)
        self.index = index
        self.key = key
        self.label = label
        self.action = action

    def set_selected(self, selected: bool) -> None:
        marker = "›" if selected else " "
        self.update(f"{marker} [{self.key}] {self.label}")
        self.set_class(selected, "selected")

    def on_click(self, event: events.Click) -> None:
        event.stop()
        self.post_message(MemoryReviewPanel.Decision(self.action))


class MemoryReviewPanel(Vertical):
    """Inline review queue that appears directly above the composer input."""

    can_focus = True

    class Decision(Message):
        def __init__(self, action: str) -> None:
            super().__init__()
            self.action = action  # "keep" | "discard" | "skip" | "close"

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._selected_idx = 0

    def compose(self) -> ComposeResult:
        yield Static("", id="review-title", markup=False)
        yield Static("", id="review-meta", markup=True)
        yield Static("", id="review-text", markup=False)
        for idx, (key, label, action) in enumerate(_ACTIONS):
            yield _ActionRow(idx, key, label, action)
        yield Static(
            "[a] keep · [r] discard · [s] skip · ↑/↓ Enter · Esc close",
            id="review-hint", markup=False,
        )

    def on_mount(self) -> None:
        self._render_options()

    def set_item(self, item: dict, idx: int, total: int) -> None:
        kind = str(item.get("kind") or "memory")
        pct = ""
        conf = item.get("confidence")
        try:
            if conf is not None:
                pct = f" · {round(float(conf) * 100)}%"
        except (TypeError, ValueError):
            pct = ""
        self.query_one("#review-title", Static).update(f"Review memory · {idx + 1}/{total}")
        self.query_one("#review-meta", Static).update(f"[dim]{escape(kind)}{pct}[/dim]")
        self.query_one("#review-text", Static).update(str(item.get("text") or ""))
        self._selected_idx = 0
        self._render_options()
        self.focus_options()

    def clear(self) -> None:
        try:
            for wid in ("review-title", "review-meta", "review-text"):
                self.query_one(f"#{wid}", Static).update("")
            self._selected_idx = 0
            self._render_options()
        except Exception:
            pass

    def focus_options(self) -> None:
        try:
            self.focus()
        except Exception:
            pass

    def route_editor_key(self, key: str) -> bool:
        k = key.lower().replace("_", "+")
        if k in ("up", "shift+tab", "ctrl+p"):
            self._move(-1)
            return True
        if k in ("down", "tab", "ctrl+n"):
            self._move(1)
            return True
        if k in ("enter", "return"):
            self._choose_selected()
            return True
        if k == "escape":
            self.post_message(self.Decision("close"))
            return True
        for ck, _label, action in _ACTIONS:
            if k == ck:
                self.post_message(self.Decision(action))
                return True
        return False

    def on_key(self, event: events.Key) -> None:
        if not self.route_editor_key(event.key):
            return
        event.stop()
        event.prevent_default()

    def _move(self, delta: int) -> None:
        self._selected_idx = (self._selected_idx + delta) % len(_ACTIONS)
        self._render_options()

    def _choose_selected(self) -> None:
        if 0 <= self._selected_idx < len(_ACTIONS):
            self.post_message(self.Decision(_ACTIONS[self._selected_idx][2]))

    def _render_options(self) -> None:
        for row in self.query(_ActionRow):
            row.set_selected(row.index == self._selected_idx)
