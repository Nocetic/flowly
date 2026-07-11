"""Command permissions editor — set security/ask and prune the allowlist from
inside the TUI. The modal STAYS OPEN: each click applies the change live (via
an async callback that performs the RPC) and updates in place. It closes only
on the Close button or Esc.

The optional ``apply`` callback has signature
``async (action: dict) -> updated_policy | None`` where action is one of:
  * {"action": "set", "security": <mode>} / {"action": "set", "ask": <mode>}
  * {"action": "remove", "pattern": <pattern>}
It returns the authoritative policy after the change (or None on failure).
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Button, Label, ListItem, ListView, Static

SECURITY_CHOICES: list[tuple[str, str]] = [
    ("deny", "Deny all"),
    ("allowlist", "Allowlist only"),
    ("full", "Full access"),
]
ASK_CHOICES: list[tuple[str, str]] = [
    ("off", "Never ask"),
    ("on-miss", "Ask if not allowlisted"),
    ("always", "Always ask"),
]

ApplyFn = Callable[[dict[str, Any]], Awaitable[dict[str, Any] | None]]


def action_for_button(button_id: str | None) -> dict[str, str] | None:
    """Map a security/ask button id to a 'set' action, else None."""
    if not button_id:
        return None
    if button_id.startswith("sec-"):
        return {"action": "set", "security": button_id[len("sec-"):]}
    if button_id.startswith("ask-"):
        return {"action": "set", "ask": button_id[len("ask-"):]}
    return None


class PolicyPanel(Vertical):
    """Edit command permissions (stays open; applies changes live)."""

    can_focus = True

    class Dismissed(Message):
        pass

    DEFAULT_CSS = """
    PolicyPanel {
        width: 100%;
        max-width: 100%;
        height: auto;
        max-height: 24;
        border: none;
        background: transparent;
        padding: 0;
    }
    PolicyPanel .title { text-style: bold; color: #00a6c8; height: 1; }
    PolicyPanel .hint  { color: #83b8c2; text-style: italic; height: 1; margin-bottom: 1; }
    PolicyPanel .group { color: #e6fbff; text-style: bold; margin-top: 1; height: 1; }
    PolicyPanel #allowlist-box { height: auto; min-height: 3; max-height: 8; }
    PolicyPanel ListView { height: auto; max-height: 8; background: transparent; border: none; }
    PolicyPanel ListItem { background: transparent; padding: 0 1; }
    PolicyPanel Horizontal { height: auto; }
    PolicyPanel Button { margin-right: 1; }
    """

    BINDINGS = [
        ("escape", "close", "Close"),
        ("q", "close", "Close"),
        ("r", "remove_selected", "Remove"),
    ]

    def __init__(self, policy: dict[str, Any] | None, apply: ApplyFn | None = None) -> None:
        super().__init__()
        self._apply = apply
        self._set_state(policy)

    def _set_state(self, policy: dict[str, Any] | None) -> None:
        policy = policy or {}
        self._security = str(policy.get("security", "full"))
        self._ask = str(policy.get("ask", "off"))
        self._allowlist: list[dict[str, Any]] = list(policy.get("allowlist") or [])

    def compose(self) -> ComposeResult:
        yield Label("Command permissions", classes="title")
        yield Label(
            "Click to change · ↑/↓ select allowlist · R remove · Esc close",
            classes="hint",
        )

        yield Static("Security", classes="group")
        with Horizontal():
            for value, label in SECURITY_CHOICES:
                yield Button(
                    self._btn_label(label, value == self._security),
                    id=f"sec-{value}",
                    variant="success" if value == self._security else "default",
                )

        yield Static("Approval prompts", classes="group")
        with Horizontal():
            for value, label in ASK_CHOICES:
                yield Button(
                    self._btn_label(label, value == self._ask),
                    id=f"ask-{value}",
                    variant="success" if value == self._ask else "default",
                )

        yield Static(self._allowlist_heading(), id="allowlist-group", classes="group")
        yield Vertical(id="allowlist-box")

        with Horizontal():
            yield Button("Close (esc)", id="close-modal", variant="primary")

    async def on_mount(self) -> None:
        await self._rebuild_allowlist()
        self._focus_current_security()

    def on_focus(self) -> None:
        self._focus_current_security()

    def _focus_current_security(self) -> None:
        try:
            self.query_one(f"#sec-{self._security}", Button).focus()
        except Exception:
            pass

    # --- rendering helpers ----------------------------------------

    @staticmethod
    def _btn_label(label: str, selected: bool) -> str:
        return f"{'● ' if selected else ''}{label}"

    def _allowlist_heading(self) -> str:
        return f"Allowlist ({len(self._allowlist)})"

    def _refresh_choice_buttons(self) -> None:
        for value, label in SECURITY_CHOICES:
            b = self.query_one(f"#sec-{value}", Button)
            sel = value == self._security
            b.label = self._btn_label(label, sel)
            b.variant = "success" if sel else "default"
        for value, label in ASK_CHOICES:
            b = self.query_one(f"#ask-{value}", Button)
            sel = value == self._ask
            b.label = self._btn_label(label, sel)
            b.variant = "success" if sel else "default"

    async def _rebuild_allowlist(self) -> None:
        box = self.query_one("#allowlist-box", Vertical)
        await box.remove_children()
        self.query_one("#allowlist-group", Static).update(self._allowlist_heading())
        if not self._allowlist:
            await box.mount(Static("[dim]No allowlist entries.[/dim]"))
            return
        items: list[ListItem] = []
        for e in self._allowlist:
            pat = str(e.get("pattern", ""))
            cmd = e.get("command")
            extra = f"  [dim]{cmd}[/dim]" if cmd else ""
            items.append(ListItem(Static(f"{pat}{extra}")))
        await box.mount(ListView(*items))

    # --- interaction ----------------------------------------------

    async def _do(self, action: dict[str, Any]) -> None:
        """Apply an action via the callback and refresh in place (no close)."""
        if self._apply is None:
            return
        updated = await self._apply(action)
        if updated is None:
            return
        self._set_state(updated)
        self._refresh_choice_buttons()
        await self._rebuild_allowlist()

    @on(Button.Pressed)
    async def _on_button(self, event: Button.Pressed) -> None:
        if event.button.id == "close-modal":
            self.action_close()
            return
        action = action_for_button(event.button.id)
        if action is not None:
            await self._do(action)

    def _selected_pattern(self) -> str | None:
        try:
            lv = self.query_one(ListView)
        except Exception:
            return None
        if lv.index is None or not self._allowlist:
            return None
        return str(self._allowlist[lv.index].get("pattern", ""))

    async def action_remove_selected(self) -> None:
        pattern = self._selected_pattern()
        if pattern:
            await self._do({"action": "remove", "pattern": pattern})

    def action_close(self) -> None:
        self.post_message(self.Dismissed())


class PolicyModal(ModalScreen[None]):
    """Compatibility wrapper; the chat TUI mounts :class:`PolicyPanel`."""

    BINDINGS = PolicyPanel.BINDINGS

    DEFAULT_CSS = """
    PolicyModal { align: center middle; }
    PolicyModal > PolicyPanel {
        width: 90%;
        max-width: 120;
        padding: 1 2;
        border: thick #00a6c8;
        background: #050505;
    }
    """

    def __init__(self, policy: dict[str, Any] | None, apply: ApplyFn | None = None) -> None:
        super().__init__()
        policy = policy or {}
        self._security = str(policy.get("security", "full"))
        self._ask = str(policy.get("ask", "off"))
        self._allowlist: list[dict[str, Any]] = list(policy.get("allowlist") or [])
        self._policy = policy
        self._apply = apply

    def compose(self) -> ComposeResult:
        yield PolicyPanel(self._policy, self._apply)

    @on(PolicyPanel.Dismissed)
    def _on_dismissed(self, event: PolicyPanel.Dismissed) -> None:
        event.stop()
        self.dismiss(None)

    async def action_remove_selected(self) -> None:
        await self.query_one(PolicyPanel).action_remove_selected()

    def action_close(self) -> None:
        self.query_one(PolicyPanel).action_close()
