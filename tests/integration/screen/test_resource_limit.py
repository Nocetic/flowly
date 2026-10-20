"""Run separately in a disposable container with --memory=1g --memory-swap=1g."""

import os

import pytest

from flowly.desktop_runtime.authority import ScreenError
from flowly.desktop_runtime.launcher import ScreenRuntime

pytestmark = pytest.mark.skipif(os.environ.get("FLOWLY_SCREEN_RESOURCE_LIMIT_TEST") != "1",
                                reason="requires the explicitly memory-limited Linux runner")


def test_real_cgroup_limit_blocks_start_without_reserving_a_generation(tmp_path):
    directory = tmp_path / "not-created/desktop"
    report = ScreenRuntime.doctor(directory)
    assert not directory.exists()
    assert report["reason"] == "INSUFFICIENT_RESOURCES"
    assert report["resources"]["memory"]["availableBytes"] <= 1024**3
    assert "LOW_MEMORY" in report["resources"]["issues"]
    runtime = ScreenRuntime(directory)
    with pytest.raises(ScreenError, match="INSUFFICIENT_RESOURCES"):
        runtime.start()
    assert runtime.read_manifest() is None
    assert not runtime.authority.snapshot()["running"]
