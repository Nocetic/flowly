"""Consolidation merges the same memory when it names its owner both ways.

Live model, skipped by default. Run against a real Flowly home:

    FLOWLY_REAL_LLM_HOME=~/.flowly pytest -m real_llm tests/test_memory_consolidate_real.py

Memories call the owner "User" in one note and by name in the next ("User
prefers replies in Turkish." / "Ozan wants the agent to answer in
Turkish."). The proposer did not know they were one person: with the active
cheap model (deepseek-v4.1-flash, 2026-10-28) 2 of 10 such duplicates merged
over five runs; told who the owner is, 10 of 10, and no related-but-different
memory ever merged. Runs the production path: the active provider and model.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

from flowly.memory.consolidate import PROMPT, build_context, owner_profile, parse_operations
from flowly.memory.governance import GovernanceStore

pytestmark = pytest.mark.real_llm

OWNER_MD = "# User Profile — Ozan\n\n- **Name:** Ozan\n- **Language:** Turkish (casual tone)\n"
DUPLICATES = [
    ("User prefers replies in Turkish.", "Ozan wants the agent to answer in Turkish."),
    ("User's birthday is March 3.", "Ozan was born on 3 March."),
]
DISTINCT = [
    "User drinks coffee without sugar.",
    "User's dog is named Pamuk.",
    "User lives in Istanbul.",
    "User's sister lives in Berlin.",
    "User wakes up at 7am on weekdays.",
    "User wakes up at 9am on weekends.",
]
RUNS = 3


def _provider():
    """The active provider of the Flowly home named by FLOWLY_REAL_LLM_HOME.

    Tests run in a home of their own (conftest), so the owner points at the
    real one explicitly.
    """
    from flowly.config.loader import load_config
    from flowly.integrations.active_provider import resolve_active_provider
    from flowly.providers.factory import build_provider

    home = os.environ.get("FLOWLY_REAL_LLM_HOME", "")
    if not home:
        pytest.skip("set FLOWLY_REAL_LLM_HOME to a Flowly home with a provider")
    config = load_config(Path(home).expanduser() / "config.json")
    active = resolve_active_provider(config)
    if active is None:
        pytest.skip("no LLM provider configured")
    model = config.agents.defaults.model
    return build_provider(active, default_model=model, config=config), model


def test_the_owner_named_both_ways_is_one_memory(tmp_path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "USER.md").write_text(OWNER_MD, encoding="utf-8")
    gov = GovernanceStore(tmp_path / "gov.sqlite3")
    for text in [text for pair in DUPLICATES for text in pair] + DISTINCT:
        item = gov.add_item(kind="preference", text=text, status="candidate", ref_kind="memory_md",
                            normalized_key="pref:" + text.lower(), confidence=0.85, actor="system", reason="test")
        gov.transition(item.id, "active", actor="system", reason="test")
    context = build_context(gov, "", owner_profile(workspace))
    text_of = {item["id"]: item["text"] for item in context["items"]}
    prompt = PROMPT.replace("{context}", json.dumps(context, ensure_ascii=False, indent=2))
    provider, model = _provider()

    async def propose() -> str:
        parts: list[str] = []
        async for delta in provider.chat_stream([{"role": "user", "content": prompt}],
                                                model=model, max_tokens=2048, temperature=0.1):
            if delta.content:
                parts.append(delta.content)
        return "".join(parts)

    found = 0
    for _ in range(RUNS):
        for op in parse_operations(asyncio.run(propose())):
            pair = {text_of.get(op.item_id), text_of.get(op.into_id or "")}
            if op.op == "merge":
                assert pair in [set(p) for p in DUPLICATES], f"merged memories that differ: {pair}"
                found += 1
    assert found >= RUNS * len(DUPLICATES) - 1
