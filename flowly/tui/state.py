"""Persistent TUI state — last-used session, assistant, etc.

Stored at ``~/.flowly/tui_state.json``. All fields optional; load() always
returns a dict (empty if file missing or malformed). Writes are best-effort:
disk failures degrade silently rather than killing the app.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

STATE_PATH = Path.home() / ".flowly" / "tui_state.json"


def load_state() -> dict[str, Any]:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state: dict[str, Any]) -> None:
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except OSError:
        pass


def fresh_session_key() -> str:
    """Generate a unique session key for a brand-new TUI session.

    Format: ``cli:{uuid4}``. The ``cli:`` prefix is non-negotiable — the agent
    loop derives the outbound channel from it; a colon-less key would split one
    logical session across two disk files on resume. The id is a plain uuid4,
    matching every other Flowly client (desktop/iOS) so brand-new sessions share
    a single id scheme across the apps.

    Backward compatible: existing ``cli:{ts}-{hex}`` / ``cli:tui-…`` / raw
    ``tui-…`` keys keep resolving via :func:`canonical_session_key` and the
    app-layer raw-key fallback — only NEW sessions get the uuid4 form.
    """
    return f"cli:{uuid.uuid4()}"


def canonical_session_key(key: str) -> str:
    """Promote a legacy raw key (no channel prefix) to ``cli:…`` form.

    Idempotent: keys that already contain a ``:`` are passed through
    unchanged. Used at TUI startup to migrate keys persisted by older
    builds — both the original raw ``tui-…`` keys and any keys saved
    by tooling that forgot the prefix — so resume-after-upgrade still
    finds the real session file."""
    if not key:
        return key
    if ":" in key:
        return key
    return f"cli:{key}"
