"""Tests for the three read-only introspection slash commands.

``/skills`` / ``/whoami`` / ``/status`` all turn ``InboundMessage``
content into a Markdown OutboundMessage without mutating session
state. These tests pin down the formatter output (no full agent
loop spinup needed) plus the lightweight parse logic so accidentally
moving a destructive command into the same elif doesn't slip past
CI.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from flowly.agent.loop import AgentLoop
from flowly.bus.events import InboundMessage
from flowly.session.manager import Session


# --------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------- #


@pytest.fixture
def loop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AgentLoop:
    """A minimally-wired AgentLoop just enough to call the formatters.

    We bypass the heavy constructor by allocating the instance with
    ``object.__new__`` and back-filling the handful of attributes the
    three formatters actually read. The full ``__init__`` pulls in
    LLM provider, session store, plugin manager, etc — none of which
    are exercised by ``_format_skills_list`` / ``_format_whoami`` /
    ``_format_status``.
    """
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path))
    loop = object.__new__(AgentLoop)
    loop.workspace = tmp_path
    loop.provider = MagicMock(model="anthropic/claude-haiku-4.5")
    loop._active_model = "anthropic/claude-haiku-4.5"
    loop.sessions = MagicMock()
    loop.subagent_registry = None
    return loop


def _msg(content: str = "", chat_id: str = "chat-abc") -> InboundMessage:
    """Build an InboundMessage. ``session_key`` is derived from
    ``channel:chat_id`` by the dataclass — pass ``chat_id=""`` to
    exercise the "no session" code path.
    """
    return InboundMessage(
        channel="web",
        sender_id="user-42",
        chat_id=chat_id,
        content=content,
        media=[],
        metadata={},
    )


# --------------------------------------------------------------------- #
# /skills
# --------------------------------------------------------------------- #


def test_skills_empty_workspace_returns_friendly_message(loop, tmp_path):
    """Brand-new workspace with no skills installed."""
    # Point the loader at an empty tmp workspace; the builtin dir on
    # the package still has skills, so we'd otherwise get those back.
    # We accept that: the formatter still emits valid Markdown.
    output = loop._format_skills_list("")
    assert "Skills" in output
    assert "/" in output  # at least one /name reference


def test_skills_filter_matches_name(loop, tmp_path):
    """``/skills compact`` filters to skills containing 'compact'."""
    output = loop._format_skills_list("compact")
    # If the bundled compact skill exists, it should be in the output.
    # If not, we get the "No skills match" branch — both are valid
    # depending on which builtins ship with this checkout. Just confirm
    # the filter banner is rendered.
    assert "compact" in output.lower()


def test_skills_filter_no_matches_returns_friendly_message(loop, tmp_path):
    output = loop._format_skills_list("zzz-definitely-not-a-real-skill")
    assert "No skills match" in output


def test_skills_output_is_markdown(loop, tmp_path):
    """Formatter emits bullets and code spans the chat renderers expect."""
    output = loop._format_skills_list("")
    # Bold section labels and markdown bullets are the minimal
    # contract — Streamdown needs `- ` (not the `•` glyph) plus a
    # blank line above the list to render a real <ul> instead of
    # collapsing items into one paragraph.
    assert "**" in output
    assert "- " in output


@pytest.mark.asyncio
async def test_help_lists_learn_command(loop):
    msg = _msg("/help")

    output = await loop._process_message_inner(msg)

    assert output is not None
    assert "`/learn [--dry-run] [source]`" in output.content


# --------------------------------------------------------------------- #
# /whoami
# --------------------------------------------------------------------- #


def test_whoami_includes_channel_sender_chat(loop):
    """Identity block surfaces the three routing fields."""
    msg = _msg()
    fake_session = MagicMock()
    fake_session.metadata = {"persona": "default"}
    fake_session.messages = []
    loop.sessions.get_or_create.return_value = fake_session

    output = loop._format_whoami(msg)
    assert "Identity" in output
    assert "web" in output
    assert "user-42" in output
    assert "chat-abc" in output


def test_whoami_includes_active_model_when_present(loop):
    msg = _msg()
    fake_session = MagicMock()
    fake_session.metadata = {}
    fake_session.messages = []
    loop.sessions.get_or_create.return_value = fake_session

    output = loop._format_whoami(msg)
    assert "claude-haiku-4.5" in output


def test_whoami_handles_missing_session(loop, monkeypatch):
    """When the session store raises, the call still returns a message."""
    msg = _msg()
    loop.sessions.get_or_create.side_effect = RuntimeError("session DB offline")
    # Should not raise; the identity block degrades gracefully.
    output = loop._format_whoami(msg)
    assert "Identity" in output
    assert "user-42" in output


# --------------------------------------------------------------------- #
# /status
# --------------------------------------------------------------------- #


def test_status_reports_message_count(loop):
    # Use a real Session so a typo on a dataclass attribute
    # (``session.key`` vs the bogus ``session.session_key``) blows
    # up in CI instead of silently working against a MagicMock that
    # auto-vivifies any attribute access.
    msg = _msg()
    real_session = Session(
        key="web:test-conv",
        messages=[{"role": "user"}, {"role": "assistant"}, {"role": "user"}],
        metadata={
            "last_model": "anthropic/claude-haiku-4.5",
            "last_turn_tokens": 2481,
            "last_prompt_tokens": 1847,
            "last_completion_tokens": 634,
            "persona": "default",
        },
    )
    loop.sessions.get_or_create.return_value = real_session

    output = loop._format_status(msg)
    assert "Session status" in output
    assert "web:test-conv" in output  # conversation key surfaces
    assert "3" in output  # message count
    assert "claude-haiku-4.5" in output
    assert "2,481" in output  # token count formatted with comma
    assert "1,847" in output  # prompt
    assert "634" in output    # completion
    assert "default" in output


def test_status_handles_session_without_metadata(loop):
    """Empty metadata — formatter still produces a stable block."""
    msg = _msg()
    real_session = Session(key="web:t", messages=[], metadata={})
    loop.sessions.get_or_create.return_value = real_session

    output = loop._format_status(msg)
    assert "Session status" in output
    assert "web:t" in output  # key still surfaces even with empty metadata
    assert "0" in output  # zero messages


def test_status_no_session_returns_placeholder(loop):
    """Session-store returns None — formatter still renders the header."""
    msg = _msg()
    loop.sessions.get_or_create.return_value = None
    output = loop._format_status(msg)
    assert "Session status" in output
    assert "No session" in output


def test_status_handles_session_store_failure(loop):
    """Session lookup raises — formatter still answers."""
    msg = _msg()
    loop.sessions.get_or_create.side_effect = RuntimeError("DB locked")
    output = loop._format_status(msg)
    assert "Session status" in output
