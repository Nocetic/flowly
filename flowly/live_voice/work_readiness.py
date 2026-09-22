"""Read-only task conversation readiness from its canonical owner record."""
from __future__ import annotations

from flowly.board.store import Card
from flowly.live_voice.authority import RequestOwner, request_owner_scope
from flowly.session.manager import session_file_lock
from flowly.session.ownership import require_session_file
from flowly.utils.helpers import safe_filename


def work_session_state(card: Card) -> str:
    """Call only after the Board has authorized the card and conversation.

    A queued card does not prove that the target conversation exists yet.
    Inspecting it must not start a profile, reserve a session or execute work.
    """
    from flowly.profile import describe_profile

    if card.execution_mode != 'voice' or card.session_key != f'desktop:voice-work:{card.id}':
        return 'unavailable'
    try:
        before = describe_profile(card.assignee_profile)
        if not card.assignee_bot_id or before.bot_id != card.assignee_bot_id:
            return 'unavailable'
        path = before.path / 'sessions' / (safe_filename(card.session_key.replace(':', '_')) + '.jsonl')
        with request_owner_scope(RequestOwner(card.voice_owner_uid or None)):
            if path.exists() or path.with_name(path.stem + '.full.jsonl').exists():
                with session_file_lock(path):
                    metadata = require_session_file(path, card.session_key, allow_missing_work=True)
            else:
                # Avoid creating a session directory or lock for a queued task.
                # A concurrent first reservation will be observed on refresh.
                metadata = None
        after = describe_profile(card.assignee_profile)
        if after.bot_id != before.bot_id or after.path != before.path:
            return 'unavailable'
        return 'ready' if metadata is not None else 'not_created'
    except Exception:
        # Readiness reveals no private content or underlying filesystem errors.
        return 'unavailable'
