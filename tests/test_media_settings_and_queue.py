"""Media settings resolution and the provider queue loop.

Settings: ``tools.imageGeneration`` shipped before video existed. The two config
shapes are merged at read time rather than migrated on disk, so an upgrade
cannot lose someone's key or their chosen model — and a rollback to an older
build still finds the config it wrote.

Queue: video takes minutes, so it goes through the provider's queue API rather
than a held-open request. The parts worth pinning are the ones that only show
up when something goes wrong — a transient blip, a hard rejection, a user
pressing Stop.
"""

from __future__ import annotations

import asyncio

import pytest

from flowly.config.schema import (
    ImageGenerationConfig,
    MediaGenerationConfig,
    MediaGenerationDefaults,
    ToolsConfig,
)
from flowly.media.fal_queue import (
    FalQueueCancelledError,
    FalQueueError,
    QueuedJob,
    wait_for_result,
)
from flowly.media.settings import resolve_media_settings

# ── settings ────────────────────────────────────────────────────────────────


def test_a_legacy_only_config_keeps_its_key_and_model():
    tools = ToolsConfig(
        image_generation=ImageGenerationConfig(
            enabled=True, api_key="legacy-key", model="fal-ai/flux/schnell"
        )
    )
    settings = resolve_media_settings(tools)

    assert settings.enabled is True
    assert settings.api_key == "legacy-key"
    assert settings.text_to_image == "fal-ai/flux/schnell"
    assert settings.image_ready is True
    # No video model was ever configured, so video stays off.
    assert settings.video_ready is False


def test_the_new_block_wins_field_by_field():
    tools = ToolsConfig(
        image_generation=ImageGenerationConfig(
            enabled=True, api_key="old", model="fal-ai/flux/schnell"
        ),
        media_generation=MediaGenerationConfig(
            enabled=True,
            api_key="new",
            defaults=MediaGenerationDefaults(text_to_video="vendor/t2v"),
        ),
    )
    settings = resolve_media_settings(tools)

    assert settings.api_key == "new"
    assert settings.text_to_video == "vendor/t2v"
    # …but an unset new field still falls back to the old one.
    assert settings.text_to_image == "fal-ai/flux/schnell"


def test_either_block_can_enable_it():
    """An upgrade must not silently switch off image generation that worked."""
    legacy_on = ToolsConfig(image_generation=ImageGenerationConfig(enabled=True, api_key="k"))
    assert resolve_media_settings(legacy_on).enabled is True

    new_on = ToolsConfig(media_generation=MediaGenerationConfig(enabled=True, api_key="k"))
    assert resolve_media_settings(new_on).enabled is True


def test_video_needs_a_chosen_model_not_just_a_key():
    tools = ToolsConfig(media_generation=MediaGenerationConfig(enabled=True, api_key="k"))
    settings = resolve_media_settings(tools)

    assert settings.image_ready is True
    assert settings.video_ready is False, "no default model — picking one spends the user's money"


def test_an_image_to_video_model_alone_enables_video():
    tools = ToolsConfig(
        media_generation=MediaGenerationConfig(
            enabled=True, api_key="k",
            defaults=MediaGenerationDefaults(image_to_video="vendor/i2v"),
        )
    )
    assert resolve_media_settings(tools).video_ready is True


def test_a_default_config_generates_nothing():
    settings = resolve_media_settings(ToolsConfig())
    assert settings.enabled is False
    assert settings.image_ready is False
    assert settings.video_ready is False


def test_an_out_of_the_box_image_model_is_still_supplied():
    tools = ToolsConfig(media_generation=MediaGenerationConfig(enabled=True, api_key="k"))
    assert resolve_media_settings(tools).text_to_image == "fal-ai/flux/dev"


# ── queue loop ──────────────────────────────────────────────────────────────

JOB = QueuedJob(
    request_id="req-1",
    status_url="https://queue/status",
    response_url="https://queue/result",
    cancel_url="https://queue/cancel",
)


@pytest.fixture(autouse=True)
def no_sleeping(monkeypatch):
    """Polling cadence is not what these tests are about."""
    async def instant(_seconds):
        return None

    monkeypatch.setattr(asyncio, "sleep", instant)


def _patch_queue(monkeypatch, *, states, result=None, cancels=None):
    """Drive the loop through a scripted sequence of provider states."""
    import flowly.media.fal_queue as queue

    pending = list(states)

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return False

    monkeypatch.setattr(queue.httpx, "AsyncClient", lambda **_k: FakeClient())

    async def fake_status(job, *, api_key, client):
        step = pending.pop(0)
        if isinstance(step, Exception):
            raise step
        return step

    async def fake_result(job, *, api_key, client):
        return result or {"video": {"url": "https://x/y.mp4"}}

    async def fake_cancel(job, *, api_key):
        if cancels is not None:
            cancels.append(job.request_id)
        return True

    monkeypatch.setattr(queue, "status", fake_status)
    monkeypatch.setattr(queue, "result", fake_result)
    monkeypatch.setattr(queue, "cancel", fake_cancel)


async def test_a_job_polls_until_it_completes(monkeypatch):
    seen: list[str] = []
    _patch_queue(
        monkeypatch,
        states=[{"status": "IN_QUEUE"}, {"status": "IN_PROGRESS"}, {"status": "COMPLETED"}],
    )
    out = await wait_for_result(JOB, api_key="k", on_state=seen.append)

    assert out == {"video": {"url": "https://x/y.mp4"}}
    assert seen == ["IN_QUEUE", "IN_PROGRESS", "COMPLETED"]


async def test_repeated_states_are_reported_once(monkeypatch):
    seen: list[str] = []
    _patch_queue(
        monkeypatch,
        states=[{"status": "IN_PROGRESS"}, {"status": "IN_PROGRESS"}, {"status": "COMPLETED"}],
    )
    await wait_for_result(JOB, api_key="k", on_state=seen.append)
    assert seen == ["IN_PROGRESS", "COMPLETED"]


async def test_a_provider_failure_becomes_an_error(monkeypatch):
    _patch_queue(monkeypatch, states=[{"status": "FAILED", "error": "content policy"}])
    with pytest.raises(FalQueueError, match="content policy"):
        await wait_for_result(JOB, api_key="k")


async def test_a_transient_blip_is_retried(monkeypatch):
    _patch_queue(
        monkeypatch,
        states=[
            FalQueueError("network error polling the job: reset"),
            {"status": "COMPLETED"},
        ],
    )
    assert await wait_for_result(JOB, api_key="k")


async def test_a_hard_rejection_is_not_retried(monkeypatch):
    """A provider that answers 4xx has decided; retrying only wastes time."""
    _patch_queue(monkeypatch, states=[FalQueueError("fal rejected the API key.")])
    with pytest.raises(FalQueueError, match="rejected the API key"):
        await wait_for_result(JOB, api_key="k")


async def test_persistent_network_failure_eventually_gives_up(monkeypatch):
    _patch_queue(
        monkeypatch,
        states=[FalQueueError("network error polling the job: down")] * 10,
    )
    with pytest.raises(FalQueueError, match="network error"):
        await wait_for_result(JOB, api_key="k")


async def test_stop_cancels_the_render_not_just_the_wait(monkeypatch):
    """Pressing Stop should stop the bill, not only stop us watching."""
    cancels: list[str] = []
    _patch_queue(monkeypatch, states=[{"status": "IN_PROGRESS"}], cancels=cancels)

    with pytest.raises(FalQueueCancelledError):
        await wait_for_result(JOB, api_key="k", should_cancel=lambda: True)
    assert cancels == ["req-1"]


async def test_a_provider_side_cancellation_is_reported_as_such(monkeypatch):
    _patch_queue(monkeypatch, states=[{"status": "CANCELLED"}])
    with pytest.raises(FalQueueCancelledError):
        await wait_for_result(JOB, api_key="k")


async def test_a_timeout_cancels_the_job(monkeypatch):
    cancels: list[str] = []
    _patch_queue(monkeypatch, states=[{"status": "IN_PROGRESS"}] * 5, cancels=cancels)

    with pytest.raises(FalQueueError, match="did not finish"):
        await wait_for_result(JOB, api_key="k", timeout_seconds=-1)
    assert cancels == ["req-1"], "an abandoned job must not keep rendering"


async def test_a_broken_progress_callback_does_not_break_the_job(monkeypatch):
    def boom(_state):
        raise RuntimeError("UI blew up")

    _patch_queue(monkeypatch, states=[{"status": "COMPLETED"}])
    assert await wait_for_result(JOB, api_key="k", on_state=boom)
