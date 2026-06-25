"""P2: memory_dreaming config wiring (defaults + camelCase round-trip)."""

from __future__ import annotations

from flowly.config.schema import AgentDefaults, MemoryDreamingConfig


def test_defaults_on_and_selective():
    cfg = MemoryDreamingConfig()
    assert cfg.enabled is True           # ON by default (auto-enables on update)
    assert cfg.auto_consolidate is True  # full-auto: background cleanup too
    assert cfg.commit_mode == "selective"
    assert cfg.auto_floor == 0.80
    assert cfg.review_floor == 0.55


def test_agent_defaults_includes_memory_dreaming():
    defs = AgentDefaults()
    assert isinstance(defs.memory_dreaming, MemoryDreamingConfig)
    assert defs.memory_dreaming.idle_minutes == 30


def test_camelcase_roundtrip():
    from flowly.config.loader import convert_keys, convert_to_camel

    camel = {"memoryDreaming": {"enabled": True, "dailyTime": "04:00", "autoFloor": 0.9}}
    snake = convert_keys(camel)  # converts nested AND outer keys
    cfg = MemoryDreamingConfig(**snake["memory_dreaming"])
    assert cfg.enabled is True
    assert cfg.daily_time == "04:00"
    assert cfg.auto_floor == 0.9
    back = convert_to_camel(cfg.model_dump())
    assert "dailyTime" in back and "autoFloor" in back
