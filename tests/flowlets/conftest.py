"""Shared fixtures for flowlet tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

FIXTURE_DIR = Path(__file__).parent.parent / "fixtures" / "flowlets"
FIXTURE_NAMES = ["water", "habits", "mood", "pomodoro"]


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURE_DIR / f"{name}.json").read_text(encoding="utf-8"))


@pytest.fixture
def water_def() -> dict:
    return load_fixture("water")


@pytest.fixture
def store(tmp_path):
    from flowly.flowlets.store import FlowletStore
    s = FlowletStore(tmp_path / "flowlets.sqlite")
    yield s
    s.close()
