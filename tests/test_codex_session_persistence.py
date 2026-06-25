"""End-to-end persistence test for codex_session metadata.

Proves that ``codex_thread_id`` and ``codex_reasoning_items`` survive a
``SessionManager`` round-trip through disk — the actual scenario the
user cares about: Flowly gateway restart while a long Codex thread is
in flight, then resume.

Other codex_session tests mock the SessionManager. This one uses the
real on-disk implementation against a temp FLOWLY_HOME so a regression
in the save / load path would be caught here.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest


@pytest.fixture
def temp_flowly_home(tmp_path, monkeypatch):
    """Point FLOWLY_HOME at a fresh temp directory for this test.

    SessionManager reads ``get_flowly_home() / "sessions"`` at __init__
    time, so we must set the env var BEFORE constructing one.
    """
    home = tmp_path / "flowly-home"
    home.mkdir()
    monkeypatch.setenv("FLOWLY_HOME", str(home))
    # Also clear the get_flowly_home cache if it uses one.
    from flowly import profile
    if hasattr(profile, "_cached_home"):
        profile._cached_home = None
    return home


def test_codex_thread_id_survives_disk_roundtrip(temp_flowly_home):
    """The full restart scenario:

    1. User runs codex turn → tool writes codex_thread_id + reasoning
       to session.metadata.
    2. AgentLoop calls sessions.save(session) — metadata hits disk.
    3. Flowly gateway crashes / restarts → process exits.
    4. New SessionManager comes up, user sends another codex message.
    5. get_or_create loads the session from disk; metadata still
       carries codex_thread_id + reasoning so the new CodexSession can
       resume the same Codex thread.
    """
    from flowly.session.manager import Session, SessionManager

    # --- Round 1: simulate a turn that produced a codex thread ---
    mgr1 = SessionManager(workspace=Path("/tmp"))
    session = mgr1.get_or_create("web:test-session-1")
    # Simulate what CodexSessionTool does after a successful turn.
    session.metadata["codex_thread_id"] = "thr_persistent_42"
    session.metadata["codex_reasoning_items"] = [
        {"itemId": "r0", "encryptedContent": "blob-AAA", "summary": "thinking about auth"},
        {"itemId": "r1", "encryptedContent": "blob-BBB", "summary": "writing tests"},
    ]
    session.add_message("user", "refactor the auth module")
    session.add_message("assistant", "Done.")
    mgr1.save(session)

    # Sanity: the JSONL file exists on disk.
    session_path = mgr1._get_session_path("web:test-session-1")
    assert session_path.exists(), "session file was not written to disk"

    # --- Restart: drop in-memory state, build a fresh manager ---
    mgr2 = SessionManager(workspace=Path("/tmp"))
    # Force a cache miss: a brand-new manager has an empty cache, so
    # get_or_create() must hit the loader. (Verified by sanity check
    # above and by mgr2 having no shared state with mgr1.)
    assert "web:test-session-1" not in mgr2._cache

    # --- Round 2: resume the session ---
    resumed = mgr2.get_or_create("web:test-session-1")

    # Metadata was preserved exactly.
    assert resumed.metadata["codex_thread_id"] == "thr_persistent_42"
    reasoning = resumed.metadata["codex_reasoning_items"]
    assert len(reasoning) == 2
    assert reasoning[0]["encryptedContent"] == "blob-AAA"
    assert reasoning[1]["encryptedContent"] == "blob-BBB"
    # Messages also restored (so the agent has full history).
    assert len(resumed.messages) == 2
    assert resumed.messages[0]["content"] == "refactor the auth module"


def test_codex_session_tool_seeds_session_from_persisted_metadata(temp_flowly_home):
    """The tool's _build_codex_session must read codex_thread_id and
    codex_reasoning_items from metadata and pass them to a fresh
    CodexSession via set_thread_id() + set_initial_reasoning_items().

    This is the OTHER half of the round-trip: after metadata is loaded
    from disk, the tool must hand it to the new CodexSession so Codex
    actually resumes the thread instead of starting a new one.
    """
    from flowly.session.manager import SessionManager
    from flowly.agent.tools.codex_session import CodexSessionTool
    from flowly.codex.session import CodexSession, CodexSessionConfig, TurnResult

    # Set up a manager + pre-populated session as if Round 1 happened.
    mgr = SessionManager(workspace=Path("/tmp"))
    session = mgr.get_or_create("web:resume-test")
    session.metadata["codex_thread_id"] = "thr_resume_xyz"
    session.metadata["codex_reasoning_items"] = [
        {"itemId": "rPrev", "encryptedContent": "carryover"},
    ]
    mgr.save(session)

    # Drop and reload to simulate a restart.
    mgr2 = SessionManager(workspace=Path("/tmp"))
    resumed_session = mgr2.get_or_create("web:resume-test")

    # Capture what _build_codex_session does with the metadata.
    captured = {"set_thread_id": [], "set_initial_reasoning_items": []}

    class _RecorderCodexSession:
        def __init__(self):
            self.reasoning_items = []
            self._retired = False
        def set_thread_id(self, tid):
            captured["set_thread_id"].append(tid)
        def set_initial_reasoning_items(self, items):
            captured["set_initial_reasoning_items"].append(list(items))
        @property
        def retired(self):
            return self._retired

    # Drive the tool's _build_codex_session directly with the loaded
    # metadata. We bypass execute() because we already test the full
    # path in other tests; here we want to assert the wiring contract.
    config = CodexSessionConfig(codex_bin="codex-stub")
    tool = CodexSessionTool(
        config=config,
        session_accessor=lambda sk: resumed_session.metadata,
        stream_resolver=lambda sk: None,
        session_store_get=lambda sk: None,
        session_store_set=lambda sk, sess: None,
        active_session_key_getter=lambda: "web:resume-test",
    )

    # Monkeypatch the real CodexSession constructor inside the tool's
    # builder so we don't actually try to spawn a subprocess.
    import flowly.agent.tools.codex_session as tool_module
    real_cls = tool_module.CodexSession
    tool_module.CodexSession = lambda *, config, approval_callback=None: _RecorderCodexSession()  # type: ignore[assignment]
    try:
        built = tool._build_codex_session(
            metadata=resumed_session.metadata, cwd_override=None,
        )
    finally:
        tool_module.CodexSession = real_cls

    # The persisted thread_id was handed to the new session.
    assert captured["set_thread_id"] == ["thr_resume_xyz"]
    # And the reasoning continuity blobs.
    assert len(captured["set_initial_reasoning_items"]) == 1
    assert captured["set_initial_reasoning_items"][0][0]["encryptedContent"] == "carryover"
