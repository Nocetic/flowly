"""One-time discoverability hints — fire each prompt at most once per profile.

The TUI surfaces several non-obvious shortcuts (``/retry``, queue-while-busy,
Ctrl+C abort, F3 approvals queue) that a brand-new user has no way to
discover without reading the help modal. We surface them with
contextual first-touch prompts instead: the first time you hit the
relevant state, a one-line dim hint appears in the transcript; we mark
it seen, never show it again.

State persists in ``~/.flowly/tui_state.json`` under the
``first_touch_seen`` key — same file the TUI already uses for draft
restore and last-session-key, so we don't add a new I/O path.
"""

from __future__ import annotations

from flowly.tui.state import load_state, save_state

# Stable IDs — adding a new hint? pick a fresh slug; deleting a hint?
# leave the slug in this list so old "seen" markers are harmless.
HINT_FIRST_TURN = "first_turn_complete"
HINT_FIRST_TOOL = "first_tool_run"
HINT_FIRST_APPROVAL = "first_approval"
HINT_FIRST_CLEAR = "first_clear"

# Body text — kept terse so the dim transcript line doesn't dominate.
# Style is up to the renderer; here we only own the copy.
HINT_TEXT: dict[str, str] = {
    HINT_FIRST_TURN: (
        "💡 Tip · type [b]/retry[/] for another go with the same prompt, "
        "or [b]/undo[/] to delete this turn"
    ),
    HINT_FIRST_TOOL: (
        "💡 Tip · streaming · [b]Ctrl+C[/] aborts the turn (doesn't quit) · "
        "[b]F3[/] shows pending approvals"
    ),
    HINT_FIRST_APPROVAL: (
        "💡 Tip · approval list: [b]↑/↓[/] choose · [b]Enter[/] select · "
        "[b]Esc[/] deny · or press [b]F3[/] for the queue"
    ),
    HINT_FIRST_CLEAR: (
        "💡 Tip · pass [b]/clear --yes[/] (or [b]now[/]) next time to "
        "skip the confirmation prompt"
    ),
}


def is_seen(hint_id: str) -> bool:
    """``True`` if the prompt has already fired this profile."""
    state = load_state()
    seen = state.get("first_touch_seen") or {}
    return bool(seen.get(hint_id))


def mark_seen(hint_id: str) -> None:
    """Persist that the prompt fired. Idempotent on repeat calls."""
    state = load_state()
    seen = dict(state.get("first_touch_seen") or {})
    if seen.get(hint_id):
        return
    seen[hint_id] = True
    state["first_touch_seen"] = seen
    save_state(state)


def get_text(hint_id: str) -> str | None:
    """Return the hint body, or ``None`` for an unknown id."""
    return HINT_TEXT.get(hint_id)
