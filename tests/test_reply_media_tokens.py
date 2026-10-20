"""``MEDIA:/path`` in a final reply becomes a real attachment on every channel.

The channel hints tell the agent to attach a file it made with the token
(prompt_blocks.py). Only the iMessage adapter ever read it; everywhere else the
reply showed the raw server path (a Screen screenshot on Desktop, 2026-10-20).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import flowly.profile as profiles
from flowly.agent.loop import AgentLoop
from flowly.agent.reply_media import deliverable_media_path, media_token_roots, publish_reply_file, take_media_tokens
from flowly.bus.events import InboundMessage
from flowly.bus.queue import MessageBus
from flowly.config.schema import Config
from flowly.providers.base import LLMProvider, LLMResponse
from flowly.session.manager import SessionManager


def _file(path, data=b"\xff\xd8\xff jpeg"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _image(path, size=(1440, 900)):
    from PIL import Image

    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, (20, 120, 220)).save(path, "JPEG")
    return path


def test_a_token_becomes_an_attachment_and_leaves_the_text(tmp_path):
    shot = _file(tmp_path / "home" / "screenshots" / "browser-1.jpg")
    text, accepted, refused = take_media_tokens(f"X is open.\n\nMEDIA:{shot}\n\nShall I sign in?", [tmp_path / "home"])
    assert text == "X is open.\n\nShall I sign in?"
    assert accepted == [str(shot.resolve())]
    assert refused == []


def test_backticks_trailing_punctuation_and_repeats(tmp_path):
    shot = _file(tmp_path / "shot.png")
    text, accepted, _ = take_media_tokens(f"Here: `MEDIA:{shot}`. Again (MEDIA:{shot}).", [tmp_path])
    assert text == "Here: . Again ()."
    assert accepted == [str(shot.resolve())]


@pytest.mark.parametrize("name, data, reason", [
    ("missing.jpg", None, "not found"),
    ("config.json", b"{}", "unsupported type application/json"),
    ("page.html", b"<script>", "unsupported type text/html"),
    ("id_rsa", b"-----BEGIN", "unsupported type unknown"),
    ("empty.png", b"", "empty"),
])
def test_only_sendable_files_are_attached(tmp_path, name, data, reason):
    path = tmp_path / name
    if data is not None:
        _file(path, data)
    text, accepted, refused = take_media_tokens(f"See MEDIA:{path}", [tmp_path])
    assert accepted == []
    assert refused == [(str(path), reason)]
    # A refused path never stays for a channel adapter to send after all.
    assert "MEDIA:" not in text and str(path) not in text


def test_the_owners_other_files_are_refused_even_through_a_link(tmp_path):
    home = tmp_path / "home"
    photo = _file(tmp_path / "Pictures" / "family.jpg")
    link = home / "screenshots" / "innocent.jpg"
    link.parent.mkdir(parents=True)
    link.symlink_to(photo)
    for raw in (str(photo), str(link)):
        assert deliverable_media_path(raw, [home.resolve()]) == (None, "outside the agent's files")


def test_size_limits_follow_the_kind_of_file(tmp_path):
    big = tmp_path / "big.png"
    big.write_bytes(b"0" * (10 * 1024 * 1024 + 1))
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"0" * (10 * 1024 * 1024 + 1))
    assert deliverable_media_path(str(big), [tmp_path.resolve()]) == (None, "too large")
    assert deliverable_media_path(str(clip), [tmp_path.resolve()])[0] == clip.resolve()


def test_roots_are_the_agents_home_workspace_and_temp(tmp_path, monkeypatch):
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    roots = media_token_roots(tmp_path / "work")
    assert (tmp_path / "home").resolve() in roots
    assert (tmp_path / "work").resolve() in roots


class _Reply(LLMProvider):
    def __init__(self, content: str) -> None:
        super().__init__(api_key="test")
        self.content = content

    def get_default_model(self) -> str:
        return "test/model"

    async def chat(self, *args: Any, **kwargs: Any) -> LLMResponse:
        return LLMResponse(content=self.content)


@pytest.mark.asyncio
async def test_a_reply_naming_its_screenshot_delivers_it_and_keeps_history_clean(tmp_path, monkeypatch):
    default = tmp_path / "flowly"
    monkeypatch.setattr(profiles, "_DEFAULT_HOME", default)
    monkeypatch.setattr(profiles, "_PROFILES_ROOT", default / "profiles")
    home = profiles.create_profile("rhody")
    monkeypatch.setenv("FLOWLY_HOME", str(home))
    shot = _image(home / "screenshots" / "browser-20261019-225233-670793.jpg")
    reply = f"X is open — the sign-in page.\n\nMEDIA:{shot}\n\nTake control on the screen to sign in."
    loop = AgentLoop(bus=MessageBus(), provider=_Reply(reply), workspace=home / "workspace",
                     main_config=Config(), max_iterations=2, soft_warn_at_iteration=0)
    try:
        out = await loop._process_message(InboundMessage(channel="telegram", sender_id="owner", chat_id="1",
                                                         content="open x.com and send me a screenshot"))
    finally:
        loop.stop()
    assert out is not None
    assert out.content == "X is open — the sign-in page.\n\nTake control on the screen to sign in."
    # Attached from the media directory, where the clients fetch the original:
    # the screenshot opens at full size, not as its blown-up preview.
    published = home.resolve() / "media" / shot.name
    assert out.media == [str(published)]
    assert published.samefile(shot)
    from flowly.media.serving import resolve_media_id

    assert resolve_media_id(shot.name, home / "media")[0] == published
    saved = [m for m in SessionManager(home / "workspace").get_full_messages("telegram:1") if m.get("role") == "assistant"]
    assert "MEDIA:" not in saved[-1]["content"]
    assert saved[-1]["media"] == [str(published)]
    from flowly.media.library import get_library

    item = get_library(home).get(shot.name)
    assert item is not None and item["source"] == "generated" and item["width"] == 1440


def test_imessage_sends_tokens_by_the_same_rule(tmp_path, monkeypatch):
    from flowly.agent import reply_media
    from flowly.channels.imessage import IMessageChannel

    home = tmp_path / "home"
    shot = _file(home / "screenshots" / "shot.jpg")
    photo = _file(tmp_path / "Pictures" / "family.jpg")
    monkeypatch.setattr(reply_media, "media_token_roots", lambda workspace=None: [home.resolve()])
    text, paths = IMessageChannel._extract_media_tags(f"Here MEDIA:{shot} and MEDIA:{photo}")
    assert text == "Here  and"
    assert paths == [str(shot.resolve())]


def test_publishing_links_into_the_media_dir_without_overwriting(tmp_path):
    media = tmp_path / "home" / "media"
    shot = _file(tmp_path / "home" / "screenshots" / "browser-1.jpg")
    first = publish_reply_file(str(shot), media)
    assert first == str(media.resolve() / "browser-1.jpg")
    assert Path(first).samefile(shot)
    # Attached again (another turn): the same published file.
    assert publish_reply_file(str(shot), media) == first
    # Another file with the same name never replaces it.
    other = _file(tmp_path / "work" / "browser-1.jpg", b"\xff\xd8\xff other")
    second = publish_reply_file(str(other), media)
    assert second != first and Path(second).parent == media.resolve() and Path(second).samefile(other)
    assert Path(first).read_bytes() == shot.read_bytes()


def test_media_already_servable_keeps_its_path(tmp_path):
    media = tmp_path / "media"
    generated = _file(media / "image-1.png")
    assert publish_reply_file(str(generated), media) == str(generated)
    assert publish_reply_file("https://cdn.example/x.png", media) == "https://cdn.example/x.png"
    # A file that vanished keeps its path; the preview is all there is.
    assert publish_reply_file(str(tmp_path / "gone.jpg"), media) == str(tmp_path / "gone.jpg")


@pytest.mark.asyncio
async def test_a_media_tools_screenshot_is_published_for_full_size_viewing(tmp_path, monkeypatch):
    from flowly.agent.reply_media import media_envelope
    from flowly.agent.tools.base import Tool
    from flowly.providers.base import ToolCallRequest

    default = tmp_path / "flowly"
    monkeypatch.setattr(profiles, "_DEFAULT_HOME", default)
    monkeypatch.setattr(profiles, "_PROFILES_ROOT", default / "profiles")
    home = profiles.create_profile("lovelace")
    monkeypatch.setenv("FLOWLY_HOME", str(home))
    shot = _image(home / "screenshots" / "browser-20261020-020000-abcdef.jpg")

    class Shot(Tool):
        name = "screenshot"
        description = "test"
        parameters = {"type": "object", "properties": {}}

        async def execute(self, **kwargs: Any) -> str:
            return media_envelope([str(shot)], "Browser screenshot captured, attached to this reply.")

    class Script(LLMProvider):
        def __init__(self) -> None:
            super().__init__(api_key="test")
            self.calls = 0

        def get_default_model(self) -> str:
            return "test/model"

        async def chat(self, *args: Any, **kwargs: Any) -> LLMResponse:
            self.calls += 1
            if self.calls == 1:
                return LLMResponse(content=None, tool_calls=[ToolCallRequest(id="s1", name="screenshot", arguments={})])
            return LLMResponse(content="Here it is.")

    loop = AgentLoop(bus=MessageBus(), provider=Script(), workspace=home / "workspace",
                     main_config=Config(), max_iterations=3, soft_warn_at_iteration=0)
    loop.tools.register(Shot())
    try:
        out = await loop._process_message(InboundMessage(channel="telegram", sender_id="owner", chat_id="2",
                                                         content="send me a screenshot"))
    finally:
        loop.stop()
    published = home.resolve() / "media" / shot.name
    assert out is not None and out.media == [str(published)] and published.samefile(shot)
