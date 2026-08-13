"""Optional quality gate for the pinned local semantic routing model.

CI/release jobs opt in by setting ``FLOWLY_SEMANTIC_MODEL_DIR`` to an already
verified model snapshot. Normal unit tests remain offline and deterministic.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from flowly.agent.tools.semantic_routing import (
    DEFAULT_MODEL_FILE,
    SemanticToolRouter,
    build_semantic_snapshot,
)


def _definition(name: str, description: str) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The user's query.",
                    },
                },
            },
        },
    }


def _quality_snapshot():
    external = {
        "docs_resolve": (
            "docs",
            "Resolve a software library identifier before searching its documentation.",
        ),
        "docs_query": (
            "docs",
            "Retrieve up-to-date official software library documentation and code examples.",
        ),
        "social_publish": (
            "social",
            "Create, schedule, and publish posts, tweets, threads, drafts, and social media content.",
        ),
        "email_search": (
            "email",
            "Search an email inbox, read messages, manage mail, and send email replies.",
        ),
        "calendar_event": (
            "calendar",
            "List meetings and calendar events, create appointments, and update schedules.",
        ),
        "project_issue": (
            "project",
            "Search issues, create tickets, update tasks, boards, projects, and sprints.",
        ),
        "source_repo": (
            "source",
            "Search repositories, inspect commits and branches, and manage pull requests.",
        ),
        "storage_file": (
            "storage",
            "Search files and folders, upload and download documents from a cloud drive.",
        ),
        "database_query": (
            "database",
            "Query SQL tables, database schemas, records, rows, and PostgreSQL data.",
        ),
        "monitoring_error": (
            "monitoring",
            "Search errors, logs, incidents, alerts, traces, and application performance.",
        ),
        "payments_invoice": (
            "payments",
            "Manage invoices, subscriptions, checkout sessions, prices, and customer payments.",
        ),
        "crm_contact": (
            "crm",
            "Search customers, leads, contacts, deals, accounts, sales pipelines, and opportunities.",
        ),
        "design_file": (
            "design",
            "Inspect design files, frames, prototypes, UI components, and canvas assets.",
        ),
    }
    core = {
        "read_file": ("filesystem", "Read the contents of a local workspace file."),
        "list_dir": ("filesystem", "List files and directories in the local workspace."),
        "exec": (
            "execution",
            "Run a shell command or local development command in the current workspace.",
        ),
        "web_search": ("web", "Search the public web for current information."),
        "clarify": (
            "interactive",
            "Ask the user for clarification when information is missing.",
        ),
    }
    definitions = []
    toolsets = {}
    sources = {}
    expected_names = {}
    for suffix, (source, description) in external.items():
        name = f"mcp_{source}_{suffix}"
        definitions.append(_definition(name, description))
        toolsets[name] = "mcp"
        sources[name] = source
        expected_names.setdefault(source, set()).add(name)
    for name, (toolset, description) in core.items():
        definitions.append(_definition(name, description))
        toolsets[name] = toolset
    snapshot = build_semantic_snapshot(
        definitions,
        toolsets=toolsets,
        sources=sources,
        deferred_toolsets={"mcp", "extensions"},
        generation=1,
    )
    return snapshot, expected_names


def test_pinned_model_multilingual_precision_gate(tmp_path: Path) -> None:
    model_dir = Path(str(os.environ.get("FLOWLY_SEMANTIC_MODEL_DIR") or ""))
    if not model_dir.is_dir() or not (model_dir / DEFAULT_MODEL_FILE).is_file():
        pytest.skip("pinned semantic model asset is not available")

    snapshot, expected_names = _quality_snapshot()
    router = SemanticToolRouter(
        state_dir=tmp_path,
        auto_download=False,
        source_min_score=0.78,
        source_min_margin=0.009,
        tool_min_score=0.76,
    )
    positives = (
        ("docs", "Check the official React library documentation for useEffect cleanup"),
        ("docs", "Consulta la documentación oficial de React sobre useEffect"),
        ("social", "Bu gönderiyi yarın sosyal medyada yayınla"),
        ("email", "Suche die E-Mail von Anna in meinem Posteingang"),
        ("calendar", "Ajoute une réunion à mon calendrier demain"),
        ("project", "أنشئ تذكرة جديدة في مشروع العمل"),
        ("source", "Найди последний коммит в репозитории"),
        ("storage", "मेरी क्लाउड ड्राइव में रिपोर्ट फ़ाइल खोजें"),
        ("database", "Consulte os registros desta tabela PostgreSQL"),
        ("monitoring", "프로덕션 오류 로그와 추적을 찾아줘"),
        ("payments", "Trova la fattura e lo stato del pagamento"),
        ("design", "デザインファイルのUIコンポーネントを確認して"),
    )
    direct_sources = set()
    for expected_source, query in positives:
        decision = router.select_sync(
            query,
            snapshot,
            allowed_names=frozenset(snapshot.documents),
        )
        if decision.promotions:
            assert set(decision.promotions) <= expected_names[expected_source]
            direct_sources.add(expected_source)

    # Recall is deliberately bounded by a high-precision confidence gate.
    # Uncertain intents retain the complete lexical search/call fallback.
    assert len(direct_sources) >= 8

    for query in (
        "Selam, nasılsın?",
        "Write a short poem about the sea",
        "Bu klasördeki pyproject.toml dosyasını oku",
        "计算 17 乘以 42",
    ):
        decision = router.select_sync(
            query,
            snapshot,
            allowed_names=frozenset(snapshot.documents),
        )
        assert decision.promotions == ()
