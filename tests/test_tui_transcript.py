from __future__ import annotations

from rich.console import Console

from flowly.tui.panes.transcript import Bubble
from flowly.tui.panes.transcript import TranscriptPane
from flowly.tui.panes.transcript import ToolLine


def test_transcript_tail_position_allows_small_gap() -> None:
    assert TranscriptPane._is_near_tail_position(98, 100, threshold=2)
    assert not TranscriptPane._is_near_tail_position(97, 100, threshold=2)
    assert TranscriptPane._is_near_tail_position(120, 100, threshold=2)


def test_transcript_tail_scroll_skips_when_user_is_away(monkeypatch) -> None:
    transcript = TranscriptPane()
    scheduled = []

    monkeypatch.setattr(transcript, "_is_near_tail", lambda: False)
    monkeypatch.setattr(
        transcript,
        "call_after_refresh",
        lambda callback: scheduled.append(callback) or True,
    )

    transcript._follow_tail = False
    transcript.request_tail_scroll()

    assert scheduled == []


def test_transcript_tail_scroll_force_rearms_follow(monkeypatch) -> None:
    transcript = TranscriptPane()
    scheduled = []
    scrolls = []

    monkeypatch.setattr(transcript, "_is_near_tail", lambda: False)
    monkeypatch.setattr(
        transcript,
        "call_after_refresh",
        lambda callback: scheduled.append(callback) or True,
    )
    monkeypatch.setattr(transcript, "scroll_end", lambda **kwargs: scrolls.append(kwargs))

    transcript._follow_tail = False
    transcript.request_tail_scroll(force=True)
    scheduled[0]()

    assert transcript._follow_tail is True
    assert transcript._tail_scroll_pending is False
    assert scrolls == [{"animate": False, "immediate": True}]


def test_transcript_tail_scroll_coalesces_pending_refresh(monkeypatch) -> None:
    transcript = TranscriptPane()
    scheduled = []

    monkeypatch.setattr(transcript, "_is_near_tail", lambda: False)
    monkeypatch.setattr(
        transcript,
        "call_after_refresh",
        lambda callback: scheduled.append(callback) or True,
    )

    transcript.request_tail_scroll()
    transcript.request_tail_scroll()

    assert len(scheduled) == 1


def test_assistant_markdown_table_renders_without_background_fill() -> None:
    renderable = Bubble(
        "assistant",
        "| Metric | Value |\n|---|---:|\n| Revenue | 12.5% |\n",
    )._renderable()
    console = Console(width=80, force_terminal=True, color_system=None)
    segments = list(console.render(renderable))

    assert any("Metric" in segment.text for segment in segments)
    assert all(
        segment.style is None or segment.style.bgcolor is None
        for segment in segments
        if segment.text.strip()
    )


def test_long_system_messages_collapse_by_default() -> None:
    bubble = Bubble("system", "x" * (Bubble.LONG_SYSTEM_CHARS + 1))

    assert bubble._collapsed is True


def test_long_system_messages_can_opt_out_of_collapse() -> None:
    bubble = Bubble(
        "system",
        "https://auth.x.ai/oauth2/auth?" + ("x" * Bubble.LONG_SYSTEM_CHARS),
        collapse_long=False,
    )

    assert bubble._collapsed is False


def test_tool_line_running_animation_uses_status_prefix(monkeypatch) -> None:
    line = ToolLine("tc1", "read_file", "README.md")
    rendered = []

    monkeypatch.setattr(line, "update", rendered.append)

    line._render_running()
    line._tick()

    # Labels are Text objects now (never markup-parsed) — assert on .plain
    # and on the bold span carrying the tool name.
    assert rendered[0].plain.lstrip().startswith(f"{ToolLine.SPINNER_FRAMES[0]} ")
    assert rendered[1].plain.lstrip().startswith(f"{ToolLine.SPINNER_FRAMES[1]} ")
    assert "read_file" in rendered[0].plain


def test_tool_line_complete_replaces_animation_prefix(monkeypatch) -> None:
    line = ToolLine("tc1", "read_file", "README.md")
    rendered = []

    monkeypatch.setattr(line, "update", rendered.append)

    line.complete(True, 125, "")

    assert rendered[-1].plain.lstrip().startswith("✓ ")
    assert "read_file" in rendered[-1].plain
    assert "125ms" in rendered[-1].plain
    assert "details" in rendered[-1].plain


def test_tool_line_running_detail_shows_sanitized_args() -> None:
    line = ToolLine(
        "tc1",
        "web_fetch",
        "https://example.com",
        {"url": "https://example.com", "authorization": "Bearer secret"},
    )

    detail = line._detail_renderable()

    assert "status: running" in detail
    assert "https://example.com" in detail
    assert "Bearer secret" not in detail
    assert "redacted" in detail


def test_tool_line_complete_refreshes_open_detail(monkeypatch) -> None:
    line = ToolLine("tc1", "read_file", "README.md", {"path": "README.md"})
    updates = []

    class _FakeDetail:
        def update(self, renderable):
            updates.append(renderable)

    monkeypatch.setattr(line, "update", lambda _renderable: None)
    monkeypatch.setattr(line, "_find_detail_widget", lambda: _FakeDetail())

    line.complete(True, 125, '{"ok": true}')

    assert updates
    assert '"ok": true' in updates[-1]
    assert "status: running" not in updates[-1]
