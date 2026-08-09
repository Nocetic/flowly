"""The CLI must say when it is compacting.

``flowly agent`` runs the agent in-process, and the compaction notification
hook is wired by the GATEWAY — so on the CLI it was never attached at all.
Automatic compaction therefore ran silently: the conversation stalled for many
seconds mid-turn with nothing on screen to say why, and afterwards nothing to
say it had happened.

These pin the wiring rather than the pixels: the render needs a live terminal,
but what matters is that the hook exists, that it is keyed by cycle, and that
it stays quiet while the user is already watching a manual /compact.
"""

from __future__ import annotations

import inspect

from flowly.cli import agent_cmd


def _source() -> str:
    return inspect.getsource(agent_cmd)


def test_the_cli_attaches_the_compaction_hook():
    assert "agent_loop._on_compaction = _on_cli_compaction" in _source(), (
        "automatic compaction is invisible on the CLI again"
    )


def test_the_running_phase_shows_a_spinner():
    """A compaction takes seconds to minutes. Without a live indicator the CLI
    just stops responding."""
    source = _source()

    assert "console.status(" in source
    assert 'spinner="dots"' in source


def test_a_terminal_only_closes_its_own_cycle():
    """Same rule as every other surface: an event from another pass must not
    close the notice on screen and report its own numbers."""
    assert "compaction_id != cycle" in _source()


def test_a_manual_compact_is_not_narrated_twice():
    """/compact prints its own progress and result."""
    source = _source()

    assert '_manual_compact_in_flight["value"] = True' in source
    assert source.count('if _manual_compact_in_flight["value"]:') >= 2


def test_a_failed_compaction_is_said_out_loud():
    """Otherwise the spinner just vanishes and the user is left guessing."""
    source = _source()

    assert "context compaction failed" in source
    assert "history kept" in source


def test_clear_goes_through_the_one_reset_entry_point():
    """It bumps the context epoch, which is what stops a compaction already in
    flight from committing the old conversation over the cleared one."""
    source = _source()

    assert "agent_loop.reset_conversation(session_id)" in source
    assert "session.reset_conversation_context()" not in source, (
        "the CLI reset bypasses the epoch again"
    )
