from __future__ import annotations

import asyncio
import inspect
import json
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from flowly.agent.loop import AgentLoop
from flowly.cli import gateway_cmd
from flowly.config.loader import load_config
from flowly.config.schema import Config
from flowly.exec.process_checkpoint import ProcessCheckpoint
from flowly.exec.process_registry import ProcessRegistry, ProcessSession
from flowly.integrations.active_provider import resolve_named_provider


def test_goal_config_defaults_and_camel_case_loading(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "agents": {
                    "defaults": {
                        "goals": {
                            "maxTurns": 44,
                            "judgeProvider": "anthropic",
                            "judgeModel": "judge-model",
                            "judgeTimeoutSeconds": 18,
                            "judgeMaxTokens": 2048,
                            "gateTimeoutSeconds": 90,
                            "gateMaxRetries": 5,
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    goals = load_config(path).agents.defaults.goals

    assert goals.enabled is True
    assert goals.max_turns == 44
    assert goals.judge_provider == "anthropic"
    assert goals.judge_model == "judge-model"
    assert goals.judge_timeout_seconds == 18
    assert goals.judge_max_tokens == 2048
    assert goals.gate_timeout_seconds == 90
    assert goals.gate_max_retries == 5


def test_named_goal_judge_provider_does_not_apply_active_provider_cascade() -> None:
    config = Config.model_validate(
        {
            "providers": {
                "active": "openrouter",
                "openrouter": {"api_key": "router-key"},
                "anthropic": {"api_key": "judge-key"},
            }
        }
    )

    judge = resolve_named_provider(config, "anthropic")

    assert judge is not None
    assert judge.key == "anthropic"
    assert judge.api_key == "judge-key"
    assert resolve_named_provider(config, "missing") is None


def _reload_harness() -> AgentLoop:
    loop = object.__new__(AgentLoop)
    loop._explicit_goal_provider = None
    loop.goal_manager = SimpleNamespace(
        judge=SimpleNamespace(
            provider=None,
            model="old",
            timeout_seconds=1,
            max_tokens=64,
        ),
        default_max_turns=1,
    )
    loop.goal_commands = SimpleNamespace(
        gate_timeout_seconds=1,
        gate_max_retries=0,
    )
    return loop


def test_goal_judge_hot_reload_tracks_inherited_main_provider() -> None:
    config = Config.model_validate(
        {
            "agents": {
                "defaults": {
                    "goals": {
                        "max_turns": 44,
                        "judge_timeout_seconds": 18,
                        "judge_max_tokens": 2048,
                        "gate_timeout_seconds": 90,
                        "gate_max_retries": 5,
                    }
                }
            }
        }
    )
    loop = _reload_harness()
    provider = object()

    result = loop.reload_goal_configuration(
        config,
        main_provider=provider,  # type: ignore[arg-type]
        main_model="new-main-model",
    )

    assert loop.goal_manager.judge.provider is provider
    assert loop.goal_manager.judge.model is None
    assert loop.goal_manager.judge.timeout_seconds == 18
    assert loop.goal_manager.judge.max_tokens == 2048
    assert loop.goal_manager.default_max_turns == 44
    assert loop.goal_commands.gate_timeout_seconds == 90
    assert loop.goal_commands.gate_max_retries == 5
    assert result["inheritsMainProvider"] is True


def test_goal_judge_hot_reload_rebuilds_named_provider(monkeypatch) -> None:
    config = Config.model_validate(
        {
            "agents": {
                "defaults": {
                    "goals": {
                        "judge_provider": "anthropic",
                        "judge_model": "judge-model",
                    }
                }
            }
        }
    )
    loop = _reload_harness()
    main_provider = object()
    named_provider = object()
    build = Mock(return_value=named_provider)
    monkeypatch.setattr("flowly.providers.factory.build_named_provider", build)

    result = loop.reload_goal_configuration(
        config,
        main_provider=main_provider,  # type: ignore[arg-type]
        main_model="main-model",
    )

    build.assert_called_once_with(
        config,
        "anthropic",
        default_model="judge-model",
    )
    assert loop.goal_manager.judge.provider is named_provider
    assert loop.goal_manager.judge.model == "judge-model"
    assert result["inheritsMainProvider"] is False


def test_goal_judge_hot_reload_preserves_explicit_injected_provider(monkeypatch) -> None:
    config = Config.model_validate(
        {
            "agents": {
                "defaults": {
                    "goals": {"judge_provider": "anthropic"},
                }
            }
        }
    )
    loop = _reload_harness()
    explicit_provider = object()
    loop._explicit_goal_provider = explicit_provider
    build = Mock()
    monkeypatch.setattr("flowly.providers.factory.build_named_provider", build)

    loop.reload_goal_configuration(
        config,
        main_provider=object(),  # type: ignore[arg-type]
        main_model="main-model",
    )

    assert loop.goal_manager.judge.provider is explicit_provider
    build.assert_not_called()


def test_gateway_provider_reload_refreshes_goal_judge() -> None:
    assert "agent.reload_goal_configuration(" in inspect.getsource(gateway_cmd)


def test_process_summary_contains_goal_wait_evidence() -> None:
    session = ProcessSession(
        id="proc_1",
        command="pytest -q",
        session_key="web:chat",
        started_at=time.time() - 3,
        cwd="/tmp/project",
        pid=42,
        output_buffer="\x1b[31mtest output\x1b[0m",
        watch_patterns=["passed"],
        notify_on_complete=False,
        watch_hit=True,
        watch_hit_at=123.0,
        watch_hit_pattern="passed",
    )

    summary = session.to_summary()

    assert summary["session_id"] == "proc_1"
    assert summary["watch_patterns"] == ["passed"]
    assert summary["watch_hit"] is True
    assert summary["watch_hit_pattern"] == "passed"
    assert summary["notify_on_complete"] is False
    assert summary["output_preview"] == "test output"


@pytest.mark.asyncio
async def test_first_watch_match_releases_barrier_and_notifies_once() -> None:
    registry = ProcessRegistry()
    session = ProcessSession(
        id="proc_1",
        command="server",
        session_key="web:chat",
        started_at=time.time(),
        watch_patterns=["ready"],
    )
    registry._running[session.id] = session
    events: list[tuple[str, str | None, str]] = []
    registry.subscribe_trigger(lambda sid, owner, kind: events.append((sid, owner, kind)))

    assert registry.wait_barrier_active("proc_1") is True
    await registry._check_watch_patterns(session, "server ready\n")
    await registry._check_watch_patterns(session, "server ready again\n")

    assert registry.wait_barrier_active("proc_1") is False
    assert events == [("proc_1", "web:chat", "watch_match")]


@pytest.mark.asyncio
async def test_process_exit_notifies_even_when_chat_notice_is_disabled() -> None:
    registry = ProcessRegistry()
    events: list[tuple[str, str | None, str]] = []

    async def callback(session_id: str, owner: str | None, trigger: str) -> None:
        events.append((session_id, owner, trigger))

    registry.subscribe_trigger(callback)
    session = await registry.spawn(
        "exit 0",
        session_key="web:chat",
        notify_on_complete=False,
    )
    await asyncio.wait_for(session._exit_event.wait(), timeout=2)
    await asyncio.sleep(0)

    assert events == [(session.id, "web:chat", "exit")]
    assert registry.wait_barrier_active(session.id) is False


def test_process_checkpoint_preserves_watch_hit(tmp_path: Path, monkeypatch) -> None:
    checkpoint = ProcessCheckpoint(tmp_path / "processes.json")
    session = ProcessSession(
        id="proc_1",
        command="server",
        session_key="web:chat",
        started_at=10,
        pid=4321,
        watch_patterns=["ready"],
        watch_hit=True,
        watch_hit_at=12,
        watch_hit_pattern="ready",
    )
    checkpoint.save([session])
    monkeypatch.setattr("flowly.exec.process_checkpoint._is_pid_alive", lambda _pid: True)

    restored = checkpoint.recover()

    assert len(restored) == 1
    assert restored[0].watch_hit is True
    assert restored[0].watch_hit_at == 12
    assert restored[0].watch_hit_pattern == "ready"
