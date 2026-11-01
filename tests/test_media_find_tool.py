"""``media_find``: the agent finds what it made earlier by its prompt (2026-11-01).

Asked for "the image we made earlier", the agent listed its media folder and
guessed, once pulling a frame from an unrelated video. The library keeps
every generated file with its prompt: the tool reads it.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from PIL import Image

from flowly.agent.tools.media_find import MediaFindTool
from flowly.media import library as library_mod
from flowly.media.assets import describe


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    (home / "media").mkdir(parents=True)
    monkeypatch.setenv("FLOWLY_HOME", str(home))
    library_mod.reset_library_cache()
    yield home
    library_mod.reset_library_cache()


def made(home: Path, name: str, prompt: str, color: tuple[int, int, int]) -> Path:
    path = home / "media" / name
    Image.new("RGB", (8, 8), color).save(path)
    library_mod.get_library().record([describe(path, provider="fal", model="openai/gpt-image-2", prompt=prompt)],
                                     source=library_mod.SOURCE_GENERATED)
    return path


def find(**kwargs) -> dict:
    return json.loads(asyncio.run(MediaFindTool().execute(**kwargs)))


def test_it_finds_an_image_by_words_of_its_prompt_with_the_path_to_give_it(home):
    made(home, "img-apple.png", "A glossy red apple on a white surface", (200, 0, 0))
    control = made(home, "img-control.png", "Flowly has control written on a foldable phone screen", (0, 0, 200))
    result = find(search="Flowly control")
    assert [item["path"] for item in result["found"]] == [str(control)]
    assert result["found"][0]["prompt"].startswith("Flowly has control")
    assert result["found"][0]["source"] == "generated"


def test_without_words_it_lists_the_latest_and_filters_by_kind(home):
    made(home, "img-1.png", "first", (1, 1, 1))
    made(home, "img-2.png", "second", (2, 2, 2))
    result = find(kind="image", limit=1)
    assert len(result["found"]) == 1 and result["total"] == 2
    assert find(kind="video")["found"] == []
