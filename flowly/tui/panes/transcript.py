"""Scrollable chat transcript with bordered message bubbles."""

from __future__ import annotations

import time
from datetime import datetime

from rich import box
from rich.console import RenderableType
from rich.markdown import Markdown as RichMarkdown
from rich.markdown import TableElement as RichMarkdownTableElement
from rich.markup import escape
from rich.style import Style
from rich.table import Table
from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Container, VerticalScroll
from textual.widgets import Static

from flowly.tui.theme import get_code_theme, get_palette


class _TransparentMarkdownTable(RichMarkdownTableElement):
    """Markdown table renderer without a cell/header background fill."""

    def __rich_console__(self, console, options):
        table = Table(
            box=box.SIMPLE,
            pad_edge=False,
            style=Style(),
            header_style=Style(),
            border_style=Style(),
            row_styles=[],
            show_edge=True,
            collapse_padding=True,
        )

        if self.header is not None and self.header.row is not None:
            for column in self.header.row.cells:
                table.add_column(column.content.copy(), header_style=Style(), style=Style())

        if self.body is not None:
            for row in self.body.rows:
                table.add_row(*(element.content for element in row.cells))

        yield table


class _TranscriptMarkdown(RichMarkdown):
    elements = {
        **RichMarkdown.elements,
        "table_open": _TransparentMarkdownTable,
    }


def _request_tail_scroll_from(widget: object, *, force: bool = False) -> None:
    """Ask the containing transcript to follow the tail after layout settles."""
    node = widget
    while node is not None:
        request = getattr(node, "request_tail_scroll", None)
        if callable(request):
            request(force=force)
            return
        node = getattr(node, "parent", None)


def _format_message_time(value: object | None) -> str:
    if value is None:
        return time.strftime("%I:%M %p").lstrip("0")
    try:
        if isinstance(value, (int, float)):
            dt = datetime.fromtimestamp(float(value)).astimezone()
        else:
            raw = str(value).strip()
            if not raw:
                return time.strftime("%I:%M %p").lstrip("0")
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if dt.tzinfo is not None:
                dt = dt.astimezone()
        return dt.strftime("%I:%M %p").lstrip("0")
    except (OSError, ValueError, TypeError):
        return time.strftime("%I:%M %p").lstrip("0")


class Bubble(Container):
    """One message in the transcript.

    Renders as a rounded box:
        ╭─ ❯ You ─────────────────────╮
            user message content
        ╰──────────────────────────────╯

        ╭─ 🦦 Flowly ──────────────────╮
            assistant response markdown
        ╰──────────────────────────────╯

    The title is set via ``border_title`` so the box-drawing border picks
    it up as `─ title ─` inside the top edge — matches the screenshot exactly.
    """

    DEFAULT_CSS = """
    Bubble {
        height: auto;
        padding: 0 2;
        margin: 0 0 1 0;
        border: round #466b73;
        background: transparent;
        color: #e6fbff;
    }
    Bubble.user {
        width: 100%;
        padding: 1 2;
        margin: 1 0 1 0;
        border: none;
        background: #101010;
    }
    Bubble.assistant { border: round #00a6c8; background: transparent; }
    Bubble.system    { border: round #466b73; background: transparent; }
    Bubble.slash     { border: round #466b73; background: transparent; }
    Bubble.error     { border: round #ff5d6c; background: transparent; }

    Bubble > .bubble-body {
        height: auto;
        padding: 0;
        background: transparent;
        color: #e6fbff;
    }
    Bubble > ToolLine {
        margin: 0;
    }
    """

    LABEL_GLYPHS = {
        "user":      ("❯", "You"),
        "assistant": ("🦦", "Flowly"),
        "system":    ("·", "system"),
        "error":     ("!", "error"),
        "slash":     ("/", "command"),
    }

    # Long content thresholds
    LONG_USER_MSG_CHARS = 800
    LONG_SYSTEM_CHARS = 400

    # STREAM_TYPING_BATCH_MS — batch deltas every 80ms instead of
    # re-parsing markdown on each token. Eliminates flicker + cuts CPU.
    STREAM_BATCH_MS = 80

    def __init__(
        self,
        role: str,
        text: str = "",
        timestamp: object | None = None,
        *,
        collapse_long: bool = True,
    ) -> None:
        super().__init__(classes=role)
        self._role = role
        self._text = text
        self._streaming = False
        self._collapse_long = collapse_long
        self._collapsed = (
            collapse_long and role == "system" and len(text) > self.LONG_SYSTEM_CHARS
        )
        # Stream batching: deltas accumulate in _text directly (cheap str
        # concat). A single periodic timer triggers the expensive Markdown
        # re-render. No blinking cursor — we just keep the text static
        # while streaming and let the spinner segment of the status bar
        # carry the "still working" signal.
        self._stream_timer = None
        self._dirty = False
        self._created_at = _format_message_time(timestamp)
        if role != "user":
            glyph, label = self.LABEL_GLYPHS.get(role, ("·", role))
            self.border_title = f" {glyph} {label} "
            self.border_title_align = "left"

    def compose(self) -> ComposeResult:
        yield Static(self._renderable(), classes="bubble-body", markup=False)

    def on_mount(self) -> None:
        self._refresh_body()

    def mark_streaming(self, on: bool) -> None:
        """Begin/end stream batching for this bubble.

        While streaming, ``append(delta)`` is a cheap string concat that
        only sets a dirty flag. A single timer drains the buffer every
        STREAM_BATCH_MS, doing one Markdown re-parse per batch. No
        blinking cursor — keeps the bubble visually stable.
        """
        if on and not self._streaming:
            self._streaming = True
            self._stream_timer = self.set_interval(
                self.STREAM_BATCH_MS / 1000, self._flush_stream
            )
        elif not on and self._streaming:
            self._streaming = False
            if self._stream_timer:
                self._stream_timer.stop()
                self._stream_timer = None
            # Final flush — render whatever's left.
            self._refresh_body()
            self._dirty = False

    def _flush_stream(self) -> None:
        if self._dirty:
            self._refresh_body()
            self._dirty = False

    def update_text(self, text: str) -> None:
        self._text = text
        self._refresh_body()

    def append(self, delta: str) -> None:
        # Hot path: cheap concat + dirty flag. Markdown re-parse happens
        # on the next _flush_stream tick (≤ STREAM_BATCH_MS later).
        self._text += delta
        if self._streaming:
            self._dirty = True
        else:
            self._refresh_body()

    def _renderable(self) -> RenderableType:
        body = self._text or " "

        if self._role == "user":
            return self._render_user(body)

        # Long system message → collapsed by default
        if self._role == "system" and self._collapsed:
            first_line = body.split("\n", 1)[0][:120]
            return Text(
                f"▸ {first_line} — {len(body):,} chars  (click to expand)",
                style="dim",
            )

        if self._role in ("user", "assistant"):
            return _TranscriptMarkdown(body, code_theme=get_code_theme(), inline_code_lexer="python")
        # System / error / slash bubbles routinely include Rich markup like
        # [b]…[/b] or [green]…[/green]. Text.from_markup parses those tags;
        # plain Text(body) would render them literally as seen in the
        # "[b]brunowasright@gmail.com[/b]" screenshot bug.
        if self._role == "error":
            return Text.from_markup(body, style="bold red")
        if self._role == "slash":
            return Text.from_markup(body, style="dim italic")
        return Text.from_markup(body, style="dim")

    def _render_user(self, body: str) -> Table:
        if len(body) > self.LONG_USER_MSG_CHARS:
            head = body[: self.LONG_USER_MSG_CHARS // 4]
            tail = body[-self.LONG_USER_MSG_CHARS // 4 :]
            body = f"{head}\n\n...[long message · {len(body):,} chars]...\n\n{tail}"

        row = Table.grid(expand=True)
        row.add_column(ratio=1)
        row.add_column(justify="right", no_wrap=True)

        palette = get_palette()
        text = Text()
        text.append("› ", style=f"bold {palette.text_muted}")
        text.append(body, style=f"bold {palette.text}")
        row.add_row(text, Text(self._created_at, style=palette.text_muted))
        return row

    def on_click(self) -> None:
        # System bubble: toggle collapse on click.
        if (
            self._collapse_long
            and self._role == "system"
            and len(self._text) > self.LONG_SYSTEM_CHARS
        ):
            self._collapsed = not self._collapsed
            self._refresh_body()

    def _refresh_body(self) -> None:
        try:
            self.query_one(".bubble-body", Static).update(self._renderable())
        except Exception:
            pass
        else:
            _request_tail_scroll_from(self)

    def add_tool(self, tool_call_id: str, name: str, args: dict) -> "ToolLine":
        """Attach a tool trail to this assistant turn instead of the transcript.

        Keeping tool rows inside the assistant bubble models tools as
        details of the current response, not separate chat messages.
        """
        line = ToolLine(tool_call_id, name, _summarize_args(name, args), args)
        try:
            body = self.query_one(".bubble-body", Static)
        except Exception:
            self.mount(line)
        else:
            self.mount(line, before=body)
        _request_tail_scroll_from(self)
        return line

    def find_tool(self, tool_call_id: str) -> "ToolLine | None":
        for child in self.children:
            if isinstance(child, ToolLine) and child.tool_call_id == tool_call_id:
                return child
        return None


class ToolLine(Static):
    """Compact tool-invocation line with live elapsed counter."""

    DEFAULT_CSS = """
    ToolLine {
        height: 1;
        padding: 0 1 0 3;
        margin: 0 0 0 0;
        background: transparent;
    }
    ToolLine.running { color: #f2c94c; }
    ToolLine.ok      { color: #31d0aa; }
    ToolLine.fail    { color: #ff5d6c; text-style: bold; }
    """

    SPINNER_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
    can_focus = True

    def __init__(
        self,
        tool_call_id: str,
        name: str,
        arg_preview: str,
        args: dict | None = None,
    ) -> None:
        super().__init__("", classes="running")
        self.tool_call_id = tool_call_id
        self._name = name
        self._icon = _tool_icon(name)
        self._arg_preview = arg_preview
        self._args = dict(args or {})
        self._frame = 0
        self._timer = None
        self._done = False
        self._success = False
        self._duration_ms = 0
        self._preview = ""
        self._expanded = False
        self._start = time.monotonic()

    def on_click(self) -> None:
        self._toggle_expand()

    def key_enter(self) -> None:
        self._toggle_expand()

    def on_mount(self) -> None:
        self._render_running()
        self._timer = self.set_interval(0.1, self._tick)

    def _tick(self) -> None:
        if self._done:
            return
        self._frame = (self._frame + 1) % len(self.SPINNER_FRAMES)
        self._render_running()

    def _render_running(self) -> None:
        spin = self.SPINNER_FRAMES[self._frame]
        elapsed = time.monotonic() - self._start
        elapsed_str = f"{elapsed:.1f}s" if elapsed < 60 else f"{int(elapsed) // 60}m{int(elapsed) % 60}s"
        preview = f"  [dim]{self._arg_preview}[/dim]" if self._arg_preview else ""
        hint = "  [dim]↵ details[/dim]" if self._can_expand() else ""
        self.update(
            f"  {spin} {self._icon}  [b]{self._name}[/b]{preview}  [dim]· {elapsed_str}[/dim]{hint}"
        )

    def complete(self, success: bool, duration_ms: int, preview: str = "") -> None:
        self._done = True
        self._success = success
        self._duration_ms = duration_ms
        self._preview = preview
        if self._timer:
            self._timer.stop()
        self.remove_class("running")
        icon = "✓" if success else "✗"
        self.add_class("ok" if success else "fail")
        dur = f"{duration_ms}ms" if duration_ms < 1000 else f"{duration_ms / 1000:.1f}s"
        if preview:
            hint = "  [dim]↵ expand[/dim]"
        elif self._can_expand():
            hint = "  [dim]↵ details[/dim]"
        else:
            hint = ""
        arg_preview = f"  [dim]{self._arg_preview}[/dim]" if self._arg_preview else ""
        self.update(
            f"  {icon} {self._icon}  [b]{self._name}[/b]{arg_preview}  [dim]· {dur}[/dim]{hint}"
        )
        self._refresh_detail()
        _request_tail_scroll_from(self)

    def _toggle_expand(self) -> None:
        if not self._can_expand():
            return
        parent = self.parent
        if parent is None:
            return
        marker_id = self._detail_marker_id()
        existing = self._find_detail_widget()
        if existing is not None:
            existing.remove()
            self._expanded = False
            _request_tail_scroll_from(self)
            return
        rendered = self._detail_renderable()
        # markup=True so the ` … +N more lines[/dim]` hint colors render
        detail = Static(
            rendered,
            id=marker_id,
            classes="tool-detail",
            markup=True,
        )
        parent.mount(detail, after=self)
        self._expanded = True
        _request_tail_scroll_from(self)

    def _can_expand(self) -> bool:
        if self._done:
            return bool(self._preview or self._args or self._arg_preview)
        return bool(self._args or self._arg_preview)

    def _detail_marker_id(self) -> str:
        # Sanitize: tool_call_id may contain characters invalid in a Textual
        # widget id (e.g. Moonshot/Kimi emit "obsidian_search:49ee91c7" with a
        # colon). Textual ids allow only letters, digits, underscore, hyphen.
        raw_marker = str(self.tool_call_id or id(self))
        safe_marker = "".join(c if (c.isalnum() or c in "_-") else "_" for c in raw_marker)
        return f"tool-detail-{safe_marker}"

    def _find_detail_widget(self):
        parent = self.parent
        if parent is None:
            return None
        marker_id = self._detail_marker_id()
        for child in parent.children:
            if getattr(child, "id", None) == marker_id:
                return child
        return None

    def _refresh_detail(self) -> None:
        detail = self._find_detail_widget()
        if detail is None:
            return
        detail.update(self._detail_renderable())

    def _detail_renderable(self) -> str:
        if self._done:
            if self._preview:
                return _render_tool_preview(self._preview)
            return _railed_text("status: complete\nresult: no preview returned")

        if self._args:
            import json

            safe_args = _redact_tool_args(self._args)
            pretty_args = json.dumps(safe_args, indent=2, ensure_ascii=False)
            return _railed_text(f"status: running\nargs:\n{escape(pretty_args)}")
        return _railed_text(f"status: running\nargs: {escape(self._arg_preview)}")


def _render_tool_preview(preview: str):
    """Prepend ` │ ` rail to each line; JSON parsed if detected."""
    import json
    text = preview.strip()
    # Try JSON first — render with rich + then prefix rails.
    if text.startswith(("{", "[")):
        try:
            obj = json.loads(text)
            # Pretty-print to lines, then add rails.
            pretty = json.dumps(obj, indent=2, ensure_ascii=False)
            return _railed_text(pretty)
        except (ValueError, TypeError):
            pass
    return _railed_text(preview)


def _railed_text(s: str, max_lines: int = 12) -> str:
    """Add ` │ ` prefix to each line; truncate to max_lines with continuation hint."""
    lines = s.splitlines() or [""]
    if len(lines) > max_lines:
        shown = lines[:max_lines]
        suffix = f"\n │ [dim]…and {len(lines) - max_lines} more lines[/dim]"
    else:
        shown = lines
        suffix = ""
    return "\n".join(f" │ {ln}" for ln in shown) + suffix


_SENSITIVE_KEY_PARTS = (
    "api_key",
    "apikey",
    "authorization",
    "auth",
    "bearer",
    "cookie",
    "credential",
    "password",
    "secret",
    "token",
)

_TOOL_ICONS = {
    "artifact": "◇",
    "browser": "◉",
    "browser_tab": "◉",
    "computer": "⌘",
    "cron": "◷",
    "docker": "▣",
    "edit_file": "✎",
    "email": "@",
    "exec": "$",
    "google_calendar": "◷",
    "google_drive": "▤",
    "knowledge_graph": "⌬",
    "linear": "◆",
    "list_dir": "▤",
    "memory_append": "＋",
    "message": "✉",
    "process": "$",
    "read_file": "▤",
    "screenshot": "□",
    "session_search": "⌕",
    "skill_manage": "✦",
    "skill_view": "✦",
    "spawn": "↗",
    "system": "◌",
    "web_fetch": "◎",
    "web_search": "⌕",
    "write_file": "✎",
    "x": "𝕏",
    "x_search": "𝕏",
}

_PRIMARY_ARG_KEYS = {
    "artifact": ("action", "title", "name"),
    "browser": ("action", "url", "selector", "text"),
    "browser_tab": ("action", "url", "selector", "text"),
    "computer": ("action", "app", "text"),
    "cron": ("action", "name", "schedule", "message"),
    "docker": ("action", "container", "image", "name"),
    "edit_file": ("path", "file_path", "old_text"),
    "email": ("action", "to", "subject", "query"),
    "exec": ("command", "cmd"),
    "google_calendar": ("action", "summary", "calendar_id"),
    "google_drive": ("action", "name", "query", "file_id"),
    "knowledge_graph": ("action", "subject", "name"),
    "linear": ("action", "title", "issue_id", "query"),
    "list_dir": ("path", "dir", "directory"),
    "memory_append": ("content", "memory", "path"),
    "message": ("channel", "chat_id", "content"),
    "process": ("action", "command", "session_id", "data"),
    "read_file": ("path", "file_path"),
    "screenshot": ("path", "window", "app"),
    "session_search": ("query", "session_key"),
    "skill_manage": ("action", "name", "file_path"),
    "skill_view": ("name", "path"),
    "spawn": ("task", "label", "agent"),
    "system": ("action", "name"),
    "web_fetch": ("url", "urls"),
    "web_search": ("query",),
    "write_file": ("path", "file_path"),
    "x": ("action", "query", "text"),
    "x_search": ("query",),
}


def _tool_icon(name: str) -> str:
    return _TOOL_ICONS.get(name, "⚡")


def _is_sensitive_key(key: str) -> bool:
    lowered = key.lower().replace("-", "_")
    return any(part in lowered for part in _SENSITIVE_KEY_PARTS)


def _redact_tool_args(value: object, key: str = "") -> object:
    if key and _is_sensitive_key(key):
        return "redacted"
    if isinstance(value, dict):
        return {str(k): _redact_tool_args(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_tool_args(v, key) for v in value]
    if isinstance(value, tuple):
        return [_redact_tool_args(v, key) for v in value]
    return value


def _clean_preview_value(key: str, value: object, max_len: int = 72) -> str:
    if _is_sensitive_key(key):
        return "redacted"
    if isinstance(value, (list, tuple)):
        if not value:
            text = "[]"
        elif len(value) == 1:
            text = str(value[0])
        else:
            text = f"{len(value)} items"
    elif isinstance(value, dict):
        text = f"{len(value)} fields"
    else:
        text = str(value)
    text = " ".join(text.split())
    if len(text) > max_len:
        text = text[: max_len - 1] + "…"
    return escape(text)


def _format_arg_pair(key: str, value: object) -> str:
    value_text = _clean_preview_value(key, value)
    if _is_sensitive_key(key):
        return f"{key}=••••"
    if key in {"command", "cmd", "query", "url", "path", "file_path"}:
        return value_text
    return f"{key}={value_text}"


def _with_extras(preview: str, args: dict, used_keys: set[str]) -> str:
    extras = len([k for k, v in args.items() if k not in used_keys and v not in (None, "")])
    return preview + (f"  [dim]+{extras}[/dim]" if extras else "")


def _first_arg(args: dict, keys: tuple[str, ...]) -> tuple[str, object] | None:
    for key in keys:
        value = args.get(key)
        if value not in (None, ""):
            return key, value
    return None


def _summarize_args(name: str, args: dict) -> str:
    """One-line compact, redacted preview that keeps tool rows scan-friendly."""
    if not args:
        return ""

    if name in {"browser", "browser_tab"}:
        action = _clean_preview_value("action", args.get("action", "action"), max_len=24)
        detail = _first_arg(args, ("url", "selector", "ref", "text", "query"))
        if detail:
            key, value = detail
            preview = f"{action} {_clean_preview_value(key, value, max_len=56)}"
            return _with_extras(preview, args, {"action", key})
        return _with_extras(action, args, {"action"})

    if name == "message":
        channel = _clean_preview_value("channel", args.get("channel", "message"), max_len=24)
        chat_id = args.get("chat_id")
        target = channel
        if chat_id:
            target = f"{target}:{_clean_preview_value('chat_id', chat_id, max_len=24)}"
        content = args.get("content")
        if content:
            preview = f"to {target} · {_clean_preview_value('content', content, max_len=48)}"
            return _with_extras(preview, args, {"channel", "chat_id", "content"})
        return _with_extras(f"to {target}", args, {"channel", "chat_id"})

    if name == "process":
        action = _clean_preview_value("action", args.get("action", "process"), max_len=24)
        detail = _first_arg(args, ("command", "session_id", "data"))
        if detail:
            key, value = detail
            preview = f"{action} {_clean_preview_value(key, value, max_len=56)}"
            return _with_extras(preview, args, {"action", key})
        return _with_extras(action, args, {"action"})

    if name == "cron":
        action = _clean_preview_value("action", args.get("action", "cron"), max_len=24)
        detail = _first_arg(args, ("name", "schedule", "message"))
        if detail:
            key, value = detail
            preview = f"{action} {_clean_preview_value(key, value, max_len=56)}"
            return _with_extras(preview, args, {"action", key})
        return _with_extras(action, args, {"action"})

    priority_keys = _PRIMARY_ARG_KEYS.get(name, ()) + (
        "path",
        "file_path",
        "filename",
        "command",
        "cmd",
        "query",
        "url",
        "text",
        "prompt",
        "name",
        "id",
        "action",
    )
    for k in priority_keys:
        if k in args and args[k] not in (None, ""):
            extras = len(args) - 1
            preview = _format_arg_pair(k, args[k])
            return preview + (f"  [dim]+{extras}[/dim]" if extras else "")

    bits: list[str] = []
    for key, value in list(args.items())[:3]:
        bits.append(_format_arg_pair(str(key), value))
    return ", ".join(bits)


class _TurnSeparator(Static):
    """Thin horizontal rule rendered between turns (above each user msg)."""

    DEFAULT_CSS = """
    _TurnSeparator {
        height: 1;
        margin: 1 0 1 0;
        padding: 0;
        color: #0f4c5c;
        background: transparent;
    }
    """

    def on_mount(self) -> None: self._draw()
    def on_resize(self) -> None: self._draw()

    def _draw(self) -> None:
        w = max(1, (self.size.width or 80) - 4)
        self.update("─" * w)


class TranscriptPane(VerticalScroll):
    """Scrolling history of bubbles."""

    TAIL_FOLLOW_THRESHOLD = 2.0

    DEFAULT_CSS = """
    TranscriptPane {
        height: 1fr;
        padding: 1 2 0 2;
        background: #000000;
        scrollbar-gutter: stable;
        scrollbar-background: #000000;
        scrollbar-background-hover: #050505;
        scrollbar-color: #0f4c5c;
        scrollbar-color-hover: #00a6c8;
    }
    TranscriptPane .tool-detail {
        height: auto;
        padding: 0 2;
        margin: 0 0 1 2;
        color: #83b8c2;
        background: transparent;
    }
    TranscriptPane .welcome-bubble {
        height: auto;
        padding: 1 2 2 2;
        margin-bottom: 1;
        background: transparent;
    }
    TranscriptPane .resume-marker {
        height: 1;
        padding: 0 2;
        margin: 0 0 1 0;
        color: #4a5358;
        background: transparent;
        text-align: center;
    }
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._follow_tail = True
        self._tail_scroll_pending = False

    @staticmethod
    def _is_near_tail_position(
        scroll_y: float,
        max_scroll_y: float,
        *,
        threshold: float = TAIL_FOLLOW_THRESHOLD,
    ) -> bool:
        return max(0.0, float(max_scroll_y) - float(scroll_y)) <= threshold

    def _is_near_tail(self) -> bool:
        return self._is_near_tail_position(
            self.scroll_y,
            self.max_scroll_y,
            threshold=self.TAIL_FOLLOW_THRESHOLD,
        )

    def watch_scroll_y(self, old_value: float, new_value: float) -> None:
        super().watch_scroll_y(old_value, new_value)
        self._follow_tail = self._is_near_tail_position(
            new_value,
            self.max_scroll_y,
            threshold=self.TAIL_FOLLOW_THRESHOLD,
        )

    def request_tail_scroll(self, *, force: bool = False) -> None:
        """Follow new transcript content only when the user is already at the tail."""
        if force:
            self._follow_tail = True
        elif not self._follow_tail and not self._is_near_tail():
            return

        if self._tail_scroll_pending:
            return
        self._tail_scroll_pending = self.call_after_refresh(self._scroll_to_tail)

    def _scroll_to_tail(self) -> None:
        self._tail_scroll_pending = False
        if self._follow_tail or self._is_near_tail():
            self.scroll_end(animate=False, immediate=True)

    def add_user(self, text: str, *, timestamp: object | None = None) -> Bubble:
        # Multi-turn separator: thin ─ rule above each user message after
        # the first non-welcome content.
        if self._has_prior_content():
            self.mount(_TurnSeparator())
        b = Bubble("user", text, timestamp=timestamp)
        self.mount(b)
        self.request_tail_scroll(force=True)
        return b

    def _has_prior_content(self) -> bool:
        """True if there's any non-welcome content above this point."""
        for c in self.children:
            if isinstance(c, Bubble):
                cls = getattr(c, "classes", set())
                if "welcome-bubble" in (cls if isinstance(cls, set) else set()):
                    continue
                return True
        return False

    def start_assistant(self) -> Bubble:
        b = Bubble("assistant", "")
        self.mount(b)
        self.request_tail_scroll()
        return b

    def add_assistant(self, text: str) -> Bubble:
        b = Bubble("assistant", text)
        self.mount(b)
        self.request_tail_scroll()
        return b

    def add_system(self, text: str, *, collapse_long: bool = True) -> Bubble:
        b = Bubble("system", text, collapse_long=collapse_long)
        self.mount(b)
        self.request_tail_scroll()
        return b

    def add_slash_echo(self, text: str) -> Bubble:
        """Render the user's slash command as a muted echo bubble."""
        b = Bubble("slash", text)
        self.mount(b)
        self.request_tail_scroll(force=True)
        return b

    def add_error(self, text: str) -> Bubble:
        b = Bubble("error", text)
        self.mount(b)
        self.request_tail_scroll()
        return b

    def add_welcome(self, renderable) -> Static:
        widget = Static(renderable, classes="welcome-bubble", markup=False)
        self.mount(widget)
        self.request_tail_scroll()
        return widget

    def add_marker(self, text: str) -> Static:
        """Tiny one-line dim separator (resume notice, etc.).

        A full system Bubble is too visually heavy for transient
        notices like "resumed N messages from …" — the user reads it
        once at the start of a session and never again. Render as a
        single dim, centered Static so it sits quietly between the
        welcome and the first replayed user message.
        """
        widget = Static(text, classes="resume-marker", markup=False)
        self.mount(widget)
        self.request_tail_scroll()
        return widget

    def add_tool(
        self,
        tool_call_id: str,
        name: str,
        args: dict,
        *,
        bubble: Bubble | None = None,
    ) -> ToolLine:
        target = bubble or self.latest_assistant()
        line = target.add_tool(tool_call_id, name, args) if target else ToolLine(
            tool_call_id, name, _summarize_args(name, args), args
        )
        if target is None:
            self.mount(line)
        self.request_tail_scroll()
        return line

    def find_tool(self, tool_call_id: str) -> ToolLine | None:
        for child in self.children:
            if isinstance(child, ToolLine) and child.tool_call_id == tool_call_id:
                return child
            if isinstance(child, Bubble):
                line = child.find_tool(tool_call_id)
                if line is not None:
                    return line
        return None

    def latest_assistant(self) -> Bubble | None:
        for child in reversed(list(self.children)):
            if isinstance(child, Bubble) and getattr(child, "_role", "") == "assistant":
                return child
        return None
