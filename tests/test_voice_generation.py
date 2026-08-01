"""Voice generation — speech and music from a provider we talk to directly.

What these cover is the band where a voice provider genuinely differs from the
media catalog: the voice and the model belong to the USER'S account, so nothing
about them can be defaulted, guessed, or looked up in a shared index. Almost
everything else — saving, retention, delivery, playback — is the media layer's,
already tested there, and identical whichever provider produced the sound.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from flowly.agent.tools.voice_generate import VoiceGenerateTool
from flowly.voice.providers import elevenlabs
from flowly.voice.settings import ElevenLabsSettings, resolve_elevenlabs


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path))
    return tmp_path


def _settings(**overrides) -> ElevenLabsSettings:
    base = {
        "enabled": True,
        "api_key": "sk_test",
        "voice_id": "voice-1",
        "model_id": "eleven_multilingual_v2",
        "music_model_id": "music_v2",
    }
    base.update(overrides)
    return ElevenLabsSettings(**base)


class _Cfg:
    """Stand-in for a config block — attribute access is all the resolver uses."""

    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


# ── readiness: every missing piece fails differently ────────────────────────


def test_speech_needs_a_voice_and_a_model_not_just_a_key():
    """Neither can be guessed on someone's behalf: a voice is a personal
    choice, and a model is a price."""
    assert _settings().speech_ready
    assert not _settings(voice_id="").speech_ready
    assert not _settings(model_id="").speech_ready
    assert not _settings(api_key="").speech_ready
    assert not _settings(enabled=False).speech_ready


def test_music_is_off_until_a_music_model_is_chosen():
    assert _settings().music_ready
    assert not _settings(music_model_id="").music_ready
    # Speech staying available is the point: the two are configured separately
    # and one being unset must not disable the other.
    assert _settings(music_model_id="").speech_ready


def test_a_key_inherited_from_the_phone_card_does_not_switch_generation_on():
    """The old voice card's key was entered for phone calls. Reading it here so
    nobody re-types it is a courtesy; treating it as consent to start
    generating audio would be a surprise."""
    resolved = resolve_elevenlabs(
        _Cfg(
            elevenlabs=_Cfg(enabled=False, api_key="", voice_id="", model_id="",
                            music_model_id=""),
            voice=_Cfg(elevenlabs_api_key="sk_old", tts_voice="voice-old"),
        )
    )
    assert resolved.api_key == "sk_old"
    assert resolved.voice_id == "voice-old"
    assert resolved.from_legacy_voice_config
    assert resolved.configured
    assert not resolved.enabled
    assert not resolved.speech_ready


def test_the_standalone_connection_wins_over_the_legacy_one():
    resolved = resolve_elevenlabs(
        _Cfg(
            elevenlabs=_Cfg(enabled=True, api_key="sk_new", voice_id="voice-new",
                            model_id="m", music_model_id=""),
            voice=_Cfg(elevenlabs_api_key="sk_old", tts_voice="voice-old"),
        )
    )
    assert (resolved.api_key, resolved.voice_id) == ("sk_new", "voice-new")
    assert not resolved.from_legacy_voice_config


# ── the provider: it is the account's own library, not a catalog ────────────


def _transport(handler):
    return httpx.MockTransport(handler)


@pytest.fixture
def mock_http(monkeypatch):
    """Swap httpx.AsyncClient for one wired to a handler the test provides."""

    def install(handler):
        real = httpx.AsyncClient

        def factory(*args, **kwargs):
            kwargs["transport"] = _transport(handler)
            return real(*args, **kwargs)

        monkeypatch.setattr(elevenlabs.httpx, "AsyncClient", factory)

    return install


async def test_voices_come_from_the_users_own_library(mock_http):
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["key"] = request.headers.get("xi-api-key")
        return httpx.Response(200, json={"voices": [
            {"voice_id": "v1", "name": "Ayşe", "category": "cloned",
             "labels": {"accent": "turkish"}},
            {"voice_id": "v2", "name": "Rex", "category": "premade"},
            {"name": "no id — dropped"},
        ]})

    mock_http(handler)
    options = await elevenlabs.list_voices("sk_test", "ay")

    assert [o.value for o in options] == ["v1", "v2"]
    assert options[0].label == "Ayşe"
    # The description is what lets somebody tell two voices apart.
    assert "cloned" in options[0].description and "turkish" in options[0].description
    assert "search=ay" in seen["url"]
    assert seen["key"] == "sk_test"


async def test_models_that_cannot_speak_are_not_offered(mock_http):
    """Offering one would produce a failure at generation time for a choice
    that looked perfectly valid when it was made."""
    def handler(_request):
        return httpx.Response(200, json=[
            {"model_id": "eleven_v3", "name": "v3", "can_do_text_to_speech": True,
             "languages": [{"language_id": "tr"}, {"language_id": "en"}]},
            {"model_id": "conv_1", "name": "Converter", "can_do_voice_conversion": True},
        ])

    mock_http(handler)
    options = await elevenlabs.list_speech_models("sk_test")

    assert [o.value for o in options] == ["eleven_v3"]
    assert "2 languages" in options[0].description


async def test_a_rejected_key_says_to_replace_it(mock_http):
    mock_http(lambda _r: httpx.Response(401, json={"detail": "nope"}))

    with pytest.raises(elevenlabs.ElevenLabsAuthError, match="Replace it"):
        await elevenlabs.list_voices("sk_bad")


async def test_no_key_fails_before_any_request_is_made(mock_http):
    def handler(_request):  # pragma: no cover - must never run
        raise AssertionError("a keyless call must not reach the network")

    mock_http(handler)
    with pytest.raises(elevenlabs.ElevenLabsAuthError):
        await elevenlabs.list_voices("")
    with pytest.raises(elevenlabs.ElevenLabsAuthError):
        await elevenlabs.list_music_models("")


# ── generation ──────────────────────────────────────────────────────────────


async def test_speech_is_posted_to_the_chosen_voice(mock_http):
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["format"] = request.url.params.get("output_format")
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, content=b"ID3audio")

    mock_http(handler)
    data = await elevenlabs.synthesize_speech(
        "sk_test", text="Merhaba", voice_id="v1", model_id="eleven_v3"
    )

    assert data == b"ID3audio"
    assert seen["path"] == "/v1/text-to-speech/v1"
    assert seen["body"] == {"text": "Merhaba", "model_id": "eleven_v3"}
    assert seen["format"] == elevenlabs.DEFAULT_OUTPUT_FORMAT


async def test_a_music_length_beyond_the_limit_is_clamped_not_refused(mock_http):
    """Somebody asking for twelve minutes wants the longest piece available,
    not an error about a limit they had no way of knowing."""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, content=b"song")

    mock_http(handler)
    await elevenlabs.compose_music("sk_test", prompt="a slow piano piece",
                                   length_ms=12 * 60 * 1000, model_id="music_v2")

    assert seen["body"]["music_length_ms"] == elevenlabs.MAX_MUSIC_MS
    assert seen["body"]["model_id"] == "music_v2"


async def test_an_empty_response_is_an_error_not_a_silent_zero_byte_file(mock_http):
    mock_http(lambda _r: httpx.Response(200, content=b""))

    with pytest.raises(elevenlabs.ElevenLabsError, match="empty"):
        await elevenlabs.synthesize_speech("sk_test", text="hi", voice_id="v1",
                                           model_id="m")


async def test_an_error_body_is_read_before_the_stream_is_abandoned(mock_http):
    """Otherwise the reason is lost and every failure reads the same."""
    mock_http(lambda _r: httpx.Response(422, json={"detail": "bad voice"}))

    with pytest.raises(elevenlabs.ElevenLabsError, match="invalid"):
        await elevenlabs.compose_music("sk_test", prompt="x", model_id="music_v2")


# ── saving ──────────────────────────────────────────────────────────────────


def test_the_extension_follows_the_format_the_provider_was_asked_for(isolated_home):
    from flowly.voice.generate import save_audio

    assert save_audio(b"x", output_format="mp3_44100_128").suffix == ".mp3"
    assert save_audio(b"x", output_format="opus_48000_128").suffix == ".opus"
    assert save_audio(b"x", output_format="pcm_16000").suffix == ".wav"


def test_a_half_written_file_never_appears_finished(isolated_home):
    """The rename is what makes this true; the test pins it so a refactor to a
    direct write does not quietly reintroduce partial files."""
    from flowly.media.generate import media_dir
    from flowly.voice.generate import save_audio

    path = save_audio(b"audio-bytes", output_format="mp3_44100_128", stem="take")

    assert path.name == "take.mp3"
    assert path.read_bytes() == b"audio-bytes"
    assert not list(media_dir().glob(".*.part"))


# ── the tool ────────────────────────────────────────────────────────────────


def _patch_generate(monkeypatch, tmp_path, captured=None, error=None):
    import flowly.voice.generate as gen

    async def fake(settings, *, mode, prompt, length_seconds=0, instrumental=False):
        if error is not None:
            raise error
        if captured is not None:
            captured.update({"mode": mode, "prompt": prompt,
                             "length_seconds": length_seconds,
                             "instrumental": instrumental,
                             "voice": settings.voice_id})
        from flowly.media.assets import describe

        out = tmp_path / "speech.mp3"
        out.write_bytes(b"\x00" * 64)
        asset = describe(out, provider="elevenlabs", model="m", probe_media=False)
        return gen.VoiceResult(assets=[asset], provider="elevenlabs", model="m")

    monkeypatch.setattr(gen, "generate_elevenlabs", fake)


async def test_the_file_rides_the_reply(monkeypatch, tmp_path):
    captured: dict = {}
    _patch_generate(monkeypatch, tmp_path, captured)

    result = await VoiceGenerateTool(elevenlabs=_settings()).execute(prompt="Merhaba")

    parsed = json.loads(result)
    assert parsed["_reply_media"] == [str(tmp_path / "speech.mp3")]
    assert parsed["_reply_media_assets"][0]["kind"] == "audio"
    # Without the path in the summary the model has no idea the file exists and
    # answers "where is it?" by inventing something.
    assert str(tmp_path / "speech.mp3") in parsed["summary"]
    assert captured["mode"] == "speech"


async def test_music_mode_carries_its_length_and_instrumental_flag(monkeypatch, tmp_path):
    captured: dict = {}
    _patch_generate(monkeypatch, tmp_path, captured)

    await VoiceGenerateTool(elevenlabs=_settings()).execute(
        prompt="a slow piano piece", mode="music", length_seconds=45, instrumental=True
    )

    assert captured["mode"] == "music"
    assert captured["length_seconds"] == 45
    assert captured["instrumental"] is True


async def test_no_provider_says_so_rather_than_failing_obscurely():
    out = await VoiceGenerateTool(elevenlabs=None).execute(prompt="Merhaba")
    assert "no voice provider is configured" in out

    out = await VoiceGenerateTool(elevenlabs=_settings(api_key="")).execute(prompt="hi")
    assert "no voice provider is configured" in out


async def test_a_failure_reads_as_a_tool_error_and_attaches_nothing(monkeypatch, tmp_path):
    from flowly.voice.generate import VoiceGenerationError

    _patch_generate(monkeypatch, tmp_path, error=VoiceGenerationError("the model failed"))
    out = await VoiceGenerateTool(elevenlabs=_settings()).execute(prompt="Merhaba")

    assert out.startswith("Voice generation failed:")
    assert "_reply_media" not in out


def test_the_schema_never_exposes_the_voice_or_the_model():
    """Both are chosen once, on the connection, from the user's own account.
    Letting the agent override them mid-conversation would mean hearing a
    different narrator than the one you picked."""
    props = VoiceGenerateTool().parameters["properties"]
    assert set(props) == {"prompt", "mode", "length_seconds", "instrumental"}

    blob = json.dumps(VoiceGenerateTool().parameters)
    for leak in ("voice_id", "model_id", "elevenlabs", "xi-api-key"):
        assert leak not in blob


# ── the picker surface ──────────────────────────────────────────────────────


def _picker(source: str, query: str = "", home: Path | None = None):
    import asyncio

    from flowly.channels import feature_rpc

    return asyncio.run(
        feature_rpc.dispatch("picker.options", {"source": source, "query": query})
    )[0]


def test_a_picker_that_cannot_open_still_opens_and_says_why(isolated_home):
    """An empty picker that explains itself beats one that throws: the field
    underneath is a plain text input either way, and a thrown error would leave
    the dialog with nothing at all in it."""
    result = _picker("elevenlabs.voices")

    assert result["options"] == []
    assert result["error"]
    assert result["source"] == "elevenlabs.voices"


def test_an_unknown_source_is_reported_not_raised(isolated_home):
    result = _picker("someprovider.things")

    assert result["options"] == []
    assert "unknown picker source" in result["error"]


def test_the_card_asks_for_pickers_the_bot_can_actually_serve(isolated_home):
    """The card names its picker sources as opaque strings and the clients hand
    them straight back untouched, so a typo here would surface as an empty
    dropdown on three platforms and be visible nowhere else."""
    from flowly.integrations.registry import get_card

    card = get_card("elevenlabs")
    assert card is not None and card.category == "voice"

    sources = [f.picker for f in card.fields if f.picker]
    assert sources, "the ElevenLabs card must offer pickers"

    for picker in sources:
        assert picker.startswith("options:"), picker
        result = _picker(picker.split(":", 1)[1])
        # With no key configured every source fails — but on the KEY, which is
        # the user's problem to fix, never on the routing, which would be ours.
        assert "unknown picker source" not in (result["error"] or ""), picker


def test_audio_is_never_handed_to_the_image_compressor(isolated_home, monkeypatch):
    """A sound file has nothing to look at. Asking for a preview anyway fed an
    mp3 to Pillow once per file, to be told what we already knew."""
    from flowly.gateway import server

    def explode(_path):  # pragma: no cover - must never run
        raise AssertionError("audio must not reach the thumbnailer")

    monkeypatch.setattr(server, "_thumbnail_b64", explode)

    clip = isolated_home / "speech.mp3"
    clip.write_bytes(b"\x00" * 32)
    asset = type("A", (), {"kind": "audio", "poster_path": ""})()

    assert server._inline_preview_b64(asset, clip) is None
