"""Asset descriptors must survive a session reload.

A reopened chat rebuilds its attachments from the session file. If only the
paths were persisted, history would have to re-probe every clip to learn its
duration — and on a host without ffmpeg it simply couldn't, so a video would
come back from history poorer than it was live. Persisting the descriptors is
what keeps the two renderings identical.
"""

from __future__ import annotations

import pytest

from flowly.media.assets import MediaAsset, assets_from_meta
from flowly.session.manager import SessionManager


@pytest.fixture
def flowly_home(tmp_path, monkeypatch):
    """Isolate ~/.flowly so sessions land in the test's own directory."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("FLOWLY_HOME", str(home))
    from flowly import profile

    if hasattr(profile, "_cached_home"):
        profile._cached_home = None
    yield home
    if hasattr(profile, "_cached_home"):
        profile._cached_home = None


def _manager(tmp_path):
    from pathlib import Path as _Path

    return SessionManager(workspace=_Path(tmp_path))


def _clip(tmp_path):
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"\x00" * 64)
    return path, MediaAsset(
        path=str(path),
        kind="video",
        file_name="clip.mp4",
        mime_type="video/mp4",
        size=64,
        width=1080,
        height=1920,
        duration_ms=6000,
        poster_path=str(tmp_path / "clip.jpg"),
        id="media_1",
    )


def test_assets_persist_on_the_closing_assistant_message(tmp_path, flowly_home):
    path, asset = _clip(tmp_path)
    sessions = _manager(tmp_path)
    session = sessions.get_or_create("web:conv-1")

    session.extend_with_turn_messages(
        user_content="make me a clip",
        new_messages=[{"role": "assistant", "content": "Here it is."}],
        final_content="Here it is.",
        reply_media=[str(path)],
        reply_media_assets=[asset],
    )
    sessions.save(session)

    reloaded = _manager(tmp_path).get_or_create("web:conv-1")
    closing = reloaded.messages[-1]
    assert closing["media"] == [str(path)]
    assert assets_from_meta(closing["media_assets"]) == [asset]


def test_assets_persist_when_the_turn_had_no_plain_assistant_message(tmp_path, flowly_home):
    """The capstone path (tool-only turn) must carry them too."""
    path, asset = _clip(tmp_path)
    sessions = _manager(tmp_path)
    session = sessions.get_or_create("web:conv-2")

    session.extend_with_turn_messages(
        user_content="make me a clip",
        new_messages=[],
        final_content="Done.",
        reply_media=[str(path)],
        reply_media_assets=[asset],
    )
    sessions.save(session)

    closing = sessions.get_or_create("web:conv-2").messages[-1]
    assert assets_from_meta(closing["media_assets"]) == [asset]


def test_a_turn_without_assets_writes_no_key(tmp_path, flowly_home):
    sessions = _manager(tmp_path)
    session = sessions.get_or_create("web:conv-3")
    session.extend_with_turn_messages(
        user_content="hi",
        new_messages=[{"role": "assistant", "content": "hello"}],
        final_content="hello",
    )
    assert "media_assets" not in session.messages[-1]


def test_assets_never_reach_the_llm_transcript(tmp_path, flowly_home):
    """Session files carry internal bookkeeping; provider payloads must not."""
    path, asset = _clip(tmp_path)
    sessions = _manager(tmp_path)
    session = sessions.get_or_create("web:conv-4")
    session.extend_with_turn_messages(
        user_content="clip please",
        new_messages=[{"role": "assistant", "content": "Here."}],
        final_content="Here.",
        reply_media=[str(path)],
        reply_media_assets=[asset],
    )
    for message in session.get_history():
        assert "media_assets" not in message
        assert "media" not in message


def test_gateway_history_rebuilds_a_playable_video_attachment(tmp_path, flowly_home):
    """End of the chain: what a reopened chat actually sends to a client."""
    from flowly.gateway.server import _assets_index, _reply_media_attachments

    path, asset = _clip(tmp_path)
    sessions = _manager(tmp_path)
    session = sessions.get_or_create("web:conv-5")
    session.extend_with_turn_messages(
        user_content="clip please",
        new_messages=[{"role": "assistant", "content": "Here."}],
        final_content="Here.",
        reply_media=[str(path)],
        reply_media_assets=[asset],
    )
    sessions.save(session)

    closing = _manager(tmp_path).get_or_create("web:conv-5").messages[-1]
    att = _reply_media_attachments(
        closing["media"], _assets_index(closing.get("media_assets"))
    )[0]

    assert att["kind"] == "video"
    assert att["mimeType"] == "video/mp4"
    assert att["durationMs"] == 6000
    assert att["mediaId"] == "clip.mp4"
