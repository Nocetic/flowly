"""Input composer with multiline, persistent history, slash palette, queue."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from dataclasses import field as dc_field
from pathlib import Path

from rich.markup import escape
from textual import events, on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.message import Message
from textual.widget import Widget
from textual.widgets import Input, Label, OptionList, Static, TextArea
from textual.widgets.option_list import Option

# TUI autocomplete palette — derived from the single command registry (every
# command not flagged gateway_only). Plugin/bundle/skill commands are merged in
# at runtime from the gateway's commands.list (see _refresh_command_palette).
# One source of truth: a command added to flowly/agent/slash_commands.py shows
# up here and in the gateway/desktop catalogue automatically.
from flowly.agent.slash_commands import cli_commands as _cli_commands
from flowly.tui.attachments import (
    FileDrop,
    detect_media_drop,
    detect_video_drop,
    format_attachment_labels,
    media_marker,
    render_message_with_attachments,
)
from flowly.tui.clipboard import save_clipboard_image
from flowly.tui.panes.memory_review import MemoryReviewPanel
from flowly.tui.panes.status_panel import SessionStatusPanel
from flowly.tui.panes.usage_panel import UsagePanel

LOCAL_SLASH_COMMANDS: list[tuple[str, str]] = [
    (f"/{_c.name}", _c.description) for _c in _cli_commands()
]


def _merge_slash_palette(items: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Merge dynamic slash commands without letting skills bury core commands."""
    merged: list[tuple[str, str]] = list(LOCAL_SLASH_COMMANDS)
    seen = {name.lower() for name, _desc in merged}
    for raw_name, desc in items:
        name = raw_name.strip()
        if not name:
            continue
        if not name.startswith("/"):
            name = f"/{name}"
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        merged.append((name, desc))
    return merged


def _filter_slash_palette(
    palette: list[tuple[str, str]],
    prefix: str,
) -> list[tuple[str, str]]:
    prefix_lower = prefix.lower()
    return [
        (name, desc)
        for name, desc in palette
        if name.lower().startswith(prefix_lower)
    ]


HISTORY_PATH = Path.home() / ".flowly" / "tui_history"
HISTORY_MAX = 500
COMPOSER_MIN_INPUT_LINES = 1
COMPOSER_MAX_INPUT_LINES = 10
SETUP_FIELD_ROWS = 5
SETUP_CHOICE_ROWS = 6
WORD_DELETE_LEFT_KEYS = {
    "alt+backspace",
    "alt+delete",
    "ctrl+backspace",
    "ctrl+w",
}
LINE_DELETE_LEFT_KEYS = {
    "ctrl+meta+backspace",
    "ctrl+meta+delete",
    "ctrl+meta+h",
    "ctrl+shift+backspace",
    "ctrl+super+backspace",
    "ctrl+super+delete",
    "ctrl+super+h",
    "ctrl+u",
    "meta+backspace",
    "meta+delete",
    "meta+shift+backspace",
    "meta+shift+delete",
    "shift+super+backspace",
    "shift+super+delete",
    "super+backspace",
    "super+delete",
}
WORD_DELETE_RIGHT_KEYS = {
    "ctrl+delete",
}


def _normalize_editor_key(key: str) -> str:
    """Normalize terminal/browser modifier aliases to Textual-style keys."""
    raw = key.lower().replace("-", "_")
    raw = raw.replace("delete_left", "backspace").replace("deleteleft", "backspace")
    raw = raw.replace("_", "+")
    parts = raw.split("+")
    aliases = {
        "cmd": "super",
        "command": "super",
        "del": "delete",
        "option": "alt",
        "return": "enter",
    }
    normalized = [aliases.get(part, part) for part in parts if part]
    if len(normalized) <= 1:
        return normalized[0] if normalized else ""
    *modifiers, base = normalized
    order = {"alt": 0, "ctrl": 1, "hyper": 2, "meta": 3, "shift": 4, "super": 5}
    unique_modifiers = sorted(set(modifiers), key=lambda item: (order.get(item, 99), item))
    return "+".join([*unique_modifiers, base])


@dataclass
class QueuedDraft:
    text: str
    attachments: list[Path]
    skill_notice: str | None = None


@dataclass
class ApprovalPromptRequest:
    request_id: str
    command: str
    reasons: list[str]
    session_key: str = ""
    expires_at: float | None = None
    cwd: str | None = None
    resolved_path: str | None = None
    # When False, "Always allow" would be a no-op (e.g. sending an email),
    # so the prompt hides that option — see visible_approval_options().
    supports_always: bool = True


@dataclass
class InlineSecretPromptRequest:
    """Small composer-anchored prompt for setup secrets.

    This is intentionally not a modal: it keeps setup flows near the prompt,
    matching the existing approval palette and avoiding a full-screen context
    switch for simple one-field credentials.
    """

    title: str
    label: str
    placeholder: str = ""
    help: str = ""
    value: str = ""
    required: bool = True
    password: bool = True


@dataclass
class InlineSetupField:
    key: str
    label: str
    kind: str = "text"
    placeholder: str = ""
    help: str = ""
    required: bool = False
    value: object = ""
    choices: list[tuple[str, str]] = dc_field(default_factory=list)


@dataclass
class InlineSetupPromptRequest:
    title: str
    subtitle: str = ""
    fields: list[InlineSetupField] = dc_field(default_factory=list)


class InlineSetupBack(Message):
    pass


class InlineSetupJump(Message):
    def __init__(self, index: int) -> None:
        super().__init__()
        self.index = index


class InlineSetupChoose(Message):
    def __init__(self, index: int) -> None:
        super().__init__()
        self.index = index


@dataclass(frozen=True)
class ApprovalOption:
    decision: str
    label: str
    keys: tuple[str, ...]


APPROVAL_OPTIONS: tuple[ApprovalOption, ...] = (
    ApprovalOption("allow-once", "Allow once", ("1", "a")),
    ApprovalOption("allow-always", "Always allow this command", ("2", "s")),
    ApprovalOption("deny", "Deny", ("3", "d", "escape", "ctrl+c")),
)

APPROVAL_DECISIONS = {option.decision for option in APPROVAL_OPTIONS}


def visible_approval_options(supports_always: bool) -> tuple[ApprovalOption, ...]:
    """The approval options to show for a request.

    Drops "Always allow" when remembering the decision would do nothing
    (``supports_always=False``) so the user is never offered a silent no-op.
    """
    if supports_always:
        return APPROVAL_OPTIONS
    return tuple(o for o in APPROVAL_OPTIONS if o.decision != "allow-always")
APPROVAL_CANCEL_KEYS = {
    key
    for option in APPROVAL_OPTIONS
    if option.decision == "deny"
    for key in option.keys
}
APPROVAL_SUBMIT_KEYS = {
    "enter",
    "return",
}
APPROVAL_PREV_KEYS = {
    "up",
}
APPROVAL_NEXT_KEYS = {
    "down",
}
APPROVAL_NAV_KEYS = [
    *APPROVAL_PREV_KEYS,
    *APPROVAL_NEXT_KEYS,
    *APPROVAL_SUBMIT_KEYS,
]


def approval_decision_for_key(key: str) -> str | None:
    """Return a direct approval decision for non-navigation shortcut keys."""
    normalized = key.lower().replace("_", "+")
    for option in APPROVAL_OPTIONS:
        if normalized in option.keys:
            return option.decision
    return None


class ApprovalOptionRow(Static):
    """One deterministic approval choice row."""

    def __init__(self, index: int, option: ApprovalOption) -> None:
        super().__init__("", classes="approval-option", markup=False)
        self.index = index
        self.option = option

    def set_selected(self, selected: bool) -> None:
        marker = "›" if selected else " "
        self.update(f"{marker} {self.index + 1}  {self.option.label}")
        self.set_class(selected, "selected")

    def on_click(self, event: events.Click) -> None:
        event.stop()
        self.post_message(ApprovalPrompt.Decision(self.option.decision))


class _Rule(Static):
    """Full-width horizontal `─` rule — composer separator."""

    def on_mount(self) -> None:
        self._draw()

    def on_resize(self) -> None:
        self._draw()

    def _draw(self) -> None:
        w = max(1, self.size.width or 80)
        self.update("─" * w)


class _Editor(TextArea):
    """TextArea with chat-style key handling.

    • Enter      → submit
    • Shift+Enter→ insert newline
    • ↑/↓        → history navigation when caret is on first/last line
                   and cursor at the boundary; otherwise default movement
    • Ctrl+A     → toggle subagent sidebar; keep the advertised app binding
                   working while the text editor has focus
    """

    BINDINGS = [
        Binding(
            "alt+backspace,alt+delete,ctrl+backspace,ctrl+w",
            "delete_word_left",
            show=False,
        ),
        Binding(
            "ctrl+shift+backspace,ctrl+u,meta+backspace,meta+delete,"
            "meta+shift+backspace,meta+shift+delete,"
            "shift+super+backspace,shift+super+delete,"
            "super+backspace,super+delete,ctrl+meta+h,ctrl+super+h",
            "delete_to_start_of_line",
            show=False,
        ),
        Binding("ctrl+delete", "delete_word_right", show=False),
    ]

    class Submit(Message):
        def __init__(self, text: str) -> None:
            super().__init__()
            self.text = text

    class HistoryPrev(Message):
        pass

    class HistoryNext(Message):
        pass

    class QueueCycle(Message):
        def __init__(self, direction: int) -> None:
            super().__init__()
            self.direction = direction

    class QueueDelete(Message):
        pass

    class QueueCancelEdit(Message):
        pass

    class PasteAttachment(Message):
        def __init__(self, text: str) -> None:
            super().__init__()
            self.text = text

    class PaletteCycle(Message):
        """Routed to Composer when palette is open and user hits ↑/↓."""
        def __init__(self, direction: int) -> None:
            super().__init__()
            self.direction = direction

    class PaletteSelect(Message):
        """Routed when user hits Tab on the active palette option (apply only)."""
        pass

    class PaletteEnter(Message):
        """Routed when user hits Enter while the palette is open.

        Composer decides what to do based on the highlighted option:
          • slash command  → apply pick + immediately submit
          • path           → apply pick + stay in editor (caret after path)
          • nothing picked → fall through to plain submit of raw text
        """
        pass

    @staticmethod
    def _is_insert_newline_key(key: str) -> bool:
        normalized = _normalize_editor_key(key)
        return normalized in {
            "shift+enter",
            "shift+return",
            "alt+enter",
            "alt+return",
            "option+enter",
            "option+return",
            "ctrl+j",
            "newline",
        }

    def on_key(self, event: events.Key) -> None:
        key = _normalize_editor_key(event.key)
        self._debug_key_event(event, key)
        # If the slash/path palette is open, arrow keys + tab steer the
        # option list instead of the editor or queue/history.
        #
        # NOTE: ``self.parent`` is the ``Horizontal#composer-input-row``
        # container, NOT the Composer itself, so we have to walk up the
        # ancestor chain to find the Composer and read its palette class.
        composer = next(
            (n for n in self.ancestors if isinstance(n, Composer)),
            None,
        )
        approval_open = composer is not None and composer.has_class("approval-open")
        if approval_open and composer is not None:
            composer.route_approval_key(key)
            composer.focus_approval()
            event.stop()
            event.prevent_default()
            return

        if composer is not None and composer.artifact_navigation_active():
            if composer.route_artifact_key(key):
                event.stop()
                event.prevent_default()
                return
            composer.cancel_artifact_navigation()

        if self._is_insert_newline_key(key):
            event.stop()
            event.prevent_default()
            self.insert("\n")
            return

        if key == "ctrl+a":
            event.stop()
            event.prevent_default()
            try:
                self.app.run_action("toggle_subagents")
            except Exception:
                pass
            return

        if key in WORD_DELETE_LEFT_KEYS:
            event.stop()
            event.prevent_default()
            self.action_delete_word_left()
            return

        if key in LINE_DELETE_LEFT_KEYS:
            event.stop()
            event.prevent_default()
            self.action_delete_to_start_of_line()
            return

        if key in WORD_DELETE_RIGHT_KEYS:
            event.stop()
            event.prevent_default()
            self.action_delete_word_right()
            return

        palette_open = composer is not None and composer.has_class("palette-open")
        if palette_open:
            if key == "up":
                event.stop()
                event.prevent_default()
                self.post_message(self.PaletteCycle(-1))
                return
            if key == "down":
                event.stop()
                event.prevent_default()
                self.post_message(self.PaletteCycle(1))
                return
            if key == "tab":
                # Tab → apply current palette pick (no submit). For slash
                # commands this lets the user add args before pressing
                # Enter; for paths it inserts the path and stays put.
                event.stop()
                event.prevent_default()
                self.post_message(self.PaletteSelect())
                return
            if key == "enter":
                # Enter while palette is open → let Composer decide
                # (slash = apply+submit, path = apply+stay, empty = raw submit).
                event.stop()
                event.prevent_default()
                self.post_message(self.PaletteEnter())
                return

        if key == "enter":
            event.stop()
            event.prevent_default()
            text = self.text.strip()
            has_attachments = bool(
                composer is not None and getattr(composer, "_attachments", [])
            )
            if text or has_attachments:
                self.post_message(self.Submit(text))
            return

        # Ctrl+X — delete the currently-editing queue item
        if key == "ctrl+x":
            event.stop()
            event.prevent_default()
            self.post_message(self.QueueDelete())
            return

        # Esc — cancel queue edit (no-op if not editing)
        if key == "escape":
            event.stop()
            event.prevent_default()
            self.post_message(self.QueueCancelEdit())
            return

        # ↑/↓ in single-line draft: queue first (cycleQueue), then history
        is_single_line = self.document.line_count <= 1

        if key == "up" and is_single_line:
            event.stop()
            event.prevent_default()
            self.post_message(self.QueueCycle(1))
            return

        if key == "down" and is_single_line:
            event.stop()
            event.prevent_default()
            self.post_message(self.QueueCycle(-1))
            return

        if key == "ctrl+v":
            composer = next((n for n in self.ancestors if isinstance(n, Composer)), None)
            if composer is not None and composer._try_attach_clipboard_image(notify=False):
                event.stop()
                event.prevent_default()
            return

        if key in ("alt+v", "escape,v"):
            event.stop()
            event.prevent_default()
            composer = next((n for n in self.ancestors if isinstance(n, Composer)), None)
            if composer is not None:
                composer._try_attach_clipboard_image(notify=True)
            return

    def _debug_key_event(self, event: events.Key, normalized: str) -> None:
        if os.environ.get("FLOWLY_TUI_DEBUG_KEYS") != "1":
            return
        try:
            path = Path.home() / ".flowly" / "tui_keys.log"
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(
                    f"key={event.key!r} normalized={normalized!r} "
                    f"char={getattr(event, 'character', None)!r}\n"
                )
        except Exception:
            pass

    def on_resize(self) -> None:
        composer = next((n for n in self.ancestors if isinstance(n, Composer)), None)
        if composer is not None:
            composer._resize_editor_for_content(self)

    def on_paste(self, event: events.Paste) -> None:
        if detect_media_drop(event.text):
            event.stop()
            event.prevent_default()
            self.post_message(self.PasteAttachment(event.text))
            return
        if not event.text.strip():
            composer = next((n for n in self.ancestors if isinstance(n, Composer)), None)
            if composer is not None and composer._try_attach_clipboard_image(notify=False):
                event.stop()
                event.prevent_default()


class ApprovalPrompt(Vertical):
    """Inline approval list that appears directly above the composer input."""

    can_focus = True

    class Decision(Message):
        def __init__(self, decision: str) -> None:
            super().__init__()
            self.decision = decision

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._request: ApprovalPromptRequest | None = None
        self._selected_idx = 0
        # Options shown for the current request — filtered per request so a
        # non-persistable action never offers "Always allow".
        self._options: tuple[ApprovalOption, ...] = APPROVAL_OPTIONS

    def compose(self) -> ComposeResult:
        yield Static("Action required", id="approval-title", markup=False)
        yield Static("", id="approval-command", markup=False)
        yield Static("", id="approval-meta", markup=True)
        for idx, option in enumerate(APPROVAL_OPTIONS):
            yield ApprovalOptionRow(idx, option)
        yield Static("↑/↓ choose · Enter select · Esc deny", id="approval-hint", markup=False)

    def on_mount(self) -> None:
        self._render_options()

    def set_request(self, request: ApprovalPromptRequest) -> None:
        self._request = request
        self._selected_idx = 0
        self._options = visible_approval_options(request.supports_always)
        self.query_one("#approval-command", Static).update(request.command or "(empty command)")
        self.query_one("#approval-meta", Static).update(self._meta_text(request))
        self._render_options()
        self.focus_options()

    def clear_request(self) -> None:
        self._request = None
        try:
            self.query_one("#approval-command", Static).update("")
            self.query_one("#approval-meta", Static).update("")
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
        normalized = key.lower().replace("_", "+")
        if normalized in APPROVAL_PREV_KEYS:
            self._move_selection(-1)
            return True
        if normalized in APPROVAL_NEXT_KEYS:
            self._move_selection(1)
            return True
        if normalized in APPROVAL_SUBMIT_KEYS:
            self._choose_selected()
            return True
        decision = approval_decision_for_key(normalized)
        if decision:
            self._choose(decision)
            return True
        return False

    def on_key(self, event: events.Key) -> None:
        if not self.route_editor_key(event.key):
            return
        event.stop()
        event.prevent_default()

    def _move_selection(self, delta: int) -> None:
        option_count = len(self._options)
        if option_count == 0:
            return
        self._selected_idx = (self._selected_idx + delta) % option_count
        self._render_options()

    def _choose_selected(self) -> None:
        if 0 <= self._selected_idx < len(self._options):
            self._choose(self._options[self._selected_idx].decision)

    def _choose(self, decision: str) -> None:
        # Only honour decisions that are actually offered for this request —
        # a hidden option's shortcut key must do nothing.
        if decision not in {o.decision for o in self._options}:
            return
        self.post_message(self.Decision(decision))

    def _render_options(self) -> None:
        visible = {o.decision for o in self._options}
        selected_decision = (
            self._options[self._selected_idx].decision
            if 0 <= self._selected_idx < len(self._options)
            else None
        )
        for row in self.query(ApprovalOptionRow):
            shown = row.option.decision in visible
            row.display = shown
            row.set_selected(shown and row.option.decision == selected_decision)

    @staticmethod
    def _meta_text(request: ApprovalPromptRequest) -> str:
        parts: list[str] = []
        if request.cwd:
            parts.append(f"[dim]{escape(request.cwd)}[/]")
        if request.reasons:
            parts.append(
                " · ".join(f"[#f2c94c]{escape(reason)}[/]" for reason in request.reasons)
            )
        if not parts:
            return "[dim]Review the command before continuing.[/dim]"
        return " · ".join(parts)


class InlineSecretPrompt(Vertical):
    """Compact one-field setup prompt rendered above the composer."""

    can_focus = True

    class Submitted(Message):
        def __init__(self, value: str) -> None:
            super().__init__()
            self.value = value

    class Cancelled(Message):
        pass

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._request: InlineSecretPromptRequest | None = None

    def compose(self) -> ComposeResult:
        from textual.containers import Horizontal

        yield Static("", id="secret-title", markup=False)
        yield Static("", id="secret-label", markup=False)
        with Horizontal(id="secret-input-row"):
            yield Static("›", id="secret-prefix", markup=False)
            yield _InlineSecretInput(id="secret-value", password=True)
        yield Static("", id="secret-error", markup=False)
        yield Static("", id="secret-hint", markup=False)

    def set_request(self, request: InlineSecretPromptRequest) -> None:
        self._request = request
        self.query_one("#secret-title", Static).update(request.title)
        self.query_one("#secret-label", Static).update(request.label)
        hint = request.help.strip()
        self.query_one("#secret-hint", Static).update(
            f"{hint + ' · ' if hint else ''}Enter save · Ctrl+U clear · Esc back"
        )
        self._set_error("")
        inp = self.query_one("#secret-value", Input)
        inp.value = request.value
        inp.placeholder = request.placeholder
        inp.password = request.password
        inp.focus()

    def clear_request(self) -> None:
        self._request = None
        try:
            self.query_one("#secret-title", Static).update("")
            self.query_one("#secret-label", Static).update("")
            self.query_one("#secret-hint", Static).update("")
            self.query_one("#secret-error", Static).update("")
            self.query_one("#secret-value", Input).value = ""
        except Exception:
            pass

    def focus_input(self) -> None:
        try:
            self.query_one("#secret-value", Input).focus()
        except Exception:
            pass

    @on(Input.Submitted, "#secret-value")
    def _submit(self, event: Input.Submitted) -> None:
        event.stop()
        value = self.query_one("#secret-value", Input).value
        if self._request is not None and self._request.required and not value.strip():
            self._set_error("Required field.")
            return
        self.post_message(self.Submitted(value))

    def action_cancel(self) -> None:
        self.post_message(self.Cancelled())

    def _set_error(self, message: str) -> None:
        try:
            err = self.query_one("#secret-error", Static)
            err.update(message)
            err.display = bool(message)
        except Exception:
            pass


class _InlineSecretInput(Input):
    """Input that lets the inline prompt own Esc/Ctrl+U."""

    def on_key(self, event: events.Key) -> None:
        if event.key == "escape":
            event.stop()
            event.prevent_default()
            self.post_message(InlineSecretPrompt.Cancelled())
            return
        if event.key == "ctrl+u":
            event.stop()
            event.prevent_default()
            self.value = ""
            return


class InlineSetupFieldRow(Static):
    """One compact row in the staged setup field list."""

    def __init__(self, slot: int) -> None:
        super().__init__("", classes="setup-field", markup=False)
        self.slot = slot
        self.field_index: int | None = None

    def set_row(self, index: int, text: str, *, selected: bool, complete: bool) -> None:
        self.field_index = index
        self.display = True
        self.update(text)
        self.set_class(selected, "selected")
        self.set_class(complete and not selected, "complete")
        self.set_class(not complete and not selected, "empty")

    def clear_row(self) -> None:
        self.field_index = None
        self.display = False
        self.update("")
        self.remove_class("selected")
        self.remove_class("complete")
        self.remove_class("empty")

    def on_click(self, event: events.Click) -> None:
        event.stop()
        if self.field_index is not None:
            self.post_message(InlineSetupJump(self.field_index))


class InlineSetupChoiceRow(Static):
    """One compact choice row in a select/bool setup field."""

    def __init__(self, slot: int) -> None:
        super().__init__("", classes="setup-choice", markup=False)
        self.slot = slot
        self.choice_index: int | None = None

    def set_row(self, index: int, text: str, *, selected: bool) -> None:
        self.choice_index = index
        self.display = True
        self.update(text)
        self.set_class(selected, "selected")

    def clear_row(self) -> None:
        self.choice_index = None
        self.display = False
        self.update("")
        self.remove_class("selected")

    def on_click(self, event: events.Click) -> None:
        event.stop()
        if self.choice_index is not None:
            self.post_message(InlineSetupChoose(self.choice_index))


class InlineSetupPrompt(Vertical):
    """Composer-anchored multi-field setup wizard."""

    can_focus = True

    class Submitted(Message):
        def __init__(self, values: dict[str, object]) -> None:
            super().__init__()
            self.values = values

    class Cancelled(Message):
        pass

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._request: InlineSetupPromptRequest | None = None
        self._idx = 0
        self._values: dict[str, object] = {}
        self._choice_idx = 0

    def compose(self) -> ComposeResult:
        from textual.containers import Horizontal

        yield Static("", id="setup-title", markup=False)
        yield Static("", id="setup-subtitle", markup=False)
        yield Static("", id="setup-progress", markup=False)
        for i in range(SETUP_FIELD_ROWS):
            yield InlineSetupFieldRow(i)
        yield Static("", id="setup-label", markup=False)
        with Horizontal(id="setup-input-row"):
            yield Static("›", id="setup-prefix", markup=False)
            yield _InlineSetupInput(id="setup-value")
        for i in range(SETUP_CHOICE_ROWS):
            yield InlineSetupChoiceRow(i)
        yield Static("", id="setup-error", markup=False)
        yield Static("", id="setup-hint", markup=False)

    def set_request(self, request: InlineSetupPromptRequest) -> None:
        self._request = request
        self._idx = 0
        self._values = {field.key: field.value for field in request.fields}
        self._choice_idx = 0
        self.query_one("#setup-title", Static).update(request.title)
        self.query_one("#setup-subtitle", Static).update(request.subtitle)
        self.query_one("#setup-subtitle", Static).display = bool(request.subtitle)
        self._render_current()

    def clear_request(self) -> None:
        self._request = None
        self._idx = 0
        self._values = {}
        self._choice_idx = 0
        try:
            for wid in (
                "setup-title",
                "setup-subtitle",
                "setup-progress",
                "setup-label",
                "setup-error",
                "setup-hint",
            ):
                self.query_one(f"#{wid}", Static).update("")
            self.query_one("#setup-value", Input).value = ""
            for row in self.query(InlineSetupFieldRow):
                row.clear_row()
            for row in self.query(InlineSetupChoiceRow):
                row.clear_row()
        except Exception:
            pass

    def focus_current(self) -> None:
        field = self._current_field()
        if field is None:
            return
        if field.kind in {"select", "bool"}:
            try:
                self.focus()
            except Exception:
                pass
        else:
            try:
                self.query_one("#setup-value", Input).focus()
            except Exception:
                pass

    def on_key(self, event: events.Key) -> None:
        field = self._current_field()
        if field is None or field.kind not in {"select", "bool"}:
            return
        choices = self._choices_for(field)
        if event.key == "escape":
            event.stop()
            event.prevent_default()
            self._back_or_cancel()
            return
        if event.key == "up":
            event.stop()
            event.prevent_default()
            self._choice_idx = (self._choice_idx - 1) % max(1, len(choices))
            self._render_choices(field)
            return
        if event.key == "down":
            event.stop()
            event.prevent_default()
            self._choice_idx = (self._choice_idx + 1) % max(1, len(choices))
            self._render_choices(field)
            return
        if event.key in {"enter", "return"}:
            event.stop()
            event.prevent_default()
            self._submit_choice(field)
            return
        char = getattr(event, "character", None)
        try:
            n = 10 if char == "0" else int(char or "")
        except (TypeError, ValueError):
            return
        if 1 <= n <= len(choices):
            event.stop()
            event.prevent_default()
            self._choice_idx = n - 1
            self._submit_choice(field)

    @on(Input.Submitted, "#setup-value")
    def _submit_input(self, event: Input.Submitted) -> None:
        event.stop()
        field = self._current_field()
        if field is None:
            return
        raw = self.query_one("#setup-value", Input).value
        if field.required and not raw.strip():
            self._set_error("Required field.")
            return
        if field.kind == "int":
            raw = raw.strip()
            if raw:
                try:
                    value: object = int(raw)
                except ValueError:
                    self._set_error("Enter a whole number.")
                    return
            else:
                value = 0
        elif field.kind == "multi":
            value = [s.strip() for s in raw.split(",") if s.strip()]
        else:
            value = raw
        self._values[field.key] = value
        self._advance_or_submit()

    @on(InlineSetupBack)
    def _on_back(self, event: InlineSetupBack) -> None:
        event.stop()
        self._back_or_cancel()

    @on(InlineSetupJump)
    def _on_jump(self, event: InlineSetupJump) -> None:
        event.stop()
        if self._request is None:
            return
        if 0 <= event.index < len(self._request.fields):
            missing = self._first_missing_required_before(event.index)
            if missing is not None:
                self._idx = missing
                self._render_current()
                self._set_error("Required field.")
                return
            self._idx = event.index
            self._render_current()

    @on(InlineSetupChoose)
    def _on_choose(self, event: InlineSetupChoose) -> None:
        event.stop()
        field = self._current_field()
        if field is None or field.kind not in {"select", "bool"}:
            return
        choices = self._choices_for(field)
        if 0 <= event.index < len(choices):
            self._choice_idx = event.index
            self._submit_choice(field)

    def action_back_or_cancel(self) -> None:
        self._back_or_cancel()

    def _current_field(self) -> InlineSetupField | None:
        if self._request is None:
            return None
        if not (0 <= self._idx < len(self._request.fields)):
            return None
        return self._request.fields[self._idx]

    def _render_current(self) -> None:
        field = self._current_field()
        if field is None or self._request is None:
            return
        total = len(self._request.fields)
        self.query_one("#setup-progress", Static).update(
            f"{self._idx + 1}/{total} · {field.label}"
        )
        self._render_field_rows()
        required = " *" if field.required else ""
        self.query_one("#setup-label", Static).update(f"{field.label}{required}")
        self._set_error("")
        hint = field.help.strip()
        if field.kind in {"select", "bool"}:
            self.query_one("#setup-input-row").display = False
            self._choice_idx = self._choice_index_for(field)
            self._render_choices(field)
            action = "↑/↓ choose · Enter select · Esc back"
            self.focus_current()
        else:
            self.query_one("#setup-input-row").display = True
            for row in self.query(InlineSetupChoiceRow):
                row.clear_row()
            inp = self.query_one("#setup-value", Input)
            value = self._values.get(field.key, "")
            if isinstance(value, list):
                inp.value = ", ".join(str(v) for v in value)
            else:
                inp.value = "" if value is None else str(value)
            inp.placeholder = field.placeholder
            inp.password = field.kind == "password"
            action = (
                "Enter next · Esc back · Ctrl+U clear"
                if self._idx < total - 1
                else "Enter save · Esc back · Ctrl+U clear"
            )
            self.focus_current()
        self.query_one("#setup-hint", Static).update(
            f"{hint + ' · ' if hint else ''}{action}"
        )

    def _render_field_rows(self) -> None:
        if self._request is None:
            return
        fields = self._request.fields
        total = len(fields)
        start = self._window_start(total, self._idx, SETUP_FIELD_ROWS)
        rows = list(self.query(InlineSetupFieldRow))
        for slot, row in enumerate(rows):
            idx = start + slot
            if idx >= total:
                row.clear_row()
                continue
            field = fields[idx]
            selected = idx == self._idx
            complete = self._field_has_value(field, self._values.get(field.key))
            marker = "›" if selected else "✓" if complete else " "
            required = "*" if field.required else " "
            label = self._ellipsize(field.label, 28)
            summary = self._ellipsize(
                self._field_summary(field, self._values.get(field.key)),
                34,
            )
            row.set_row(
                idx,
                f"{marker} {idx + 1}/{total} {required} {label}  {summary}",
                selected=selected,
                complete=complete,
            )

    def _render_choices(self, field: InlineSetupField) -> None:
        choices = self._choices_for(field)
        start = self._window_start(len(choices), self._choice_idx, SETUP_CHOICE_ROWS)
        for slot, row in enumerate(self.query(InlineSetupChoiceRow)):
            idx = start + slot
            if idx >= len(choices):
                row.clear_row()
                continue
            _value, label = choices[idx]
            selected = idx == self._choice_idx
            marker = "›" if selected else " "
            row.set_row(idx, f"{marker} {idx + 1}  {label}", selected=selected)

    def _choices_for(self, field: InlineSetupField) -> list[tuple[str, str]]:
        if field.kind == "bool":
            return [("true", "on"), ("false", "off")]
        return field.choices or [("", "(none)")]

    @staticmethod
    def _window_start(total: int, selected: int, size: int) -> int:
        if total <= size:
            return 0
        half = max(1, size // 2)
        return max(0, min(selected - half, total - size))

    @staticmethod
    def _ellipsize(text: object, width: int) -> str:
        value = str(text or "")
        if len(value) <= width:
            return value
        if width <= 3:
            return value[:width]
        return value[: width - 3] + "..."

    def _field_summary(self, field: InlineSetupField, value: object) -> str:
        if not self._field_has_value(field, value):
            return "empty"
        if field.kind == "password":
            return "set"
        if field.kind == "bool":
            return "on" if bool(value) else "off"
        if field.kind == "select":
            current = str(value or "")
            for option_value, label in self._choices_for(field):
                if str(option_value) == current:
                    return label
            return current
        if field.kind == "multi" and isinstance(value, list):
            return ", ".join(str(v) for v in value if str(v).strip()) or "empty"
        return str(value)

    @staticmethod
    def _field_has_value(field: InlineSetupField, value: object) -> bool:
        if field.kind == "bool":
            return value is not None
        if isinstance(value, list):
            return bool(value)
        return value is not None and str(value).strip() != ""

    def _first_missing_required_before(self, end: int) -> int | None:
        if self._request is None:
            return None
        for idx, field in enumerate(self._request.fields[:end]):
            if field.required and not self._field_has_value(
                field,
                self._values.get(field.key),
            ):
                return idx
        return None

    def _choice_index_for(self, field: InlineSetupField) -> int:
        current = self._values.get(field.key, field.value)
        current_str = "true" if current is True else "false" if current is False else str(current or "")
        choices = self._choices_for(field)
        for i, (value, _label) in enumerate(choices):
            if str(value) == current_str:
                return i
        return 0

    def _submit_choice(self, field: InlineSetupField) -> None:
        choices = self._choices_for(field)
        if not choices:
            return
        value = choices[max(0, min(self._choice_idx, len(choices) - 1))][0]
        self._values[field.key] = value == "true" if field.kind == "bool" else value
        self._advance_or_submit()

    def _advance_or_submit(self) -> None:
        if self._request is None:
            return
        if self._idx >= len(self._request.fields) - 1:
            missing = self._first_missing_required_before(len(self._request.fields))
            if missing is not None:
                self._idx = missing
                self._render_current()
                self._set_error("Required field.")
                return
            self.post_message(self.Submitted(dict(self._values)))
            return
        self._idx += 1
        self._render_current()

    def _back_or_cancel(self) -> None:
        if self._idx > 0:
            self._idx -= 1
            self._render_current()
            return
        self.post_message(self.Cancelled())

    def _set_error(self, message: str) -> None:
        try:
            err = self.query_one("#setup-error", Static)
            err.update(message)
            err.display = bool(message)
        except Exception:
            pass


class _InlineSetupInput(Input):
    def on_key(self, event: events.Key) -> None:
        if event.key == "escape":
            event.stop()
            event.prevent_default()
            self.post_message(InlineSetupBack())
            return
        if event.key == "ctrl+u":
            event.stop()
            event.prevent_default()
            self.value = ""
            return


class Composer(Vertical):
    """Editor + floating slash palette."""

    DEFAULT_CSS = """
    Composer {
        dock: bottom;
        height: auto;
        min-height: 5;
        max-height: 24;
        background: #000000;
        layers: base overlay;
    }
    Composer.usage-open {
        max-height: 32;
    }
    Composer > .composer-rule {
        height: 1;
        background: #000000;
        color: #0f4c5c;
        padding: 0;
        margin: 0;
    }
    Composer > #composer-input-row {
        height: auto;
        min-height: 1;
        layout: horizontal;
        background: #000000;
        padding: 0;
        margin: 0;
    }
    Composer > #composer-input-row > #composer-prompt {
        width: 2;
        height: auto;
        content-align: left top;
        color: #00a6c8;
        background: #000000;
        text-style: bold;
        padding: 0 0 0 1;
    }
    Composer > #composer-input-row > _Editor {
        width: 1fr;
        height: auto;
        min-height: 1;
        max-height: 10;
        border: none;
        padding: 0 1;
        background: #000000;
        color: #e6fbff;
    }
    Composer > #composer-hint {
        height: 1;
        padding: 0 2;
        margin: 0;
        background: #000000;
        color: #83b8c2;
    }
    Composer.artifact-nav > #composer-hint {
        background: #00a6c8;
        color: #001318;
    }
    Composer.approval-open > #composer-input-row,
    Composer.secret-open > #composer-input-row,
    Composer.setup-open > #composer-input-row,
    Composer.review-open > #composer-input-row,
    Composer.usage-open > #composer-input-row,
    Composer.status-open > #composer-input-row,
    Composer.picker-inline-open > #composer-input-row,
    Composer.approval-open > #composer-hint,
    Composer.secret-open > #composer-hint,
    Composer.setup-open > #composer-hint,
    Composer.review-open > #composer-hint,
    Composer.usage-open > #composer-hint,
    Composer.status-open > #composer-hint,
    Composer.picker-inline-open > #composer-hint {
        display: none;
    }
    Composer > #composer-picker {
        display: none;
        height: auto;
        max-height: 24;
        padding: 0 2;
        margin: 0;
        background: transparent;
    }
    Composer.picker-floating-open > #composer-picker {
        overlay: screen;
        layer: overlay;
        offset-y: -100%;
        width: 100%;
        padding: 0 1;
        align: left top;
    }
    Composer.picker-open > #composer-picker {
        display: block;
    }
    Composer.picker-inline-open > #composer-picker {
        background: #000000;
    }
    Composer > #composer-picker > ProviderPickerPanel,
    Composer > #composer-picker > ModelPickerPanel,
    Composer > #composer-picker > IntegrationsPanel,
    Composer > #composer-picker > LoginPanel,
    Composer > #composer-picker > PluginsPanel {
        height: auto;
        max-height: 24;
    }
    Composer.picker-inline-open > #composer-picker > ProviderPickerPanel,
    Composer.picker-inline-open > #composer-picker > ModelPickerPanel,
    Composer.picker-inline-open > #composer-picker > IntegrationsPanel,
    Composer.picker-inline-open > #composer-picker > LoginPanel,
    Composer.picker-inline-open > #composer-picker > PluginsPanel {
        width: 100%;
        max-width: 100%;
        border: none;
        padding: 0;
        background: transparent;
    }
    Composer > #composer-usage {
        display: none;
        height: auto;
        max-height: 18;
        padding: 0 2;
        margin: 0;
        background: #000000;
    }
    Composer.usage-open > #composer-usage {
        display: block;
    }
    Composer > #composer-usage > #usage-scroll {
        height: auto;
        max-height: 16;
    }
    Composer > #composer-usage > #usage-hint {
        height: 1;
        color: #83b8c2;
    }
    Composer > #composer-status-panel {
        display: none;
        height: auto;
        padding: 0 2;
        margin: 0;
        background: #000000;
    }
    Composer.status-open > #composer-status-panel {
        display: block;
    }
    Composer > #composer-status-panel > #status-panel-title {
        height: 1;
        text-style: bold;
        color: #00a6c8;
    }
    Composer > #composer-status-panel > #status-panel-session,
    Composer > #composer-status-panel > #status-panel-provider,
    Composer > #composer-status-panel > #status-panel-model,
    Composer > #composer-status-panel > #status-panel-state,
    Composer > #composer-status-panel > #status-panel-usage,
    Composer > #composer-status-panel > #status-panel-hint {
        height: 1;
        color: #83b8c2;
    }
    Composer > #composer-attachments {
        height: 1;
        padding: 0 2;
        margin: 0;
        background: #000000;
        color: #00a6c8;
        display: none;
    }
    Composer > #composer-attachments.has-attachments {
        display: block;
    }
    Composer > #composer-approval {
        display: none;
        height: auto;
        padding: 1 2;
        margin: 0;
    }
    Composer.approval-open > #composer-approval {
        display: block;
    }
    Composer > #composer-approval > #approval-title {
        height: 1;
        text-style: bold;
    }
    Composer > #composer-approval > #approval-command {
        height: auto;
        max-height: 1;
        text-style: bold;
        margin: 0;
    }
    Composer > #composer-approval > #approval-meta {
        height: auto;
        max-height: 1;
    }
    Composer > #composer-approval > .approval-option {
        height: 1;
        margin: 0;
    }
    Composer > #composer-approval > #approval-hint {
        height: 1;
    }
    Composer > #composer-review {
        display: none;
        height: auto;
        padding: 1 2;
        margin: 0;
    }
    Composer.review-open > #composer-review {
        display: block;
    }
    Composer > #composer-review > #review-title {
        height: 1;
        text-style: bold;
        color: #00a6c8;
    }
    Composer > #composer-review > #review-meta {
        height: 1;
    }
    Composer > #composer-review > #review-text {
        height: auto;
        max-height: 3;
        margin: 0 0 1 0;
    }
    Composer > #composer-review > .review-option {
        height: 1;
        margin: 0;
        color: #83b8c2;
    }
    Composer > #composer-review > .review-option.selected {
        color: #e6fbff;
        background: #050505;
        text-style: bold;
    }
    Composer > #composer-review > #review-hint {
        height: 1;
        color: #83b8c2;
    }
    Composer > #composer-secret {
        display: none;
        height: auto;
        padding: 0 2;
        margin: 0;
        background: #000000;
    }
    Composer.secret-open > #composer-secret {
        display: block;
    }
    Composer > #composer-secret > #secret-title {
        height: 1;
        text-style: bold;
        color: #00a6c8;
    }
    Composer > #composer-secret > #secret-label {
        height: 1;
        color: #e6fbff;
    }
    Composer > #composer-secret > #secret-input-row {
        height: 1;
        layout: horizontal;
        margin: 0;
        margin-bottom: 1;
    }
    Composer > #composer-secret > #secret-input-row > #secret-prefix {
        width: 2;
        height: 1;
        color: #00a6c8;
        text-style: bold;
    }
    Composer > #composer-secret > #secret-input-row > #secret-value {
        width: 1fr;
        height: 1;
        border: none;
        padding: 0 1;
        background: #050505;
        color: #e6fbff;
    }
    Composer > #composer-secret > #secret-error {
        height: 1;
        color: #ff6b6b;
        display: none;
    }
    Composer > #composer-secret > #secret-hint {
        height: 1;
        color: #83b8c2;
    }
    Composer > #composer-setup {
        display: none;
        height: auto;
        padding: 0 2;
        margin: 0;
        background: #000000;
    }
    Composer.setup-open > #composer-setup {
        display: block;
    }
    Composer > #composer-setup > #setup-title {
        height: 1;
        text-style: bold;
        color: #00a6c8;
    }
    Composer > #composer-setup > #setup-subtitle,
    Composer > #composer-setup > #setup-progress {
        height: 1;
        color: #83b8c2;
    }
    Composer > #composer-setup > #setup-label {
        height: auto;
        color: #e6fbff;
    }
    Composer > #composer-setup > .setup-field {
        height: 1;
        color: #83b8c2;
        display: none;
    }
    Composer > #composer-setup > .setup-field.complete {
        color: #6d9ca5;
    }
    Composer > #composer-setup > .setup-field.selected {
        color: #e6fbff;
        background: #050505;
        text-style: bold;
    }
    Composer > #composer-setup > #setup-input-row {
        height: 1;
        layout: horizontal;
        margin: 0;
        margin-bottom: 1;
    }
    Composer > #composer-setup > #setup-input-row > #setup-prefix {
        width: 2;
        height: 1;
        color: #00a6c8;
        text-style: bold;
    }
    Composer > #composer-setup > #setup-input-row > #setup-value {
        width: 1fr;
        height: 1;
        border: none;
        padding: 0 1;
        background: #050505;
        color: #e6fbff;
    }
    Composer > #composer-setup > .setup-choice {
        height: 1;
        color: #83b8c2;
        display: none;
    }
    Composer > #composer-setup > .setup-choice.selected {
        color: #00a6c8;
        background: #000000;
        text-style: bold;
    }
    Composer > #composer-setup > #setup-error {
        height: 1;
        color: #ff6b6b;
        display: none;
    }
    Composer > #composer-setup > #setup-hint {
        height: 1;
        color: #83b8c2;
    }
    Composer > OptionList {
        max-height: 8;
        border: none;
        background: #050505;
        color: #e6fbff;
        display: none;
    }
    Composer.palette-open > OptionList {
        display: block;
    }
    Composer:disabled > #composer-input-row > #composer-prompt,
    Composer:disabled > #composer-input-row > _Editor,
    Composer:disabled > #composer-hint {
        color: #4b7f8a;
    }
    """

    BINDINGS = [
        Binding("ctrl+e", "open_editor", "$EDITOR", show=False),
    ]

    class Submitted(Message):
        def __init__(self, text: str, attachments: list[Path] | None = None) -> None:
            super().__init__()
            self.text = text
            self.attachments = attachments or []

    class Slash(Message):
        def __init__(self, command: str) -> None:
            super().__init__()
            self.command = command

    class Shell(Message):
        def __init__(self, command: str) -> None:
            super().__init__()
            self.command = command

    class ArtifactOpen(Message):
        def __init__(self, artifact: dict[str, object]) -> None:
            super().__init__()
            self.artifact = artifact

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._palette: list[tuple[str, str]] = list(LOCAL_SLASH_COMMANDS)
        self._history: list[str] = []
        self._history_idx: int | None = None  # None = at fresh prompt
        self._draft_when_browsing: str = ""
        self._attachments: list[Path] = []
        self._queue: list[QueuedDraft] = []
        self._queue_edit_idx: int | None = None  # which queue item is being edited
        self._draft_before_queue_edit: str = ""
        self._draft_before_queue_edit_attachments: list[Path] = []
        self._artifacts: list[dict[str, object]] = []
        self._artifact_idx: int | None = None
        # Connection / activity state used to choose the hint text shown
        # below the editor. Driven from the app via :meth:`set_state`.
        # ``"idle"``, ``"busy"``, ``"reconnecting"``, ``"offline"``.
        self._state: str = "idle"

    def compose(self) -> ComposeResult:
        from textual.containers import Horizontal

        from flowly.tui.panes.queued_messages import QueuedMessages
        from flowly.tui.panes.status import StatusBar

        yield QueuedMessages(id="composer-queue")
        yield StatusBar(id="status")
        yield _Rule(classes="composer-rule", id="composer-rule-top")
        yield Static("", id="composer-attachments", markup=False)
        yield ApprovalPrompt(id="composer-approval")
        yield InlineSecretPrompt(id="composer-secret")
        yield InlineSetupPrompt(id="composer-setup")
        yield MemoryReviewPanel(id="composer-review")
        yield UsagePanel(id="composer-usage")
        yield SessionStatusPanel(id="composer-status-panel")
        yield Vertical(id="composer-picker")
        with Horizontal(id="composer-input-row"):
            yield Label("❯", id="composer-prompt", markup=False)
            editor = _Editor(id="composer-input", show_line_numbers=False)
            editor.tab_behavior = "focus"
            self._resize_editor_for_content(editor)
            yield editor
        yield _Rule(classes="composer-rule", id="composer-rule-bottom")
        # Single-line dim hint immediately under the editor. Text is
        # driven by (state, queue_size) via :meth:`_refresh_hint`. Lives
        # here (not in the StatusBar) so it sits where the user's eyes
        # already are when they're typing.
        yield Static("", id="composer-hint", markup=True)
        yield OptionList(id="composer-palette")

    def on_mount(self) -> None:
        self._load_history()
        self._refresh_palette("")
        self._refresh_hint()

    def on_resize(self) -> None:
        self._refresh_hint()

    # --- hint line ----------------------------------------------------

    def set_state(self, state: str) -> None:
        """App pushes connection/activity state changes here.

        Recognised values: ``idle``, ``busy``, ``reconnecting``,
        ``offline``. Unknown values fall back to ``idle``-style
        rendering. Safe to call from any thread the app touches widgets
        from — the underlying Static update is synchronous.
        """
        if state not in ("idle", "busy", "reconnecting", "offline"):
            state = "idle"
        if state == self._state:
            return
        if state != "idle":
            self._artifact_idx = None
        self._state = state
        self._refresh_hint()

    def _refresh_hint(self) -> None:
        """Render the state-aware hint line under the editor."""
        try:
            hint_widget = self.query_one("#composer-hint", Static)
        except Exception:
            return  # not mounted yet
        n_queued = len(self._queue)

        # Flip the hint row onto the accent background while the artifact
        # selector owns the arrow keys, so "where am I?" is answered by
        # color, not just text.
        nav_active = self._artifact_idx is not None and bool(self._artifacts)
        self.set_class(nav_active, "artifact-nav")

        if nav_active:
            text = self._artifact_hint(active=True)
        elif self._state == "offline":
            text = "[b]offline[/] · gateway unreachable — run [b]flowly gateway[/]"
        elif self._state == "reconnecting":
            text = "[b]reconnecting…[/] · messages typed here will queue locally"
        elif self._state == "busy":
            if n_queued > 0:
                text = (
                    f"[b]thinking…[/] · [b]{n_queued}[/] queued · "
                    "type to enqueue more · [b]Ctrl+C[/] aborts this turn"
                )
            else:
                text = (
                    "[b]thinking…[/] · type to queue "
                    "(sends after current turn) · [b]Ctrl+C[/] aborts"
                )
        else:  # idle (default)
            if n_queued > 0:
                text = (
                    f"[b]{n_queued}[/] queued · ↑/↓ to edit · "
                    "Enter to replace · [dim]/ for commands · ! for shell · F1 help[/]"
                )
            elif self._artifacts:
                text = self._artifact_hint(active=False)
            else:
                text = (
                    "[dim]Type a message · / for commands · ! for shell · "
                    "Shift+Enter newline · F1 help[/]"
                )
        hint_widget.update(text)

    def _artifact_hint(self, *, active: bool) -> str:
        count = len(self._artifacts)
        width = max(30, self.size.width or 80)

        if not active:
            noun = "artifact" if count == 1 else "artifacts"
            text = f"[#00a6c8][b]◆ {count} {noun}[/b][/] · [b]↓[/] open"
            if width >= 80:
                text += " · [dim]/ commands · F1 help[/]"
            return text

        # Selection mode: the hint row flips to the accent background (the
        # ``artifact-nav`` class), so the text stays plain and just names
        # the mode, the pick, and the keys.
        idx = max(0, min(self._artifact_idx or 0, count - 1))
        artifact = self._artifacts[idx]
        title = " ".join(
            str(artifact.get("title") or artifact.get("id") or "artifact").split()
        )
        keys = "↑/↓ · Enter open · Esc" if width >= 76 else "↑/↓ · Enter · Esc"
        limit = max(10, width - len(keys) - 22)
        if len(title) > limit:
            title = title[: max(1, limit - 1)] + "…"
        return f"[b]◆ artifacts {idx + 1}/{count}[/b] · {escape(title)} · {keys}"

    @staticmethod
    def _input_height_for_line_count(line_count: int) -> int:
        return max(
            COMPOSER_MIN_INPUT_LINES,
            min(COMPOSER_MAX_INPUT_LINES, line_count),
        )

    @staticmethod
    def _wrapped_line_count(editor: _Editor) -> int:
        """Count visual lines, not just newline-delimited document lines."""
        logical_lines = editor.text.split("\n") or [""]
        wrap_width = 0
        try:
            wrap_width = int(editor.wrap_width or 0)
        except Exception:
            wrap_width = 0
        if wrap_width <= 0:
            try:
                wrap_width = int(editor.content_size.width or 0)
            except Exception:
                wrap_width = 0
        if wrap_width <= 0:
            try:
                wrap_width = int(editor.size.width or 0)
            except Exception:
                wrap_width = 0
        if wrap_width <= 0:
            return max(1, len(logical_lines))

        try:
            wrapped = editor.wrapped_document
            wrapped.wrap(wrap_width, tab_width=editor.indent_width)
            wrapped_height = int(getattr(wrapped, "height", 0) or 0)
            if wrapped_height > 0:
                return wrapped_height
        except Exception:
            pass

        tab_width = max(1, int(getattr(editor, "indent_width", 4) or 4))
        visual_lines = 0
        for line in logical_lines:
            width = len(line.expandtabs(tab_width))
            visual_lines += max(1, (width + wrap_width - 1) // wrap_width)
        return visual_lines

    def _resize_editor_for_content(self, editor: _Editor) -> None:
        editor.styles.height = self._input_height_for_line_count(
            self._wrapped_line_count(editor)
        )

    @staticmethod
    def _last_completion_token(text: str) -> str:
        parts = text.rsplit(maxsplit=1)
        return parts[-1] if parts else ""

    # --- public API -----------------------------------------------

    def set_palette(self, items: list[tuple[str, str]]) -> None:
        """Replace the slash palette source list and refresh if visible."""
        self._palette = _merge_slash_palette(items)
        editor = self.query_one("#composer-input", _Editor)
        current = editor.text
        # Refresh whether or not the palette is currently visible — if
        # the user already opened it before commands.list resolved, this
        # is the only chance to surface the new entries without a
        # keystroke. The empty-prefix case keeps the unfiltered list ready.
        prefix = current if (current.startswith("/") and "\n" not in current) else ""
        self._refresh_palette(prefix)

    def set_artifacts(self, artifacts: list[dict[str, object]]) -> None:
        """Replace the current session's lightweight artifact summaries."""
        selected_id = None
        if self._artifact_idx is not None and self._artifacts:
            selected_id = self._artifacts[self._artifact_idx].get("id")

        def _updated_at(item: dict[str, object]) -> float:
            try:
                return float(item.get("updated_at") or 0)
            except (TypeError, ValueError):
                return 0

        self._artifacts = sorted(
            (dict(item) for item in artifacts if item.get("id")),
            key=_updated_at,
            reverse=True,
        )
        if not self._artifacts:
            self._artifact_idx = None
        elif selected_id is not None:
            self._artifact_idx = next(
                (
                    i
                    for i, item in enumerate(self._artifacts)
                    if item.get("id") == selected_id
                ),
                0,
            )
        self._refresh_hint()

    def session_artifacts(self) -> list[dict[str, object]]:
        """Current chat's artifact summaries, most recently updated first."""
        return [dict(item) for item in self._artifacts]

    def upsert_artifact(self, artifact: dict[str, object]) -> None:
        artifact_id = artifact.get("id")
        if not artifact_id:
            return
        items = [
            item for item in self._artifacts if item.get("id") != artifact_id
        ]
        items.append(dict(artifact))
        self.set_artifacts(items)

    def remove_artifact(self, artifact_id: str) -> None:
        self.set_artifacts(
            [item for item in self._artifacts if item.get("id") != artifact_id]
        )

    def artifact_navigation_active(self) -> bool:
        return self._artifact_idx is not None

    def enter_artifact_navigation(self) -> bool:
        if self._state != "idle" or not self._artifacts:
            return False
        self._artifact_idx = 0
        self._refresh_hint()
        return True

    def cancel_artifact_navigation(self) -> None:
        if self._artifact_idx is None:
            return
        self._artifact_idx = None
        self._refresh_hint()

    def route_artifact_key(self, key: str) -> bool:
        if self._artifact_idx is None or not self._artifacts:
            return False
        if key == "down":
            self._artifact_idx = (self._artifact_idx + 1) % len(self._artifacts)
            self._refresh_hint()
            return True
        if key == "up":
            self._artifact_idx = (self._artifact_idx - 1) % len(self._artifacts)
            self._refresh_hint()
            return True
        if key == "enter":
            artifact = dict(self._artifacts[self._artifact_idx])
            self._artifact_idx = None
            self._refresh_hint()
            self.post_message(self.ArtifactOpen(artifact))
            return True
        if key == "escape":
            self.cancel_artifact_navigation()
            return True
        return False

    def focus_input(self) -> None:
        self.query_one("#composer-input", _Editor).focus()

    def _remove_picker_classes(self) -> None:
        self.remove_class("picker-open")
        self.remove_class("picker-floating-open")
        self.remove_class("picker-inline-open")

    def show_approval(self, request: ApprovalPromptRequest) -> None:
        self.cancel_artifact_navigation()
        self.remove_class("palette-open")
        self.remove_class("secret-open")
        self.remove_class("setup-open")
        self.remove_class("usage-open")
        self.remove_class("status-open")
        self._remove_picker_classes()
        self.add_class("approval-open")
        prompt = self.query_one("#composer-approval", ApprovalPrompt)
        prompt.set_request(request)

    def clear_approval(self) -> None:
        try:
            self.query_one("#composer-approval", ApprovalPrompt).clear_request()
        except Exception:
            pass
        self.remove_class("approval-open")
        self.focus_input_safely()

    def focus_approval(self) -> None:
        try:
            self.query_one("#composer-approval", ApprovalPrompt).focus_options()
        except Exception:
            pass

    def route_approval_key(self, key: str) -> bool:
        try:
            prompt = self.query_one("#composer-approval", ApprovalPrompt)
        except Exception:
            return False
        return prompt.route_editor_key(key)

    def show_memory_review(self, item: dict, idx: int, total: int) -> None:
        self.cancel_artifact_navigation()
        self.remove_class("palette-open")
        self.remove_class("approval-open")
        self.remove_class("secret-open")
        self.remove_class("setup-open")
        self.remove_class("usage-open")
        self.remove_class("status-open")
        self._remove_picker_classes()
        self.add_class("review-open")
        panel = self.query_one("#composer-review", MemoryReviewPanel)
        panel.set_item(item, idx, total)

    def clear_memory_review(self) -> None:
        try:
            self.query_one("#composer-review", MemoryReviewPanel).clear()
        except Exception:
            pass
        self.remove_class("review-open")
        self.focus_input_safely()

    def show_secret_prompt(self, request: InlineSecretPromptRequest) -> None:
        self.cancel_artifact_navigation()
        self.remove_class("palette-open")
        self.remove_class("approval-open")
        self.remove_class("setup-open")
        self.remove_class("usage-open")
        self.remove_class("status-open")
        self._remove_picker_classes()
        self.add_class("secret-open")
        prompt = self.query_one("#composer-secret", InlineSecretPrompt)
        prompt.set_request(request)

    def clear_secret_prompt(self) -> None:
        try:
            self.query_one("#composer-secret", InlineSecretPrompt).clear_request()
        except Exception:
            pass
        self.remove_class("secret-open")
        self.focus_input_safely()

    def focus_secret_prompt(self) -> None:
        try:
            self.query_one("#composer-secret", InlineSecretPrompt).focus_input()
        except Exception:
            pass

    def show_setup_prompt(self, request: InlineSetupPromptRequest) -> None:
        self.cancel_artifact_navigation()
        self.remove_class("palette-open")
        self.remove_class("approval-open")
        self.remove_class("secret-open")
        self.remove_class("usage-open")
        self.remove_class("status-open")
        self._remove_picker_classes()
        self.add_class("setup-open")
        prompt = self.query_one("#composer-setup", InlineSetupPrompt)
        prompt.set_request(request)

    def clear_setup_prompt(self) -> None:
        try:
            self.query_one("#composer-setup", InlineSetupPrompt).clear_request()
        except Exception:
            pass
        self.remove_class("setup-open")
        self.focus_input_safely()

    def show_usage(self, **data: object) -> None:
        self.cancel_artifact_navigation()
        self.remove_class("palette-open")
        self.remove_class("approval-open")
        self.remove_class("secret-open")
        self.remove_class("setup-open")
        self.remove_class("review-open")
        self.remove_class("status-open")
        self._remove_picker_classes()
        self.add_class("usage-open")
        self.query_one("#composer-usage", UsagePanel).set_data(**data)

    def clear_usage(self) -> None:
        try:
            self.query_one("#composer-usage", UsagePanel).clear()
        except Exception:
            pass
        self.remove_class("usage-open")
        self.focus_input_safely()

    def show_status(self, **data: object) -> None:
        self.remove_class("palette-open")
        self.remove_class("approval-open")
        self.remove_class("secret-open")
        self.remove_class("setup-open")
        self.remove_class("review-open")
        self.remove_class("usage-open")
        self._remove_picker_classes()
        self.add_class("status-open")
        self.query_one("#composer-status-panel", SessionStatusPanel).set_data(**data)

    def clear_status(self) -> None:
        try:
            self.query_one("#composer-status-panel", SessionStatusPanel).clear()
        except Exception:
            pass
        self.remove_class("status-open")
        self.focus_input_safely()

    async def show_picker(self, picker: Widget, *, inline: bool = False) -> None:
        self.remove_class("palette-open")
        self.remove_class("approval-open")
        self.remove_class("secret-open")
        self.remove_class("setup-open")
        self.remove_class("review-open")
        self.remove_class("usage-open")
        self.remove_class("status-open")
        host = self.query_one("#composer-picker", Vertical)
        await host.remove_children()
        self.add_class("picker-open")
        self.add_class("picker-inline-open" if inline else "picker-floating-open")
        await host.mount(picker)
        self._focus_picker(picker)
        self.call_after_refresh(self._focus_picker, picker)

    def _focus_picker(self, picker: Widget) -> None:
        if not picker.is_mounted:
            return
        try:
            self.app.set_focus(picker, scroll_visible=False)
        except Exception:
            try:
                picker.focus(scroll_visible=False)
            except Exception:
                pass

    async def clear_picker(self) -> None:
        try:
            host = self.query_one("#composer-picker", Vertical)
            await host.remove_children()
        except Exception:
            pass
        self._remove_picker_classes()
        self.focus_input_safely()

    def focus_setup_prompt(self) -> None:
        try:
            self.query_one("#composer-setup", InlineSetupPrompt).focus_current()
        except Exception:
            pass

    def set_enabled(self, enabled: bool) -> None:
        """Enable or disable draft entry when the gateway is unavailable."""
        self.disabled = not enabled
        try:
            self.query_one("#composer-input", _Editor).disabled = not enabled
            self.query_one("#composer-palette", OptionList).disabled = not enabled
        except Exception:
            return
        if not enabled:
            self.cancel_artifact_navigation()
            self.remove_class("palette-open")

    def clear_attachments(self) -> None:
        self._attachments.clear()
        try:
            editor = self.query_one("#composer-input", _Editor)
            editor.text = self._clean_attachment_markers(editor.text)
            editor.move_cursor((0, len(editor.text)))
        except Exception:
            pass
        self._refresh_attachments()

    def _attach_detected(self, drop: FileDrop) -> None:
        if drop.path not in self._attachments:
            self._attachments.append(drop.path)
            self._insert_attachment_marker(drop.path, drop.remainder)
        elif drop.remainder:
            self._append_to_editor(drop.remainder)
        self._refresh_attachments()

    def attach_pasted_image_path(self, text: str) -> bool:
        drop = detect_media_drop(text)
        if not drop:
            return False
        self._attach_detected(drop)
        self.focus_input_safely()
        return True

    def attach_clipboard_image(self, *, notify: bool = False) -> bool:
        ok = self._try_attach_clipboard_image(notify=notify)
        if ok:
            self.focus_input_safely()
        return ok

    def _try_attach_clipboard_image(self, *, notify: bool) -> bool:
        path = save_clipboard_image()
        if path is None:
            if notify:
                self.app.notify("no image found in clipboard", severity="warning", timeout=3)
            return False
        if path not in self._attachments:
            self._attachments.append(path)
            self._insert_attachment_marker(path)
        self._refresh_attachments()
        self.app.notify(f"attached clipboard image: {path.name}", timeout=2)
        return True

    def _insert_attachment_marker(self, path: Path, remainder: str = "") -> None:
        editor = self.query_one("#composer-input", _Editor)
        existing = self._clean_attachment_markers(editor.text)
        marker = media_marker(path)
        if remainder.strip():
            parts = [existing, marker, remainder.strip()]
            editor.text = " ".join(part for part in parts if part)
        else:
            prefix = f"{existing} " if existing else ""
            editor.text = f"{prefix}{marker} "
        editor.move_cursor((0, len(editor.text)))

    def _append_to_editor(self, text: str) -> None:
        editor = self.query_one("#composer-input", _Editor)
        editor.text = f"{editor.text.strip()} {text.strip()}".strip()
        editor.move_cursor((0, len(editor.text)))

    @staticmethod
    def _clean_attachment_markers(text: str) -> str:
        cleaned = re.sub(r"(?i)\[(?:image|video)\]\s*", " ", text)
        return re.sub(r"\s+", " ", cleaned).strip()

    def _handle_image_command(self, text: str) -> bool:
        head, _, rest = text.partition(" ")
        head = head.lower()
        if head == "/paste":
            self._try_attach_clipboard_image(notify=True)
            return True
        if head not in ("/image", "/video"):
            return False
        arg = rest.strip()
        if not arg:
            usage = "/video <path>" if head == "/video" else "/image <path> or /image clear"
            self.app.notify(f"usage: {usage}", severity="warning", timeout=3)
            return True
        if arg.lower() in ("clear", "reset", "remove"):
            self.clear_attachments()
            self.app.notify("attachments cleared", timeout=2)
            return True
        drop = (
            detect_video_drop(arg, allow_bare=True)
            if head == "/video"
            else detect_media_drop(arg, allow_bare=True)
        )
        if not drop:
            label = "video" if head == "/video" else "media"
            self.app.notify(f"{label} file not found or unsupported", severity="error", timeout=4)
            return True
        self._attach_detected(drop)
        self.app.notify(f"attached {drop.kind}: {drop.path.name}", timeout=2)
        return True

    def _refresh_attachments(self) -> None:
        try:
            widget = self.query_one("#composer-attachments", Static)
        except Exception:
            return
        labels = format_attachment_labels(self._attachments)
        if labels:
            widget.update(labels)
            widget.add_class("has-attachments")
        else:
            widget.update("")
            widget.remove_class("has-attachments")

    # --- queue API ------------------------------------------------

    def enqueue(
        self,
        text: str,
        attachments: list[Path] | None = None,
        *,
        skill_notice: str | None = None,
    ) -> int:
        """Append a message to the pending queue. Returns new queue length."""
        self._queue.append(
            QueuedDraft(
                text=text,
                attachments=list(attachments or []),
                skill_notice=skill_notice,
            )
        )
        self._render_queue()
        return len(self._queue)

    def dequeue(self) -> QueuedDraft | None:
        """Pop the oldest queued message, or None if empty."""
        if not self._queue:
            return None
        item = self._queue.pop(0)
        self._render_queue()
        return item

    def queue_size(self) -> int:
        return len(self._queue)

    def clear_queue(self) -> None:
        self._queue.clear()
        self._render_queue()

    def _render_queue(self) -> None:
        from flowly.tui.panes.queued_messages import QueuedMessages
        try:
            qw = self.query_one("#composer-queue", QueuedMessages)
        except Exception:
            return
        qw.queue = [
            render_message_with_attachments(item.text, item.attachments)
            for item in self._queue
        ]
        qw.edit_idx = self._queue_edit_idx
        # Hint line shows queue count → refresh after every queue mutation.
        self._refresh_hint()

    # --- queue edit navigation (cycleQueue + Ctrl+X + Esc) ---------

    def cycle_queue(self, direction: int) -> bool:
        """Move queue-edit cursor. Returns True if queue handled the nav."""
        if not self._queue:
            return False
        editor = self.query_one("#composer-input", _Editor)
        if self._queue_edit_idx is None:
            self._draft_before_queue_edit = editor.text
            self._draft_before_queue_edit_attachments = list(self._attachments)
            self._queue_edit_idx = 0 if direction > 0 else len(self._queue) - 1
        else:
            n = len(self._queue)
            self._queue_edit_idx = (self._queue_edit_idx + direction + n) % n
        item = self._queue[self._queue_edit_idx]
        editor.text = render_message_with_attachments(item.text, item.attachments)
        self._attachments = list(item.attachments)
        self._refresh_attachments()
        editor.move_cursor((0, 0))
        self._render_queue()
        return True

    def cancel_queue_edit(self) -> None:
        if self._queue_edit_idx is None:
            return
        self._queue_edit_idx = None
        editor = self.query_one("#composer-input", _Editor)
        editor.text = self._draft_before_queue_edit
        self._attachments = list(self._draft_before_queue_edit_attachments)
        self._draft_before_queue_edit = ""
        self._draft_before_queue_edit_attachments = []
        self._refresh_attachments()
        self._render_queue()

    def delete_editing_queue_item(self) -> None:
        # Defensive against stale ``_queue_edit_idx`` state: a queued
        # item can be removed underneath us (drain, clear_queue, etc.)
        # without the editor losing focus, so by the time Ctrl+X fires
        # the index may point past the end — or the queue may be
        # entirely empty while idx is still a hold-over integer. Both
        # used to raise IndexError on the ``del`` below; now we just
        # reset and bail like the "nothing to delete" path.
        if (
            self._queue_edit_idx is None
            or not self._queue
            or self._queue_edit_idx >= len(self._queue)
            or self._queue_edit_idx < 0
        ):
            self._queue_edit_idx = None
            try:
                editor = self.query_one("#composer-input", _Editor)
                editor.text = self._draft_before_queue_edit
                self._attachments = list(self._draft_before_queue_edit_attachments)
                self._refresh_attachments()
            except Exception:
                pass
            self._draft_before_queue_edit = ""
            self._draft_before_queue_edit_attachments = []
            self._render_queue()
            return
        del self._queue[self._queue_edit_idx]
        if not self._queue:
            self._queue_edit_idx = None
            editor = self.query_one("#composer-input", _Editor)
            editor.text = self._draft_before_queue_edit
            self._attachments = list(self._draft_before_queue_edit_attachments)
            self._draft_before_queue_edit = ""
            self._draft_before_queue_edit_attachments = []
        else:
            self._queue_edit_idx = min(self._queue_edit_idx, len(self._queue) - 1)
            editor = self.query_one("#composer-input", _Editor)
            item = self._queue[self._queue_edit_idx]
            editor.text = render_message_with_attachments(item.text, item.attachments)
            self._attachments = list(item.attachments)
        self._refresh_attachments()
        self._render_queue()

    def focus_input_safely(self) -> None:
        """Focus input if mounted, no-op otherwise (used after async ops)."""
        try:
            self.query_one("#composer-input", _Editor).focus()
        except Exception:
            pass

    # --- $EDITOR escape (Ctrl+E) -----------------------------------

    def action_open_editor(self) -> None:
        """Suspend the TUI, spawn $EDITOR on the current draft, resume."""
        editor_cmd = self._resolve_editor()
        if not editor_cmd:
            self.app.notify(
                "no $EDITOR found (set EDITOR env var or install vim/nano)",
                severity="error", timeout=4,
            )
            return

        ed = self.query_one("#composer-input", _Editor)
        original_text = ed.text

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".md", prefix="flowly-draft-", delete=False, encoding="utf-8",
        ) as fh:
            fh.write(original_text)
            tmp_path = Path(fh.name)

        try:
            with self.app.suspend():
                # Inherit terminal directly; editor runs in foreground.
                # close_fds=True avoids `fds_to_keep` errors on Python 3.14
                # when Textual's internal handles aren't kosher to inherit.
                result = subprocess.run([*editor_cmd, str(tmp_path)], close_fds=True)
            if result.returncode != 0:
                self.app.notify(
                    f"editor exited with code {result.returncode}; draft unchanged",
                    severity="warning", timeout=3,
                )
                return
            new_text = tmp_path.read_text(encoding="utf-8")
            # Strip a single trailing newline editors add silently.
            if new_text.endswith("\n"):
                new_text = new_text[:-1]
            ed.text = new_text
            ed.move_cursor((ed.document.line_count - 1, len(ed.document[-1]) if new_text else 0))
            ed.focus()
        finally:
            try:
                tmp_path.unlink(missing_ok=True)
            except Exception:
                pass

    @staticmethod
    def _resolve_editor() -> list[str] | None:
        env = os.environ.get("VISUAL") or os.environ.get("EDITOR")
        if env:
            # $EDITOR may contain flags ("code -w", "nvim +Goyo"); split safely.
            return env.split()
        for fallback in ("nvim", "vim", "nano", "vi"):
            if shutil.which(fallback):
                return [fallback]
        return None

    # --- editor events --------------------------------------------

    @on(TextArea.Changed, "#composer-input")
    def _on_changed(self, event: TextArea.Changed) -> None:
        text = event.text_area.text
        if isinstance(event.text_area, _Editor):
            self._resize_editor_for_content(event.text_area)
        # `?` alone → show floating HelpHint, hide as soon as anything else.
        self._toggle_help_hint(text == "?")
        drop = detect_media_drop(text)
        if drop:
            self._attach_detected(drop)
            self.remove_class("palette-open")
            return
        # Slash command palette
        if text.startswith("/") and "\n" not in text:
            self.add_class("palette-open")
            self._refresh_palette(text)
            return
        # Path completion: trigger when last token starts with ./ ../ ~/ /
        # or @ (file reference convention). Pulls up to 30 matches.
        last_token = self._last_completion_token(text)
        if last_token and (
            last_token[0] in "./~@" or last_token.startswith(("./", "../"))
        ):
            matches = self._path_complete(last_token)
            if matches:
                self.add_class("palette-open")
                self._show_path_matches(last_token, matches)
                return
        self.remove_class("palette-open")

    def _toggle_help_hint(self, on: bool) -> None:
        try:
            from flowly.tui.panes.help_hint import HelpHint
            hint = self.app.query_one(HelpHint)
        except Exception:
            return
        if on:
            hint.show()
        else:
            hint.hide()

    @on(_Editor.Submit)
    def _on_submit(self, event: _Editor.Submit) -> None:
        text = event.text
        editor = self.query_one("#composer-input", _Editor)

        # If editing a queued item, Enter replaces that queue entry instead
        # of sending a new message.
        if self._queue_edit_idx is not None:
            self._queue[self._queue_edit_idx] = QueuedDraft(
                text=self._clean_attachment_markers(text) if self._attachments else text,
                attachments=list(self._attachments),
            )
            self._queue_edit_idx = None
            editor.text = self._draft_before_queue_edit
            self._attachments = list(self._draft_before_queue_edit_attachments)
            self._draft_before_queue_edit = ""
            self._draft_before_queue_edit_attachments = []
            self._refresh_attachments()
            self._render_queue()
            return

        if text.split(maxsplit=1)[0].lower() in ("/image", "/video", "/paste"):
            editor.text = ""
            self.remove_class("palette-open")
            self._handle_image_command(text)
            return

        drop = detect_media_drop(text)
        if drop:
            if drop.path not in self._attachments:
                self._attachments.append(drop.path)
                self._insert_attachment_marker(drop.path, drop.remainder)
            text = drop.remainder

        attachments = list(self._attachments)
        text = self._clean_attachment_markers(text) if attachments else text
        editor.text = ""
        self._attachments = []
        self._refresh_attachments()
        self.remove_class("palette-open")
        self._record_history(render_message_with_attachments(text, attachments))
        if not attachments and text.startswith("!") and len(text) > 1:
            self.post_message(self.Shell(text[1:].strip()))
        elif not attachments and text.startswith("/"):
            self.post_message(self.Slash(text))
        else:
            self.post_message(self.Submitted(text, attachments))

    @on(_Editor.QueueCycle)
    def _on_queue_cycle(self, event: _Editor.QueueCycle) -> None:
        # Cycle order: queue first, then history.
        if self.cycle_queue(event.direction):
            return
        if event.direction > 0:
            self._history_prev()
        else:
            if self._history_idx is not None:
                self._history_next()
                return
            editor = self.query_one("#composer-input", _Editor)
            if not editor.text and self.enter_artifact_navigation():
                return
            self._history_next()

    @on(_Editor.QueueDelete)
    def _on_queue_delete(self) -> None:
        self.delete_editing_queue_item()

    @on(_Editor.QueueCancelEdit)
    def _on_queue_cancel(self) -> None:
        self.cancel_queue_edit()

    @on(_Editor.PasteAttachment)
    def _on_paste_attachment(self, event: _Editor.PasteAttachment) -> None:
        drop = detect_media_drop(event.text)
        if drop:
            self._attach_detected(drop)

    @on(_Editor.PaletteCycle)
    def _on_palette_cycle(self, event: _Editor.PaletteCycle) -> None:
        ol = self.query_one("#composer-palette", OptionList)
        if event.direction > 0:
            ol.action_cursor_down()
        else:
            ol.action_cursor_up()

    @on(_Editor.PaletteSelect)
    def _on_palette_select(self) -> None:
        ol = self.query_one("#composer-palette", OptionList)
        ol.action_select()

    @on(_Editor.PaletteEnter)
    def _on_palette_enter(self) -> None:
        """Enter while palette is open: apply highlighted pick, then submit
        (for slash commands) or stay (for path completions). If no option
        is highlighted, fall back to a plain submit of the raw text."""
        ol = self.query_one("#composer-palette", OptionList)
        opt = ol.highlighted_option
        editor = self.query_one("#composer-input", _Editor)
        if opt is None or opt.id is None:
            # Empty palette / nothing picked → just submit whatever's there.
            text = editor.text.strip()
            if text or self._attachments:
                if text.split(maxsplit=1)[0].lower() in ("/image", "/video", "/paste"):
                    editor.text = ""
                    self.remove_class("palette-open")
                    self._handle_image_command(text)
                    return
                drop = detect_media_drop(text)
                if drop:
                    if drop.path not in self._attachments:
                        self._attachments.append(drop.path)
                        self._insert_attachment_marker(drop.path, drop.remainder)
                    text = drop.remainder
                attachments = list(self._attachments)
                text = self._clean_attachment_markers(text) if attachments else text
                self._record_history(render_message_with_attachments(text, attachments))
                self.remove_class("palette-open")
                editor.text = ""
                self._attachments = []
                self._refresh_attachments()
                if not attachments and text.startswith("!") and len(text) > 1:
                    self.post_message(self.Shell(text[1:].strip()))
                elif not attachments and text.startswith("/"):
                    self.post_message(self.Slash(text))
                else:
                    self.post_message(self.Submitted(text, attachments))
            return

        cmd_id = str(opt.id)
        if cmd_id.startswith("PATH:"):
            # Path pick: apply (replace last token) and stay so the user
            # can keep composing the rest of the message around it.
            replacement = cmd_id[len("PATH:"):]
            current = editor.text
            try:
                last_space = max(current.rfind(" "), current.rfind("\n"))
            except Exception:
                last_space = -1
            editor.text = current[: last_space + 1] + replacement
            editor.move_cursor((0, len(editor.text)))
            self.remove_class("palette-open")
            return

        # Slash pick: replace whole text with the command, then submit
        # straight away. This makes "/ ↓ ↓ Enter" send the chosen command
        # without an extra keystroke.
        if cmd_id == "/image":
            editor.text = "/image "
            editor.move_cursor((0, len(editor.text)))
            self.remove_class("palette-open")
            return
        if cmd_id == "/video":
            editor.text = "/video "
            editor.move_cursor((0, len(editor.text)))
            self.remove_class("palette-open")
            return
        if cmd_id == "/paste":
            editor.text = ""
            self.remove_class("palette-open")
            self._handle_image_command(cmd_id)
            return
        editor.text = ""
        self.remove_class("palette-open")
        self._record_history(cmd_id)
        self.post_message(self.Slash(cmd_id))

    def _history_prev(self) -> None:
        if not self._history:
            return
        editor = self.query_one("#composer-input", _Editor)
        if self._history_idx is None:
            self._draft_when_browsing = editor.text
            self._history_idx = len(self._history) - 1
        elif self._history_idx > 0:
            self._history_idx -= 1
        editor.text = self._history[self._history_idx]
        editor.move_cursor((0, 0))

    def _history_next(self) -> None:
        if self._history_idx is None:
            return
        editor = self.query_one("#composer-input", _Editor)
        self._history_idx += 1
        if self._history_idx >= len(self._history):
            self._history_idx = None
            editor.text = self._draft_when_browsing
        else:
            editor.text = self._history[self._history_idx]

    @on(OptionList.OptionSelected, "#composer-palette")
    def _on_pick(self, event: OptionList.OptionSelected) -> None:
        cmd = str(event.option.id or "")
        if not cmd:
            return
        editor = self.query_one("#composer-input", _Editor)
        if cmd.startswith("PATH:"):
            # Replace just the last token (the partial path) with the full pick.
            replacement = cmd[len("PATH:"):]
            current = editor.text
            try:
                last_space = max(current.rfind(" "), current.rfind("\n"))
            except Exception:
                last_space = -1
            editor.text = current[: last_space + 1] + replacement
        else:
            editor.text = cmd + " "
        editor.move_cursor((0, len(editor.text)))
        editor.focus()
        self.remove_class("palette-open")

    # --- helpers --------------------------------------------------

    def _refresh_palette(self, prefix: str) -> None:
        ol = self.query_one("#composer-palette", OptionList)
        ol.clear_options()
        added = 0
        for name, desc in _filter_slash_palette(self._palette, prefix):
            ol.add_option(Option(f"{name:<14}  {desc}", id=name))
            added += 1
        # Pre-highlight the first option so ↑/↓ navigation has a visible
        # anchor immediately (without this the first ↓ silently moves to
        # index 0 but users perceive it as "arrows do nothing").
        if added:
            ol.highlighted = 0

    @staticmethod
    def _safe_is_dir(path: Path) -> bool:
        """``Path.is_dir`` that never raises.

        macOS TCC-protected entries (~/.Trash, sandboxed dot-dirs, …) can be
        *listed* by their parent yet raise ``PermissionError: Operation not
        permitted`` on ``stat`` — pathlib only swallows ENOENT-style errors,
        so an unguarded ``is_dir()`` on such an entry kills the whole TUI.
        """
        try:
            return path.is_dir()
        except OSError:
            return False

    def _path_complete(self, token: str) -> list[Path]:
        """Return up to 30 filesystem matches for ``token``.

        Conventions:
          ./foo  ../foo  /abs/foo  → glob from cwd or absolute
          ~/foo                    → glob from home
          @foo                     → glob from cwd
        """
        raw = token[1:] if token.startswith("@") else token
        try:
            expanded = Path(raw).expanduser()
        except Exception:
            return []
        # If the user typed a complete dir name + slash, list its contents.
        if raw.endswith("/") or raw in ("", ".", "..", "~", "~/"):
            base = expanded if self._safe_is_dir(expanded) else expanded.parent
            try:
                entries = sorted(base.iterdir())[:30]
            except (OSError, PermissionError):
                return []
            return entries
        # Otherwise glob with parent + name* pattern.
        parent = expanded.parent if expanded.parent != Path() else Path(".")
        name_pat = expanded.name + "*"
        try:
            entries = sorted(parent.glob(name_pat))[:30]
        except (OSError, PermissionError, ValueError):
            return []
        return entries

    def _show_path_matches(self, prefix: str, matches: list[Path]) -> None:
        ol = self.query_one("#composer-palette", OptionList)
        ol.clear_options()
        # Replace the last token in the input with the chosen path.
        for p in matches:
            try:
                display = str(p)
                # Re-shorten with ~ if home prefix matches.
                home = str(Path.home())
                if display.startswith(home):
                    display = "~" + display[len(home):]
            except Exception:
                display = str(p)
            suffix = "/" if self._safe_is_dir(p) else ""
            label = display + suffix
            # Encode action in id: prefix "PATH:" then full replacement
            ol.add_option(Option(f"📄 {label}", id=f"PATH:{label}"))
        if matches:
            ol.highlighted = 0

    def _record_history(self, text: str) -> None:
        if not text or (self._history and self._history[-1] == text):
            self._history_idx = None
            return
        self._history.append(text)
        if len(self._history) > HISTORY_MAX:
            self._history = self._history[-HISTORY_MAX:]
        self._history_idx = None
        self._draft_when_browsing = ""
        try:
            HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
            HISTORY_PATH.write_text(
                "\n".join(s.replace("\n", "\\n") for s in self._history) + "\n",
                encoding="utf-8",
            )
        except OSError:
            pass

    def _load_history(self) -> None:
        try:
            raw = HISTORY_PATH.read_text(encoding="utf-8")
        except (OSError, FileNotFoundError):
            return
        lines = [line for line in raw.splitlines() if line.strip()]
        self._history = [line.replace("\\n", "\n") for line in lines[-HISTORY_MAX:]]
