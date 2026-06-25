"""Assistant registry + tool tests (P2.8).

Covers the four invariants the markdown agent registry must preserve:

  1. BUILTIN SEED — writer/researcher/coder always available, even
     with an empty assistants directory.
  2. DISK PARSING — malformed frontmatter is rejected with a clear
     reason; valid files populate the catalogue.
  3. USER OVERRIDE — a ``writer.md`` file supplied by the user
     replaces the builtin and is flagged in the reload report.
  4. TOOL FILTERING — an assistant's ``allowed_tools`` actually limits
     what the child subagent can register; blocked tool names can
     NEVER make it in even if the user lists them.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from flowly.agent.assistants import (
    Assistant,
    AssistantRegistry,
    _BUILTIN_ASSISTANTS,
    _parse_markdown,
)
from flowly.agent.tools.builtin_agent import BuiltinAgentTool
from flowly.agent.subagent import SubagentManager
from flowly.agent.subagent_registry import SubagentRegistry
from flowly.bus.queue import MessageBus
from flowly.providers.base import LLMProvider, LLMResponse


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_md(dirpath: Path, name: str, body: str) -> Path:
    dirpath.mkdir(parents=True, exist_ok=True)
    p = dirpath / f"{name}.md"
    p.write_text(body, encoding="utf-8")
    return p


class _CannedProvider(LLMProvider):
    """Returns whatever LLMResponse was last queued — or a tool call."""

    def __init__(self, response: LLMResponse) -> None:
        super().__init__(api_key="x")
        self._response = response
        self.calls = 0

    def get_default_model(self) -> str:  # type: ignore[override]
        return "fake/model"

    async def chat(self, *a: Any, **kw: Any) -> LLMResponse:  # type: ignore[override]
        self.calls += 1
        return self._response


# ---------------------------------------------------------------------------
# 1. Builtin seed
# ---------------------------------------------------------------------------


class TestBuiltinSeed:
    def test_three_builtins_load_from_empty_dir(self, tmp_path: Path) -> None:
        reg = AssistantRegistry(tmp_path / "assistants")  # dir doesn't exist
        names = reg.names()
        assert names == ["coder", "researcher", "writer"]
        for name in names:
            a = reg.get(name)
            assert a is not None and a.builtin is True

    def test_builtin_assistants_do_not_auto_save(self) -> None:
        # Regression test for the 2026-04-23 change: builtin ``writer``
        # and ``researcher`` used to default to
        # ``auto_save_artifact=True``, which created unwanted artifacts
        # on every ``builtin_agent(writer|researcher)`` call. Artifact
        # save is now opt-in — the output cap (>10K chars) still
        # routes to the artifact store for context protection, but
        # that's a different code path from this flag. Pin the new
        # default here so a future "but we used to..." patch has to
        # edit the test along with the registry.
        reg = AssistantRegistry(Path("/nonexistent/never/created"))
        w = reg.get("writer")
        assert w is not None
        assert w.auto_save_artifact is False
        r = reg.get("researcher")
        assert r is not None and r.auto_save_artifact is False
        c = reg.get("coder")
        assert c is not None and c.auto_save_artifact is False


# ---------------------------------------------------------------------------
# 2. Disk parsing
# ---------------------------------------------------------------------------


class TestFrontmatterParsing:
    def test_valid_markdown_loads(self, tmp_path: Path) -> None:
        p = _write_md(tmp_path / "assistants", "seo", (
            "---\n"
            "name: seo-analyst\n"
            "description: SEO audit\n"
            "model: anthropic/haiku\n"
            "allowed_tools: [web_fetch, web_search]\n"
            "auto_save_artifact: true\n"
            "---\n"
            "You are an SEO specialist."
        ))
        asst, err = _parse_markdown(p)
        assert err is None
        assert asst is not None
        assert asst.name == "seo-analyst"
        assert asst.description == "SEO audit"
        assert asst.model == "anthropic/haiku"
        assert asst.allowed_tools == frozenset({"web_fetch", "web_search"})
        assert asst.auto_save_artifact is True
        assert asst.system_prompt == "You are an SEO specialist."
        assert asst.builtin is False
        assert asst.source_path == p

    def test_timeout_seconds_in_frontmatter_is_ignored(self, tmp_path: Path) -> None:
        # Legacy .md files may still carry a `timeout_seconds:` line.
        # Parser must accept + silently ignore it (the wall-clock cap
        # is a system-wide constant now; users don't pick it).
        p = _write_md(tmp_path / "assistants", "legacy", (
            "---\n"
            "name: legacy\n"
            "description: old file\n"
            "model: x/y\n"
            "timeout_seconds: 9999\n"
            "---\nhi"
        ))
        asst, err = _parse_markdown(p)
        assert err is None and asst is not None
        assert asst.name == "legacy"
        assert not hasattr(asst, "timeout_seconds")

    def test_missing_frontmatter_rejected(self, tmp_path: Path) -> None:
        p = _write_md(tmp_path / "assistants", "bare", "just a body, no frontmatter")
        asst, err = _parse_markdown(p)
        assert asst is None
        assert err is not None and "frontmatter" in err.lower()

    def test_missing_required_field_rejected(self, tmp_path: Path) -> None:
        p = _write_md(tmp_path / "assistants", "nomodel", (
            "---\n"
            "name: nomodel\n"
            "description: ...\n"
            "---\nbody"
        ))
        asst, err = _parse_markdown(p)
        assert asst is None
        assert err is not None and "model" in err

    def test_invalid_name_rejected(self, tmp_path: Path) -> None:
        p = _write_md(tmp_path / "assistants", "badname", (
            "---\n"
            "name: Has Spaces And CAPS\n"
            "description: ...\n"
            "model: x/y\n"
            "---\nbody"
        ))
        asst, err = _parse_markdown(p)
        assert asst is None
        assert err is not None and "invalid name" in err.lower()

    def test_malformed_yaml_rejected(self, tmp_path: Path) -> None:
        p = _write_md(tmp_path / "assistants", "broken", (
            "---\n"
            "name: ok\n"
            "description: also ok\n"
            "model: [this: is {not valid yaml\n"
            "---\nbody"
        ))
        asst, err = _parse_markdown(p)
        assert asst is None
        assert err is not None and "yaml" in err.lower()

    def test_reload_collects_errors_but_keeps_valid_files(
        self, tmp_path: Path,
    ) -> None:
        assistants_dir = tmp_path / "assistants"
        _write_md(assistants_dir, "good", (
            "---\n"
            "name: good\n"
            "description: ok\n"
            "model: x/y\n"
            "---\nbody"
        ))
        _write_md(assistants_dir, "bad", "no frontmatter here")
        reg = AssistantRegistry(assistants_dir)
        names = reg.names()
        # 3 builtins + 1 valid custom = 4 (the bad one is dropped)
        assert "good" in names
        assert "bad" not in names
        report = reg.last_report()
        assert report is not None
        assert any("bad" in e["file"] for e in report.errors)


# ---------------------------------------------------------------------------
# 3. User override
# ---------------------------------------------------------------------------


class TestUserOverride:
    def test_user_writer_overrides_builtin(self, tmp_path: Path) -> None:
        _write_md(tmp_path / "assistants", "writer", (
            "---\n"
            "name: writer\n"
            "description: MY CUSTOM WRITER\n"
            "model: custom/model\n"
            "---\nMy custom system prompt."
        ))
        reg = AssistantRegistry(tmp_path / "assistants")
        w = reg.get("writer")
        assert w is not None
        assert w.description == "MY CUSTOM WRITER"
        assert w.model == "custom/model"
        # Builtin flag flips to False because the authoritative copy is
        # now disk-sourced — makes the UI able to show "Reset to default".
        assert w.builtin is False
        report = reg.last_report()
        assert report is not None
        assert "writer" in report.overridden


# ---------------------------------------------------------------------------
# 4. Tool filtering at subagent level
# ---------------------------------------------------------------------------


class TestAssistantToolFiltering:
    @pytest.mark.asyncio
    async def test_allowed_tools_narrows_subagent_registry(
        self, tmp_path: Path,
    ) -> None:
        """An assistant with ``allowed_tools=[web_fetch]`` should run with
        exactly {web_fetch, artifact} — no read_file, no exec, no
        skill_manage. We don't need to execute anything; the mere fact
        that the subagent finishes without the LLM having access to
        blocked tools proves the filter is working.

        This test goes through the provider's first call returning a
        content response (no tool calls) so the loop exits cleanly
        and we can inspect what the child received.
        """
        # We can't directly introspect the tool registry built inside
        # _do_run's closure, but we CAN use a provider that inspects
        # the tools list it's handed and asserts on shape.
        tools_seen: list[list[dict]] = []

        class _Inspecting(LLMProvider):
            def __init__(self) -> None:
                super().__init__(api_key="x")

            def get_default_model(self) -> str:  # type: ignore[override]
                return "fake/model"

            async def chat(  # type: ignore[override]
                self, messages: Any, tools: Any = None, **kw: Any,
            ) -> LLMResponse:
                tools_seen.append(tools or [])
                return LLMResponse(content="done", tool_calls=[])

        asst = Assistant(
            name="narrow",
            description="narrow tool set",
            model="fake/model",
            system_prompt="You only read the web.",
            allowed_tools=frozenset({"web_fetch"}),
        )

        mgr = SubagentManager(
            provider=_Inspecting(),
            workspace=tmp_path,
            bus=MessageBus(),
            registry=SubagentRegistry(path=tmp_path / "runs.json"),
            state_dir=tmp_path,
        )
        await mgr.spawn(
            task="peek", label="assistant:narrow",
            origin_channel="cli", origin_chat_id="s",
            timeout_seconds=30, wait=True, assistant=asst,
        )

        assert tools_seen, "provider was never called"
        registered = {
            t["function"]["name"] for t in tools_seen[0]
            if isinstance(t, dict) and "function" in t
        }
        # artifact is always appended (output channel for auto_save)
        assert registered == {"web_fetch", "artifact"}

    @pytest.mark.asyncio
    async def test_allowed_tools_cannot_escalate_past_blocklist(
        self, tmp_path: Path,
    ) -> None:
        """A malicious assistant file listing ``allowed_tools: [spawn, cron]``
        still cannot get those tools — SubagentToolRegistry refuses them
        at registration time, the log gets a warning, and the run just
        lacks those tools."""
        tools_seen: list[list[dict]] = []

        class _Inspecting(LLMProvider):
            def __init__(self) -> None:
                super().__init__(api_key="x")

            def get_default_model(self) -> str:  # type: ignore[override]
                return "fake/model"

            async def chat(  # type: ignore[override]
                self, messages: Any, tools: Any = None, **kw: Any,
            ) -> LLMResponse:
                tools_seen.append(tools or [])
                return LLMResponse(content="done", tool_calls=[])

        asst = Assistant(
            name="evil",
            description="tries to escalate",
            model="fake/model",
            system_prompt="x",
            allowed_tools=frozenset({"spawn", "cron", "web_fetch"}),
        )
        mgr = SubagentManager(
            provider=_Inspecting(),
            workspace=tmp_path,
            bus=MessageBus(),
            registry=SubagentRegistry(path=tmp_path / "runs.json"),
            state_dir=tmp_path,
        )
        await mgr.spawn(
            task="x", label="assistant:evil",
            origin_channel="cli", origin_chat_id="s",
            timeout_seconds=30, wait=True, assistant=asst,
        )
        registered = {
            t["function"]["name"] for t in tools_seen[0]
            if isinstance(t, dict) and "function" in t
        }
        # spawn / cron are blocked regardless — only web_fetch survives.
        assert "spawn" not in registered
        assert "cron" not in registered
        assert "web_fetch" in registered
        assert "artifact" in registered


# ---------------------------------------------------------------------------
# 5. AssistantTool schema surface
# ---------------------------------------------------------------------------


class TestBuiltinAgentToolSchema:
    def test_tool_enum_reflects_registry(self, tmp_path: Path) -> None:
        # Tool description + enum should update when the registry gets
        # a new file — that's the whole point of the dynamic approach.
        reg = AssistantRegistry(tmp_path / "assistants")

        class _FakeMgr:
            pass

        tool = BuiltinAgentTool(manager=_FakeMgr(), registry=reg)
        names_before = set(tool.parameters["properties"]["agent"]["enum"])
        assert names_before == {"writer", "researcher", "coder"}

        _write_md(tmp_path / "assistants", "seo", (
            "---\nname: seo\ndescription: d\nmodel: m\n---\nbody"
        ))
        reg.reload()
        names_after = set(tool.parameters["properties"]["agent"]["enum"])
        assert names_after == {"writer", "researcher", "coder", "seo"}
        # Description also mentions new one.
        assert "seo" in tool.description

    @pytest.mark.asyncio
    async def test_builtin_agent_dispatches_via_registry(
        self, tmp_path: Path,
    ) -> None:
        # `builtin_agent(agent=…)` resolves the name through the registry
        # and spawns with the correct Assistant object + legacy label.
        reg = AssistantRegistry(tmp_path / "assistants")
        calls: list[dict] = []

        class _StubMgr:
            async def spawn(self, **kwargs: Any) -> str:
                calls.append(kwargs)
                return "stub-result"

            def last_completed_dispatch(self, **_: Any) -> None:
                # No prior dispatches → guard stays silent in this test.
                return None

        tool = BuiltinAgentTool(manager=_StubMgr(), registry=reg)
        r = await tool.execute(agent="writer", task="hi")
        assert r == "stub-result"
        assert calls and calls[0]["label"] == "builtin:writer"
        assert calls[0]["assistant"].name == "writer"
