"""Behaviour evaluation against the live model the owner has configured.

Skipped by default (it spends tokens). Run it before and after a change to
the agent loop or its prompt and compare the report:

    pytest tests/test_agent_behavior_real.py -m real_llm -s

It drives the real agent — real prompt, real tool routing, real delegation
rule — on Turkish-first conversations taken from what owners actually say,
and checks what the agent *did*, not how its prose reads:

* an explanation is answered, not turned into a tool call;
* "don't search" is respected;
* a long or detailed task is done by the agent itself (its own judgment —
  nothing in the runtime pushes it either way);
* an explicit "give this to an agent" starts exactly one subagent;
* independent parts the owner did not ask to delegate are reported, so a
  change that makes the agent start subagents more often shows up;
* after starting subagents the agent does the part that is still its own,
  then ends its turn — it does not wait for them, re-read them or poll a
  task list;
* asked to give the subagent that already reported another task ("ask the
  same agent to also check X"), it gives that subagent the task
  (``agent_followup``) instead of starting a new one. ``FLOWLY_EVAL_ONLY=a,b``
  runs only the named scenarios.

Subagents are recorded, not run, so a scenario costs one conversation turn.
Each run writes ``agent-behavior-report.json`` (path in ``FLOWLY_EVAL_OUT``,
default the system temp dir) with tool calls, model calls and timings per
scenario, so two runs can be diffed.

Credentials and model come from the owner's ``~/.flowly/config.json`` (or
``FLOWLY_EVAL_CONFIG``); everything the agent writes goes to a temp home.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

import pytest

pytestmark = pytest.mark.real_llm


@dataclass
class Scenario:
    name: str
    message: str
    check: Callable[["Observed"], str | None]  # returns a failure reason or None
    # Earlier conversation rows (oldest first) the turn continues.
    history: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class Observed:
    final: str = ""
    tools: list[str] = field(default_factory=list)
    spawn_results: list[dict[str, Any]] = field(default_factory=list)
    followups: list[dict[str, Any]] = field(default_factory=list)
    model_calls: int = 0
    seconds: float = 0.0

    @property
    def spawn_started(self) -> int:
        return sum(1 for r in self.spawn_results if r.get("status") == "started")

    @property
    def spawn_refused(self) -> int:
        return sum(1 for r in self.spawn_results if r.get("status") == "refused")


def _answered(o: Observed) -> str | None:
    if not o.final.strip():
        return "no answer"
    if "no action was taken" in o.final.lower():
        return "answer replaced by a fallback"
    return None


def _explanation(o: Observed) -> str | None:
    return _answered(o) or (f"used tools for an explanation: {o.tools}" if o.tools else None)


def _no_search(o: Observed) -> str | None:
    searched = [t for t in o.tools if t in ("web_search", "web_fetch", "web_extract")]
    return _answered(o) or (f"searched although told not to: {searched}" if searched else None)


def _did_it_itself(o: Observed) -> str | None:
    if o.spawn_started:
        return f"delegated a single task ({o.spawn_started} subagent(s))"
    return _answered(o)


_WAITING_TOOLS = ("agent_wait", "agent_read", "sessions_list", "exec")


def _ends_turn_after_starting(o: Observed) -> str | None:
    waited = [t for t in o.tools if t in _WAITING_TOOLS]
    if o.spawn_started and waited:
        return f"waited on its subagents instead of ending the turn: {waited}"
    return _answered(o)


def _one_on_request(o: Observed) -> str | None:
    if o.spawn_started != 1:
        return f"expected exactly one subagent on request, got {o.spawn_started}"
    return _ends_turn_after_starting(o)


def _one_each_on_request(o: Observed) -> str | None:
    if o.spawn_started != 3:
        return f"expected one subagent per company on request, got {o.spawn_started}"
    return _ends_turn_after_starting(o)


def _three_and_its_own_part(o: Observed) -> str | None:
    if o.spawn_started != 3:
        return f"expected three subagents, got {o.spawn_started}"
    if not any(t in ("web_search", "web_fetch", "web_extract") for t in o.tools):
        return "left its own part undone"
    return _ends_turn_after_starting(o)


def _self_or_ends_turn(o: Observed) -> str | None:
    return _answered(o) if not o.spawn_started else _ends_turn_after_starting(o)


# A subagent that already reported in this conversation (the follow-up scenario).
_EARLIER_AGENT = "5d0c6b1e-2f4a-4c3b-9a7e-1b2c3d4e5f60"
_EARLIER_HISTORY = [
    {"role": "user", "content": "Anthropic'in kuruluş yılını bir agent'la araştır."},
    {"role": "assistant", "content": "Bir alt ajan başlattım; sonucu gelince söyleyeceğim."},
    {"role": "user", "_display_hidden": True, "content": (
        "[System: subagent] [Subagent 'Anthropic kuruluş yılı' completed successfully "
        f"(runtime: 14s)] run_id={_EARLIER_AGENT} agent_id={_EARLIER_AGENT}\n"
        "Tools used (2 calls): web_search, web_fetch\n\n"
        "Brief: Find the year Anthropic was founded, with a source.\n\n"
        "Result:\nAnthropic was founded in 2021 (source: https://www.anthropic.com/company).\n\n"
        "This is the subagent's own report: check what matters before relying on it, and don't "
        "redo its work. Tell the owner what it means for their request in your own words; don't "
        "show run ids or agent ids. The subagent keeps its conversation: agent_followup gives it "
        "another task, agent_close ends it."
    )},
    {"role": "assistant", "content": "Anthropic 2021'de kuruldu (kaynak: anthropic.com)."},
]


def _followed_up_the_same_agent(o: Observed) -> str | None:
    if o.spawn_started:
        return f"started {o.spawn_started} new subagent(s) instead of asking the same one"
    if len(o.followups) != 1:
        return f"expected one follow-up to the same subagent, got {len(o.followups)}"
    if not str(o.followups[0].get("to") or "").startswith(_EARLIER_AGENT[:8]):
        return f"gave the task to {o.followups[0].get('to')!r}, not the subagent that reported"
    waited = [t for t in o.tools if t in _WAITING_TOOLS]
    if waited:
        return f"waited on the subagent instead of ending the turn: {waited}"
    return _answered(o)


SCENARIOS = [
    Scenario("explain-screenshot", "Screenshot nedir? Kısaca açıkla.", _explanation),
    Scenario("explain-send", "What does 'send' mean in an email client? One sentence.", _explanation),
    Scenario("no-search", "Arama yapma, sadece açıkla: DNS nasıl çalışır? Üç cümle.", _no_search),
    Scenario("detailed-single-task",
             "Bu konuyu detaylı incele ve özetle: HTTP/3 neden TCP yerine UDP kullanır?",
             _did_it_itself),
    Scenario("explicit-delegation",
             "Bunu bir subagent'a ver: 'teşekkürler' kelimesinin İspanyolcasını bulsun.",
             _one_on_request),
    Scenario("independent-parts",
             "Şu üç şirketin kuruluş yıllarını ayrı ayrı araştır: Anthropic, Mistral AI, Cohere.",
             _self_or_ends_turn),
    Scenario("one-agent-each",
             "Şu üç şirketin kuruluş yıllarını ve hikayelerini ayrı ayrı araştır: Anthropic, "
             "Mistral AI, Cohere. agent çalıştır her biri için.",
             _one_each_on_request),
    Scenario("agents-plus-own-part",
             "Anthropic, Mistral AI ve Cohere'in kuruluş yıllarını birer agent'la araştır; "
             "OpenAI'ın kuruluş yılını da sen kendin araştır.",
             _three_and_its_own_part),
    Scenario("followup-same-agent",
             "Aynı agent'a Mistral AI'ın kuruluş yılını da araştırt.",
             _followed_up_the_same_agent, history=_EARLIER_HISTORY),
]


def _config_path() -> Path:
    return Path(os.environ.get("FLOWLY_EVAL_CONFIG") or Path.home() / ".flowly" / "config.json")


@pytest.fixture(scope="module")
def agent_factory():
    path = _config_path()
    if not path.exists():
        pytest.skip(f"no Flowly config at {path}")
    from flowly.config.loader import load_config
    from flowly.integrations.active_provider import resolve_active_provider
    from flowly.providers.factory import build_provider

    config = load_config(path)
    active = resolve_active_provider(config)
    if active is None:
        pytest.skip("no LLM provider configured")
    provider = build_provider(active, default_model=config.agents.defaults.model, config=config)
    return config, provider


async def _observe(config, provider, scenario: Scenario, home: Path) -> Observed:
    from flowly.agent.loop import AgentLoop
    from flowly.bus.queue import MessageBus

    observed = Observed()
    original_chat = provider.chat

    async def counting_chat(*args: Any, **kwargs: Any):
        observed.model_calls += 1
        return await original_chat(*args, **kwargs)

    provider.chat = counting_chat  # type: ignore[method-assign]
    workspace = home / "workspace" / scenario.name
    workspace.mkdir(parents=True, exist_ok=True)
    loop = AgentLoop(
        bus=MessageBus(), provider=provider, workspace=workspace,
        model=config.agents.defaults.model, main_config=config, state_dir=home / "state",
    )

    async def recorded_spawn(**kwargs: Any) -> str:  # subagents are recorded, not run
        return json.dumps({"status": "started", "run_id": f"eval-{len(observed.spawn_results)}",
                           "title": kwargs.get("display_name") or ""})

    loop.subagents.spawn = recorded_spawn  # type: ignore[method-assign]

    async def recorded_followup(target: str, task: str, **kwargs: Any) -> dict[str, Any]:
        observed.followups.append({"to": target, "task": task})
        return {"status": "started", "run_id": f"eval-followup-{len(observed.followups)}",
                "agent_id": target, "turn": 2, "title": "Anthropic kuruluş yılı",
                "message": "It is working on this task in the background."}

    loop.subagents.followup = recorded_followup  # type: ignore[method-assign]
    session_key = f"eval:{scenario.name}"
    if scenario.history:
        session = loop.sessions.get_or_create(session_key)
        for row in scenario.history:
            session.add_message(row["role"], row["content"],
                                **{k: v for k, v in row.items() if k not in ("role", "content")})
        loop.sessions.save(session)
    original_execute = loop.tools.execute

    async def recording_execute(name: str, args: dict[str, Any], **kwargs: Any) -> str:
        observed.tools.append(name)
        result = await original_execute(name, args, **kwargs)
        if name == "spawn":
            try:
                observed.spawn_results.append(json.loads(result))
            except (TypeError, ValueError):
                observed.spawn_results.append({"status": "unparsed"})
        return result

    loop.tools.execute = recording_execute  # type: ignore[method-assign]
    started = time.monotonic()
    try:
        observed.final = await loop.process_direct(
            scenario.message, session_key=session_key,
        ) or ""
    finally:
        observed.seconds = round(time.monotonic() - started, 2)
        provider.chat = original_chat  # type: ignore[method-assign]
        loop.stop()
    return observed


@pytest.mark.asyncio
async def test_agent_behaviour_on_owner_conversations(agent_factory, monkeypatch):
    config, provider = agent_factory
    home = Path(tempfile.mkdtemp(prefix="flowly-eval-"))
    monkeypatch.setenv("FLOWLY_HOME", str(home))

    report: list[dict[str, Any]] = []
    failures: list[str] = []
    only = {n for n in os.environ.get("FLOWLY_EVAL_ONLY", "").split(",") if n}
    for scenario in (s for s in SCENARIOS if not only or s.name in only):
        observed = await _observe(config, provider, scenario, home)
        problem = scenario.check(observed)
        report.append({"scenario": scenario.name, "ok": problem is None, "problem": problem,
                       **{k: v for k, v in asdict(observed).items() if k != "final"},
                       "final_preview": observed.final[:300]})
        if problem:
            failures.append(f"{scenario.name}: {problem}")

    out_dir = Path(os.environ.get("FLOWLY_EVAL_OUT") or tempfile.gettempdir())
    out = out_dir / "agent-behavior-report.json"
    out.write_text(json.dumps({"model": config.agents.defaults.model, "results": report},
                              ensure_ascii=False, indent=2))
    print(f"\nagent behaviour report → {out}")
    for row in report:
        print(f"  {'✓' if row['ok'] else '✗'} {row['scenario']:<22} tools={row['tools']} "
              f"model_calls={row['model_calls']} {row['seconds']}s {row['problem'] or ''}")
    assert not failures, "\n".join(failures)
