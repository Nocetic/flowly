"""The video_generate tool and the generation pipeline behind it.

Two things matter most here. First, the tool's contract is provider-neutral —
no fal field names leak into what the model sees, so switching endpoints is a
config change rather than a prompt change. Second, the provider's output URL is
untrusted input fetched on the user's own machine, so downloading it is treated
with the suspicion that deserves.
"""

from __future__ import annotations

import json

import pytest

from flowly.agent.tools.video_generate import VideoGenerateTool
from flowly.media.catalog import COMPAT_READY, COMPAT_UNSUPPORTED, MediaModel
from flowly.media.generate import GenerationError, download_output

SCHEMA = {
    "required": ["prompt"],
    "properties": {
        "prompt": {"type": "string"},
        "duration": {"enum": ["5", "10"]},
        "aspect_ratio": {"enum": ["16:9", "9:16", "1:1"]},
        "start_image_url": {"type": "string"},
    },
}

T2V = MediaModel(
    "fal-ai/kling-video/v3/standard/text-to-video",
    "Kling v3",
    "text-to-video",
    compatibility=COMPAT_READY,
    input_schema=SCHEMA,
)
I2V = MediaModel(
    "fal-ai/kling-video/v3/standard/image-to-video",
    "Kling v3 i2v",
    "image-to-video",
    compatibility=COMPAT_READY,
    input_schema=SCHEMA,
)


class FakeCatalog:
    def __init__(self, models):
        self._models = {m.endpoint_id: m for m in models}

    async def get(self, endpoint_id, *, with_schema=False):
        return self._models.get(endpoint_id)

    async def list_models(self, *, category=None, force=False):
        return [m for m in self._models.values() if not category or m.category == category]


def _tool(**kwargs):
    return VideoGenerateTool(
        api_key="key",
        text_to_video_model=T2V.endpoint_id,
        image_to_video_model=I2V.endpoint_id,
        catalog=FakeCatalog([T2V, I2V]),
        **kwargs,
    )


def _patch_generation(monkeypatch, tmp_path, *, captured=None, error=None):
    """Stand in for the provider: record the payload, produce a local file."""
    import flowly.media.generate as generate

    async def fake_generate_video(*, api_key, model, request, explicit=frozenset(), **kwargs):
        if error is not None:
            raise error
        if captured is not None:
            captured.update({
                "api_key": api_key,
                "model": model.endpoint_id,
                "request": dict(request),
                "explicit": set(explicit),
            })
        from flowly.media.assets import describe

        clip = tmp_path / "clip.mp4"
        clip.write_bytes(b"\x00" * 128)
        asset = describe(clip, provider="fal", model=model.endpoint_id, probe_media=False)
        return generate.GenerationResult(
            assets=[asset], model=model.endpoint_id, request_id="req-1"
        )

    monkeypatch.setattr(generate, "generate_video", fake_generate_video)


# ── tool contract ───────────────────────────────────────────────────────────


def test_the_schema_exposes_no_provider_field_names():
    props = _tool().parameters["properties"]
    assert set(props) == {
        "prompt", "mode", "input_image", "duration_seconds", "aspect_ratio",
        "resolution", "generate_audio", "negative_prompt", "model",
    }
    blob = json.dumps(_tool().parameters)
    for provider_name in ("start_image_url", "fal", "queue.fal.run"):
        assert provider_name not in blob


async def test_a_generated_clip_rides_the_reply(monkeypatch, tmp_path):
    _patch_generation(monkeypatch, tmp_path)
    result = await _tool().execute(prompt="a cat on a skateboard")

    parsed = json.loads(result)
    assert parsed["_reply_media"] == [str(tmp_path / "clip.mp4")]
    assert "attached to this reply" in parsed["summary"]
    # The descriptors ride along so the delivery layer can build a player.
    assert parsed["_reply_media_assets"][0]["kind"] == "video"


async def test_the_configured_default_model_is_used(monkeypatch, tmp_path):
    captured: dict = {}
    _patch_generation(monkeypatch, tmp_path, captured=captured)
    await _tool().execute(prompt="a cat")

    assert captured["model"] == T2V.endpoint_id
    assert captured["request"]["prompt"] == "a cat"


async def test_image_to_video_switches_model_and_passes_the_still(monkeypatch, tmp_path):
    captured: dict = {}
    _patch_generation(monkeypatch, tmp_path, captured=captured)
    await _tool().execute(
        prompt="pan across it", mode="image_to_video", input_image="https://x/y.png"
    )

    assert captured["model"] == I2V.endpoint_id
    assert captured["request"]["input_image"] == "https://x/y.png"
    assert "input_image" in captured["explicit"]


async def test_only_fields_the_caller_set_count_as_explicit(monkeypatch, tmp_path):
    """A default nobody asked for must not turn into a hard requirement."""
    captured: dict = {}
    _patch_generation(monkeypatch, tmp_path, captured=captured)
    await _tool().execute(prompt="a cat", aspect_ratio="9:16")

    assert captured["explicit"] == {"aspect_ratio"}
    assert captured["request"]["duration_seconds"] == 5  # defaulted, not explicit


async def test_an_explicit_audio_request_is_marked_explicit(monkeypatch, tmp_path):
    captured: dict = {}
    _patch_generation(monkeypatch, tmp_path, captured=captured)
    await _tool().execute(prompt="a cat", generate_audio=False)

    assert captured["request"]["generate_audio"] is False
    assert "generate_audio" in captured["explicit"]


# ── refusals ────────────────────────────────────────────────────────────────


async def test_image_to_video_without_an_image_is_refused():
    assert "input_image" in await _tool().execute(prompt="x", mode="image_to_video")


async def test_a_local_path_is_not_accepted_as_the_still():
    out = await _tool().execute(
        prompt="x", mode="image_to_video", input_image="/etc/passwd"
    )
    assert "https" in out


async def test_an_unknown_mode_is_refused():
    assert "mode" in await _tool().execute(prompt="x", mode="text_to_gif")


async def test_an_empty_prompt_is_refused():
    assert "prompt" in await _tool().execute(prompt="   ")


async def test_no_configured_model_says_so_instead_of_guessing():
    """Picking a model for the user would spend their money on their behalf."""
    tool = VideoGenerateTool(api_key="key", catalog=FakeCatalog([T2V]))
    out = await tool.execute(prompt="a cat")
    assert "no text-to-video model is configured" in out


async def test_a_model_missing_from_the_catalog_is_refused():
    tool = VideoGenerateTool(
        api_key="key", text_to_video_model="ghost/model", catalog=FakeCatalog([T2V])
    )
    assert "not in the model catalog" in await tool.execute(prompt="a cat")


async def test_a_model_flowly_cannot_drive_is_refused():
    unsupported = MediaModel(
        "weird/model", "Weird", "text-to-video", compatibility=COMPAT_UNSUPPORTED
    )
    tool = VideoGenerateTool(
        api_key="key",
        text_to_video_model="weird/model",
        catalog=FakeCatalog([unsupported]),
    )
    assert "can't provide" in await tool.execute(prompt="a cat")


async def test_a_model_of_the_wrong_category_is_refused():
    tool = VideoGenerateTool(
        api_key="key", text_to_video_model=I2V.endpoint_id, catalog=FakeCatalog([I2V])
    )
    out = await tool.execute(prompt="a cat")
    assert "image-to-video model, not text-to-video" in out


async def test_a_generation_failure_reads_as_a_tool_error(monkeypatch, tmp_path):
    _patch_generation(monkeypatch, tmp_path, error=GenerationError("the model failed"))
    out = await _tool().execute(prompt="a cat")
    assert out.startswith("Video generation failed:")
    assert "_reply_media" not in out


# ── download safety ─────────────────────────────────────────────────────────


async def test_a_non_https_output_url_is_refused(tmp_path):
    for url in ("http://x/y.mp4", "file:///etc/passwd", "ftp://x/y.mp4", ""):
        with pytest.raises(GenerationError):
            await download_output(url, kind="video", dest_dir=tmp_path)


async def test_an_oversized_download_is_stopped_mid_stream(tmp_path, monkeypatch):
    """A missing or lying content-length must not become an unbounded write."""
    import flowly.media.generate as generate

    monkeypatch.setattr(generate, "MAX_DOWNLOAD_BYTES", 1024)

    class FakeResponse:
        status_code = 200
        headers = {"content-type": "video/mp4"}  # no content-length

        async def aiter_bytes(self):
            for _ in range(10):
                yield b"\x00" * 512

    class FakeStream:
        async def __aenter__(self):
            return FakeResponse()

        async def __aexit__(self, *_a):
            return False

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return False

        def stream(self, *_a, **_k):
            return FakeStream()

    monkeypatch.setattr(generate.httpx, "AsyncClient", lambda **_k: FakeClient())

    with pytest.raises(GenerationError, match="too large"):
        await download_output("https://x/y.mp4", kind="video", dest_dir=tmp_path)
    # Nothing partial is left behind for the rest of the pipeline to pick up.
    assert list(tmp_path.iterdir()) == []


async def test_a_wrong_content_type_is_refused(tmp_path, monkeypatch):
    import flowly.media.generate as generate

    class FakeResponse:
        status_code = 200
        headers = {"content-type": "text/html; charset=utf-8"}

        async def aiter_bytes(self):
            yield b"<html>"

    class FakeStream:
        async def __aenter__(self):
            return FakeResponse()

        async def __aexit__(self, *_a):
            return False

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return False

        def stream(self, *_a, **_k):
            return FakeStream()

    monkeypatch.setattr(generate.httpx, "AsyncClient", lambda **_k: FakeClient())

    with pytest.raises(GenerationError, match="text/html"):
        await download_output("https://x/y.mp4", kind="video", dest_dir=tmp_path)
    assert list(tmp_path.iterdir()) == []


async def test_a_good_download_lands_with_the_right_extension(tmp_path, monkeypatch):
    import flowly.media.generate as generate

    class FakeResponse:
        status_code = 200
        headers = {"content-type": "video/mp4", "content-length": "6"}

        async def aiter_bytes(self):
            yield b"abcdef"

    class FakeStream:
        async def __aenter__(self):
            return FakeResponse()

        async def __aexit__(self, *_a):
            return False

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return False

        def stream(self, *_a, **_k):
            return FakeStream()

    monkeypatch.setattr(generate.httpx, "AsyncClient", lambda **_k: FakeClient())

    path = await download_output("https://x/y.mp4", kind="video", dest_dir=tmp_path)
    assert path.suffix == ".mp4"
    assert path.read_bytes() == b"abcdef"
    assert not path.name.startswith("."), "the temp name must not survive"


# ── post-generation pruning ─────────────────────────────────────────────────
#
# Pruning only at gateway start meant an always-on agent could run for weeks
# past its budget and then delete a month of history in one go at the next
# restart. Doing it after each generation keeps the folder inside its budget
# continuously — but it must never delete the clip that triggered it.


async def test_a_new_clip_survives_the_prune_it_triggers(tmp_path, monkeypatch):
    from flowly.media.generate import prune_after_generation

    media = tmp_path / "media"
    media.mkdir()
    clip = media / "vid-new.mp4"
    clip.write_bytes(b"\x00" * (3 * 1024 * 1024))
    poster = media / "vid-new.jpg"
    poster.write_bytes(b"\x00" * 1024)

    import flowly.media.generate as generate
    from flowly.config.schema import Config

    monkeypatch.setattr(generate, "media_dir", lambda: media)
    # A budget the new clip alone blows past.
    config = Config()
    config.media.retention.video_max_size_mb = 1
    config.media.retention.retention_days = -1
    monkeypatch.setattr("flowly.config.loader.load_config", lambda *a, **k: config)

    await prune_after_generation([str(clip)])

    assert clip.exists(), "the generation that triggered the prune must survive it"
    assert poster.exists(), "and so must its poster"


async def test_the_prune_still_reclaims_older_media(tmp_path, monkeypatch):
    import os
    import time

    from flowly.media.generate import prune_after_generation

    media = tmp_path / "media"
    media.mkdir()
    old = media / "vid-old.mp4"
    old.write_bytes(b"\x00" * (2 * 1024 * 1024))
    stale = time.time() - 90 * 86_400
    os.utime(old, (stale, stale))
    fresh = media / "vid-new.mp4"
    fresh.write_bytes(b"\x00" * 1024)

    import flowly.media.generate as generate
    from flowly.config.schema import Config

    monkeypatch.setattr(generate, "media_dir", lambda: media)
    monkeypatch.setattr("flowly.config.loader.load_config", lambda *a, **k: Config())

    await prune_after_generation([str(fresh)])

    assert not old.exists()
    assert fresh.exists()


async def test_pruning_never_breaks_a_reply(tmp_path, monkeypatch):
    """Reclaiming disk is housekeeping; a failure in it must not surface as a
    failed generation."""
    from flowly.media.generate import prune_after_generation

    def _explode(*_a, **_k):
        raise OSError("disk on fire")

    monkeypatch.setattr("flowly.config.loader.load_config", _explode)
    await prune_after_generation(["/tmp/whatever.mp4"])  # must not raise


async def test_disabled_retention_prunes_nothing(tmp_path, monkeypatch):
    import os
    import time

    from flowly.media.generate import prune_after_generation

    media = tmp_path / "media"
    media.mkdir()
    ancient = media / "vid-ancient.mp4"
    ancient.write_bytes(b"\x00" * 1024)
    stale = time.time() - 400 * 86_400
    os.utime(ancient, (stale, stale))

    import flowly.media.generate as generate
    from flowly.config.schema import Config

    config = Config()
    config.media.retention.enabled = False
    monkeypatch.setattr(generate, "media_dir", lambda: media)
    monkeypatch.setattr("flowly.config.loader.load_config", lambda *a, **k: config)

    await prune_after_generation([])

    assert ancient.exists()
