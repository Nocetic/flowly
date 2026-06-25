"""Tests for the TUI inline board formatter (/board)."""

from __future__ import annotations

from flowly.tui.app import _format_board


def _snap(total, columns):
    return {"total": total, "columns": columns}


def test_empty_board():
    out = _format_board(_snap(0, []))
    assert "no cards yet" in out


def test_groups_by_status_with_icons():
    snap = _snap(3, [
        {"status": "todo", "cards": [
            {"id": "c_1", "title": "buy milk", "originChannel": "telegram"},
            {"id": "c_2", "title": "call mom", "originChannel": "cli"},
        ]},
        {"status": "in_progress", "cards": []},
        {"status": "waiting", "cards": []},
        {"status": "done", "cards": [{"id": "c_3", "title": "ship it", "originChannel": "desktop"}]},
    ])
    out = _format_board(snap)
    # header + count
    assert "3 cards" in out
    # status sections present with icons
    assert "○ To do" in out
    assert "◐ In progress" in out
    assert "✓ Done" in out
    # card lines with ids
    assert "`c_1` buy milk" in out
    assert "`c_3` ship it" in out
    # real channel origin tagged, local (cli/desktop) not
    assert "_telegram_" in out
    assert "_cli_" not in out
    assert "_desktop_" not in out
    # empty section marked
    assert "_(empty)_" in out


def test_singular_card_count():
    out = _format_board(_snap(1, [{"status": "todo", "cards": [{"id": "c_1", "title": "x"}]}]))
    assert "1 card" in out and "1 cards" not in out
