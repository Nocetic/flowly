"""Core semantic routing stays local, incremental, and fail-safe."""

from __future__ import annotations

import hashlib
import inspect
import math
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Iterable

import pytest

from flowly.agent.loop import AgentLoop
from flowly.agent.tools.base import Tool
from flowly.agent.tools.discovery import build_tool_disclosure
from flowly.agent.tools.registry import ToolRegistry
from flowly.agent.tools.semantic_routing import (
    SemanticRouteDecision,
    SemanticToolIndex,
    SemanticToolRouter,
    build_semantic_snapshot,
)
from flowly.bus.queue import MessageBus
from flowly.config.schema import Config
from flowly.providers.base import LLMProvider, LLMResponse


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
                        "description": "The request to send to this capability.",
                    },
                },
                "required": ["query"],
            },
        },
    }


class KeywordEncoder:
    """Small deterministic encoder used to test routing, not model quality."""

    encoder_id = "test-keywords-v1"

    def __init__(self) -> None:
        self.document_batches: list[tuple[str, ...]] = []
        self.queries: list[str] = []

    @staticmethod
    def _vector(text: str) -> list[float]:
        lowered = text.casefold()
        values = [
            float(any(word in lowered for word in (
                "documentation", "documentación", "library", "dokümantasyon",
                "文档", "ドキュメント",
            ))),
            float(any(word in lowered for word in (
                "publish", "social", "gönderi", "publicación", "投稿",
            ))),
            float(any(word in lowered for word in (
                "calendar", "meeting", "takvim", "calendario", "カレンダー",
            ))),
            float(any(word in lowered for word in (
                "email", "inbox", "e-posta", "correo", "メール",
            ))),
        ]
        if not any(values):
            values.append(1.0)
        else:
            values.append(0.0)
        norm = math.sqrt(sum(value * value for value in values))
        return [value / norm for value in values]

    def embed_documents(self, texts: Iterable[str]) -> list[list[float]]:
        batch = tuple(texts)
        self.document_batches.append(batch)
        return [self._vector(text) for text in batch]

    def embed_query(self, text: str) -> list[float]:
        self.queries.append(text)
        return self._vector(text)


def _extended_snapshot(*, generation: int = 2):
    first = _snapshot()
    name = "mcp_mail_search_inbox"
    return build_semantic_snapshot(
        [
            *(document.definition for document in first.documents.values()),
            _definition(name, "Search an email inbox."),
        ],
        toolsets={
            **{item.name: item.toolset for item in first.documents.values()},
            name: "mcp",
        },
        sources={
            **{item.name: item.source for item in first.documents.values()},
            name: "mail-service",
        },
        deferred_toolsets={"mcp", "extensions"},
        generation=generation,
    )


def _snapshot(*, generation: int = 1, include_second_docs_source: bool = False):
    definitions = [
        _definition(
            "mcp_reference_resolve_library",
            "Resolve a software library before searching its documentation.",
        ),
        _definition(
            "mcp_reference_query_docs",
            "Query official software library documentation and examples.",
        ),
        _definition(
            "mcp_social_publish_post",
            "Publish and schedule a social media post.",
        ),
        _definition(
            "read_file",
            "Read a local workspace file.",
        ),
    ]
    sources = {
        "mcp_reference_resolve_library": "reference",
        "mcp_reference_query_docs": "reference",
        "mcp_social_publish_post": "social-service",
    }
    if include_second_docs_source:
        definitions.append(_definition(
            "mcp_other_query_docs",
            "Query official software library documentation and examples.",
        ))
        sources["mcp_other_query_docs"] = "other-reference"
    return build_semantic_snapshot(
        definitions,
        toolsets={
            definition["function"]["name"]: (
                "mcp" if definition["function"]["name"].startswith("mcp_")
                else "filesystem"
            )
            for definition in definitions
        },
        sources=sources,
        deferred_toolsets={"mcp", "extensions"},
        always_visible_tools=(),
        generation=generation,
    )


def test_semantic_router_has_no_memory_embedding_dependency(tmp_path: Path) -> None:
    import flowly.agent.tools.semantic_routing as module

    source = inspect.getsource(module)
    assert "flowly.memory" not in source

    encoder = KeywordEncoder()
    router = SemanticToolRouter(
        state_dir=tmp_path,
        encoder_factory=lambda: encoder,
        auto_download=False,
    )
    router.prepare_sync(_snapshot())

    assert router.index_path.parent == tmp_path / "tool-routing"
    assert not any("memory" in part.casefold() for part in router.index_path.parts)


def test_index_embeds_only_added_or_changed_tools(tmp_path: Path) -> None:
    encoder = KeywordEncoder()
    index = SemanticToolIndex(tmp_path / "index.sqlite3")
    first = _snapshot()

    initial = index.sync(first, encoder)
    unchanged = index.sync(first, encoder)
    changed = _extended_snapshot()
    delta = index.sync(changed, encoder)

    assert initial.embedded == 4
    assert unchanged.embedded == 0
    assert unchanged.unchanged == 4
    assert delta.embedded == 1
    assert sum(len(batch) for batch in encoder.document_batches) == 5


def test_persisted_hashes_avoid_reembedding_after_restart(tmp_path: Path) -> None:
    path = tmp_path / "index.sqlite3"
    first_encoder = KeywordEncoder()
    SemanticToolIndex(path).sync(_snapshot(), first_encoder)

    second_encoder = KeywordEncoder()
    result = SemanticToolIndex(path).sync(_snapshot(), second_encoder)

    assert result.embedded == 0
    assert second_encoder.document_batches == []


def test_corrupt_vector_row_self_heals_without_touching_user_data(
    tmp_path: Path,
) -> None:
    encoder = KeywordEncoder()
    router = SemanticToolRouter(
        state_dir=tmp_path,
        encoder_factory=lambda: encoder,
        auto_download=False,
    )
    snapshot = _snapshot()
    first = router.prepare_sync(snapshot)
    assert first is not None
    with sqlite3.connect(router.index_path) as connection:
        connection.execute(
            "UPDATE tool_vectors SET dimension = 999 WHERE tool_name = ?",
            ("mcp_reference_query_docs",),
        )

    rebuilt = router.prepare_sync(snapshot)

    assert rebuilt is not None
    assert rebuilt.embedded == len(snapshot.documents)
    assert router.state == "ready"


def test_route_without_external_candidates_never_initializes_encoder(
    tmp_path: Path,
) -> None:
    calls = 0

    def factory() -> KeywordEncoder:
        nonlocal calls
        calls += 1
        return KeywordEncoder()

    snapshot = _snapshot()
    router = SemanticToolRouter(
        state_dir=tmp_path,
        encoder_factory=factory,
        auto_download=False,
    )

    decision = router.select_sync(
        "Read a local file.",
        snapshot,
        allowed_names=frozenset({"read_file"}),
    )

    assert decision.mode == "no_match"
    assert decision.state == "idle"
    assert calls == 0


def test_packaged_model_asset_is_verified_without_network(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import flowly.agent.tools.semantic_routing as module

    packaged = tmp_path / "package-model"
    model_file = packaged / module.DEFAULT_MODEL_FILE
    model_file.parent.mkdir(parents=True)
    payload = b"verified-local-model"
    model_file.write_bytes(payload)
    monkeypatch.delenv("FLOWLY_SEMANTIC_MODEL_DIR", raising=False)
    monkeypatch.setattr(module, "_packaged_model_dir", lambda: packaged)
    monkeypatch.setattr(
        module,
        "DEFAULT_MODEL_MANIFEST",
        {module.DEFAULT_MODEL_FILE: (len(payload), hashlib.sha256(payload).hexdigest())},
    )

    resolved = module.FastEmbedLocalEncoder._resolve_model(
        tmp_path / "cache",
        allow_download=False,
    )

    assert resolved == packaged


def test_packaged_model_same_size_tamper_is_rejected(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import flowly.agent.tools.semantic_routing as module

    packaged = tmp_path / "package-model"
    model_file = packaged / module.DEFAULT_MODEL_FILE
    model_file.parent.mkdir(parents=True)
    expected = b"expected-local-model"
    model_file.write_bytes(b"tampered-local-model")
    assert len(expected) == model_file.stat().st_size
    monkeypatch.delenv("FLOWLY_SEMANTIC_MODEL_DIR", raising=False)
    monkeypatch.setattr(module, "_packaged_model_dir", lambda: packaged)
    monkeypatch.setattr(
        module,
        "DEFAULT_MODEL_MANIFEST",
        {module.DEFAULT_MODEL_FILE: (len(expected), hashlib.sha256(expected).hexdigest())},
    )

    with pytest.raises(ValueError, match="checksum"):
        module.FastEmbedLocalEncoder._resolve_model(
            tmp_path / "cache",
            allow_download=False,
        )


def test_concurrent_same_query_runs_local_inference_once(tmp_path: Path) -> None:
    class BlockingQueryEncoder(KeywordEncoder):
        def __init__(self) -> None:
            super().__init__()
            self.entered = threading.Event()
            self.release = threading.Event()

        def embed_query(self, text: str) -> list[float]:
            self.queries.append(text)
            self.entered.set()
            assert self.release.wait(timeout=2)
            return self._vector(text)

    encoder = BlockingQueryEncoder()
    router = SemanticToolRouter(
        state_dir=tmp_path,
        encoder_factory=lambda: encoder,
        auto_download=False,
        source_min_score=0.80,
        source_min_margin=0.10,
    )
    snapshot = _snapshot()
    router.prepare_sync(snapshot)
    kwargs = {
        "allowed_names": frozenset(snapshot.documents),
    }
    prompt = "Check the official library documentation."

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [
            executor.submit(router.select_sync, prompt, snapshot, **kwargs)
            for _ in range(8)
        ]
        assert encoder.entered.wait(timeout=2)
        encoder.release.set()
        decisions = [future.result(timeout=2) for future in futures]

    assert all(decision.mode == "semantic" for decision in decisions)
    assert encoder.queries == [prompt]


def test_background_refresh_coalesces_to_newest_generation(tmp_path: Path) -> None:
    class BlockingDocumentEncoder(KeywordEncoder):
        def __init__(self) -> None:
            super().__init__()
            self.entered = threading.Event()
            self.release = threading.Event()

        def embed_documents(self, texts: Iterable[str]) -> list[list[float]]:
            batch = tuple(texts)
            self.document_batches.append(batch)
            if len(self.document_batches) == 1:
                self.entered.set()
                assert self.release.wait(timeout=2)
            return [self._vector(text) for text in batch]

    encoder = BlockingDocumentEncoder()
    router = SemanticToolRouter(
        state_dir=tmp_path,
        encoder_factory=lambda: encoder,
        auto_download=False,
    )
    first = _snapshot(generation=1)
    latest = _extended_snapshot(generation=2)

    assert router.prepare(first) is False
    assert encoder.entered.wait(timeout=2)
    assert router.prepare(latest) is False
    encoder.release.set()
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        with router._lock:
            if router._ready_fingerprint == latest.fingerprint:
                break
        time.sleep(0.01)

    with router._lock:
        assert router._ready_fingerprint == latest.fingerprint
        assert router._ready_generation == 2
    assert sum(len(batch) for batch in encoder.document_batches) == 5


def test_background_refresh_does_not_repeat_active_snapshot(tmp_path: Path) -> None:
    class BlockingDocumentEncoder(KeywordEncoder):
        def __init__(self) -> None:
            super().__init__()
            self.entered = threading.Event()
            self.release = threading.Event()

        def embed_documents(self, texts: Iterable[str]) -> list[list[float]]:
            batch = tuple(texts)
            self.document_batches.append(batch)
            self.entered.set()
            assert self.release.wait(timeout=2)
            return [self._vector(text) for text in batch]

    encoder = BlockingDocumentEncoder()
    router = SemanticToolRouter(
        state_dir=tmp_path,
        encoder_factory=lambda: encoder,
        auto_download=False,
    )
    snapshot = _snapshot(generation=1)

    assert router.prepare(snapshot) is False
    assert encoder.entered.wait(timeout=2)
    assert router.prepare(snapshot) is False
    encoder.release.set()
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        with router._lock:
            if router._ready_fingerprint == snapshot.fingerprint:
                break
        time.sleep(0.01)

    assert len(encoder.document_batches) == 1


def test_stale_generation_cannot_replace_newer_ready_index(tmp_path: Path) -> None:
    encoder = KeywordEncoder()
    router = SemanticToolRouter(
        state_dir=tmp_path,
        encoder_factory=lambda: encoder,
        auto_download=False,
    )
    first = _snapshot(generation=1)
    latest = _extended_snapshot(generation=2)
    router.prepare_sync(latest)

    assert router.prepare(first) is False
    with router._lock:
        assert router._ready_fingerprint == latest.fingerprint
        assert router._ready_generation == 2
    assert "mcp_mail_search_inbox" in router._vectors


def test_registry_change_subscription_covers_register_replace_and_remove() -> None:
    registry = ToolRegistry()
    generations: list[int] = []
    unsubscribe = registry.subscribe_changes(generations.append)

    registry.register(_LoopTool("mcp_service_action", "First schema. "), toolset="mcp")
    registry.register(_LoopTool("mcp_service_action", "Changed schema. "), toolset="mcp")
    registry.unregister("mcp_service_action")
    unsubscribe()
    registry.register(_LoopTool("mcp_after_unsubscribe", "Ignored. "), toolset="mcp")

    assert generations == [1, 2, 3]


def test_catalog_snapshot_is_cached_until_registry_generation_changes() -> None:
    class CountingTool(_LoopTool):
        def __init__(self, name: str, description: str) -> None:
            super().__init__(name, description)
            self.schema_reads = 0

        @property
        def description(self) -> str:
            self.schema_reads += 1
            return super().description

    registry = ToolRegistry()
    first_tool = CountingTool("mcp_first_action", "First. ")
    registry.register(first_tool, toolset="mcp")

    first = registry.get_catalog_snapshot()
    cached = registry.get_catalog_snapshot()

    assert cached is first
    assert first_tool.schema_reads == 1

    registry.register(_LoopTool("mcp_second_action", "Second. "), toolset="mcp")
    changed = registry.get_catalog_snapshot()

    assert changed.generation > first.generation
    assert first_tool.schema_reads == 2


def test_unique_source_promotes_small_workflow_across_languages(tmp_path: Path) -> None:
    encoder = KeywordEncoder()
    router = SemanticToolRouter(
        state_dir=tmp_path,
        encoder_factory=lambda: encoder,
        auto_download=False,
        source_min_score=0.80,
        source_min_margin=0.10,
    )
    snapshot = _snapshot()
    router.prepare_sync(snapshot)
    allowed = frozenset(snapshot.documents)

    for prompt in (
        "Resmî dokümantasyondan React cleanup davranışını doğrula.",
        "Verify this against the official library documentation.",
        "Consulta la documentación oficial de la biblioteca.",
        "公式ライブラリのドキュメントを確認してください。",
        "请检查官方库文档。",
    ):
        decision = router.select_sync(prompt, snapshot, allowed_names=allowed)
        assert decision.promotions == (
            "mcp_reference_query_docs",
            "mcp_reference_resolve_library",
        )
        assert decision.mode == "semantic"


def test_ambiguous_sources_do_not_promote_or_hide_tools(tmp_path: Path) -> None:
    encoder = KeywordEncoder()
    router = SemanticToolRouter(
        state_dir=tmp_path,
        encoder_factory=lambda: encoder,
        auto_download=False,
        source_min_score=0.80,
        source_min_margin=0.10,
    )
    snapshot = _snapshot(include_second_docs_source=True)
    router.prepare_sync(snapshot)

    decision = router.select_sync(
        "Use official library documentation.",
        snapshot,
        allowed_names=frozenset(snapshot.documents),
    )

    assert decision.promotions == ()
    assert decision.mode == "ambiguous"


def test_route_filter_is_authoritative_over_semantic_similarity(tmp_path: Path) -> None:
    encoder = KeywordEncoder()
    router = SemanticToolRouter(
        state_dir=tmp_path,
        encoder_factory=lambda: encoder,
        auto_download=False,
        source_min_score=0.80,
        source_min_margin=0.10,
    )
    snapshot = _snapshot()
    router.prepare_sync(snapshot)

    decision = router.select_sync(
        "Check the official library documentation.",
        snapshot,
        allowed_names=frozenset({"mcp_social_publish_post"}),
    )

    assert decision.promotions == ()
    assert "mcp_reference_query_docs" not in decision.promotions


def test_eager_core_tools_compete_but_can_never_be_promoted(tmp_path: Path) -> None:
    encoder = KeywordEncoder()
    router = SemanticToolRouter(
        state_dir=tmp_path,
        encoder_factory=lambda: encoder,
        auto_download=False,
        source_min_score=0.80,
        source_min_margin=0.10,
    )
    snapshot = _snapshot()
    router.prepare_sync(snapshot)

    decision = router.select_sync(
        "Please read this local workspace file.",
        snapshot,
        allowed_names=frozenset(snapshot.documents),
    )

    assert decision.promotions == ()
    assert decision.mode == "no_match"
    assert decision.source == "filesystem"
    assert "read_file" not in snapshot.promotable_names


def test_empty_semantic_advice_never_suppresses_deterministic_promotion() -> None:
    definitions = [
        _definition(
            "mcp_reference_query_docs",
            "Query official software library documentation and examples. " * 40,
        ),
        _definition(
            "mcp_social_publish_post",
            "Publish and schedule a social media post. " * 40,
        ),
    ]
    definitions.extend(
        _definition(
            f"mcp_unrelated_action_{index}",
            "Perform a verbose unrelated external action. " * 40,
        )
        for index in range(6)
    )

    disclosure = build_tool_disclosure(
        definitions,
        toolsets={
            definition["function"]["name"]: "mcp"
            for definition in definitions
        },
        minimum_deferred_schema_tokens=0,
        intent_text="Check the official library documentation.",
        semantic_promoted_tools=(),
    )

    assert "mcp_reference_query_docs" in disclosure.promoted_names


def test_query_embedding_is_cached_without_persisting_user_text(tmp_path: Path) -> None:
    encoder = KeywordEncoder()
    router = SemanticToolRouter(
        state_dir=tmp_path,
        encoder_factory=lambda: encoder,
        auto_download=False,
        source_min_score=0.80,
        source_min_margin=0.10,
    )
    snapshot = _snapshot()
    router.prepare_sync(snapshot)
    prompt = "SECRET-USER-TEXT: official library documentation"

    first = router.select_sync(
        prompt, snapshot, allowed_names=frozenset(snapshot.documents)
    )
    second = router.select_sync(
        prompt, snapshot, allowed_names=frozenset(snapshot.documents)
    )

    assert first.promotions == second.promotions
    assert encoder.queries == [prompt]
    assert prompt.encode() not in router.index_path.read_bytes()


def test_encoder_failure_is_a_non_blocking_fallback(tmp_path: Path) -> None:
    def fail() -> KeywordEncoder:
        raise RuntimeError("encoder unavailable")

    router = SemanticToolRouter(
        state_dir=tmp_path,
        encoder_factory=fail,
        auto_download=False,
    )

    decision = router.select_sync(
        "official documentation",
        _snapshot(),
        allowed_names=frozenset({"mcp_reference_query_docs"}),
    )

    assert decision.promotions is None
    assert decision.mode == "fallback"
    assert "encoder unavailable" not in decision.public_reason


def test_query_dimension_mismatch_is_a_safe_fallback(tmp_path: Path) -> None:
    class MismatchedEncoder(KeywordEncoder):
        def embed_query(self, text: str) -> list[float]:
            self.queries.append(text)
            return [1.0, 0.0]

    router = SemanticToolRouter(
        state_dir=tmp_path,
        encoder_factory=MismatchedEncoder,
        auto_download=False,
    )
    snapshot = _snapshot()

    decision = router.select_sync(
        "Check official documentation.",
        snapshot,
        allowed_names=frozenset(snapshot.documents),
    )

    assert decision.promotions is None
    assert decision.mode == "fallback"


def test_ten_thousand_tool_catalog_remains_bounded_and_correct(
    tmp_path: Path,
) -> None:
    target = "mcp_provider_07777_query_docs"
    definitions = [
        _definition(
            f"mcp_provider_{index:05d}_{'query_docs' if index == 7777 else 'action'}",
            (
                "Query official software library documentation."
                if index == 7777
                else "Perform a distinct connected service operation."
            ),
        )
        for index in range(10_000)
    ]
    names = [definition["function"]["name"] for definition in definitions]
    snapshot = build_semantic_snapshot(
        definitions,
        toolsets={name: "mcp" for name in names},
        sources={name: f"provider-{index:05d}" for index, name in enumerate(names)},
        deferred_toolsets={"mcp", "extensions"},
        generation=1,
    )
    router = SemanticToolRouter(
        state_dir=tmp_path,
        encoder_factory=KeywordEncoder,
        auto_download=False,
        source_min_score=0.80,
        source_min_margin=0.10,
    )
    router.prepare_sync(snapshot)

    started = time.perf_counter()
    decision = router.select_sync(
        "Use the official library documentation.",
        snapshot,
        allowed_names=frozenset(snapshot.documents),
    )
    elapsed = time.perf_counter() - started

    assert decision.promotions == (target,)
    assert elapsed < 2.0


def test_disclosure_accepts_only_route_scoped_semantic_promotions() -> None:
    definitions = [
        _definition(
            "mcp_reference_query_docs",
            "Query official documentation. " * 40,
        ),
        _definition(
            "mcp_social_publish_post",
            "Publish a social media post. " * 40,
        ),
        _definition("read_file", "Read a local file."),
    ]
    definitions.extend(
        _definition(
            f"mcp_other_action_{index}",
            "Perform a verbose unrelated external action. " * 40,
        )
        for index in range(6)
    )

    disclosure = build_tool_disclosure(
        definitions,
        toolsets={
            "mcp_reference_query_docs": "mcp",
            "mcp_social_publish_post": "mcp",
            "read_file": "filesystem",
            **{f"mcp_other_action_{index}": "mcp" for index in range(6)},
        },
        minimum_deferred_schema_tokens=0,
        intent_text="unrelated text",
        semantic_promoted_tools=(
            "mcp_reference_query_docs",
            "read_file",
            "not_registered",
        ),
    )

    assert disclosure.promoted_names == {"mcp_reference_query_docs"}
    assert "mcp_reference_query_docs" in disclosure.direct_names
    assert "mcp_social_publish_post" in disclosure.deferred


@pytest.mark.parametrize("corrupt", [b"not sqlite", b"\x00\x01\x02"])
def test_corrupt_derived_index_is_quarantined_and_rebuilt(
    tmp_path: Path,
    corrupt: bytes,
) -> None:
    path = tmp_path / "index.sqlite3"
    path.write_bytes(corrupt)
    encoder = KeywordEncoder()

    result = SemanticToolIndex(path).sync(_snapshot(), encoder)

    assert result.embedded == 4
    assert path.exists()
    assert list(tmp_path.glob("index.sqlite3.corrupt-*"))


class _LoopTool(Tool):
    def __init__(self, name: str, description: str) -> None:
        self._name = name
        self._description = description

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return self._description * 40

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "A deliberately complete external input. " * 30,
                },
            },
        }

    async def execute(self, **kwargs) -> str:
        return "ok"


def test_agent_loop_fresh_config_does_not_initialize_semantic_router(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path / "home"))

    class Provider(LLMProvider):
        def get_default_model(self) -> str:
            return "test/model"

        async def chat(self, *args, **kwargs) -> LLMResponse:
            raise AssertionError("constructor must not call the model")

    loop = AgentLoop(
        bus=MessageBus(),
        provider=Provider(api_key="test"),
        workspace=tmp_path,
        main_config=Config(),
    )
    try:
        assert loop._semantic_tool_router is None
    finally:
        loop.stop()


@pytest.mark.asyncio
async def test_agent_loop_uses_semantic_promotion_without_an_extra_llm_call(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path / "home"))

    class Provider(LLMProvider):
        def __init__(self) -> None:
            super().__init__(api_key="test")
            self.calls = 0

        def get_default_model(self) -> str:
            return "test/model"

        async def chat(self, *args, **kwargs) -> LLMResponse:
            self.calls += 1
            names = {
                definition["function"]["name"]
                for definition in kwargs["tools"]
            }
            assert "mcp_reference_query_docs" in names
            assert "mcp_social_publish_post" not in names
            return LLMResponse(content="done")

    class Router:
        def __init__(self) -> None:
            self.allowed: frozenset[str] = frozenset()

        async def select(self, intent, snapshot, *, allowed_names, max_tools):
            self.allowed = allowed_names
            return SemanticRouteDecision(
                promotions=("mcp_reference_query_docs",),
                mode="semantic",
                state="ready",
                source="reference",
                score=0.91,
                margin=0.20,
            )

        def prepare(self, snapshot) -> bool:
            return True

        def close(self) -> None:
            pass

    provider = Provider()
    config = Config()
    config.tools.routing.discovery.semantic_routing_enabled = True
    config.tools.routing.discovery.semantic_routing_consent = "enabled"
    loop = AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path,
        main_config=config,
        max_iterations=2,
        soft_warn_at_iteration=0,
    )
    original = loop._semantic_tool_router
    if original is not None:
        original.close()
    router = Router()
    loop._semantic_tool_router = router
    loop.tools.register(
        _LoopTool("mcp_reference_query_docs", "Query documentation. "),
        toolset="mcp",
    )
    loop.tools.register(
        _LoopTool("mcp_social_publish_post", "Publish social posts. "),
        toolset="mcp",
    )
    for index in range(6):
        loop.tools.register(
            _LoopTool(f"mcp_other_action_{index}", "Unrelated action. "),
            toolset="mcp",
        )

    final, *_rest = await loop._run_llm_tool_loop(
        messages=[
            {"role": "system", "content": "test"},
            {"role": "user", "content": "Check official documentation."},
        ],
        action_turn=False,
        turn_content="Check official documentation.",
        session_key="web:test",
        tool_platform="web",
    )

    assert final == "done"
    assert provider.calls == 1
    assert "mcp_reference_query_docs" in router.allowed
    loop.stop()


def test_agent_loop_auto_refreshes_after_dynamic_external_registration(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path / "home"))

    class Provider(LLMProvider):
        def get_default_model(self) -> str:
            return "test/model"

        async def chat(self, *args, **kwargs) -> LLMResponse:
            return LLMResponse(content="unused")

    class Router:
        def __init__(self) -> None:
            self.ready = threading.Event()
            self.snapshots = []

        def prepare(self, snapshot) -> bool:
            self.snapshots.append(snapshot)
            if "mcp_dynamic_search" in snapshot.promotable_names:
                self.ready.set()
            return True

        def close(self) -> None:
            pass

    config = Config()
    config.tools.routing.discovery.minimum_deferred_schema_tokens = 0
    config.tools.routing.discovery.semantic_routing_enabled = True
    config.tools.routing.discovery.semantic_routing_consent = "enabled"
    loop = AgentLoop(
        bus=MessageBus(),
        provider=Provider(api_key="test"),
        workspace=tmp_path,
        main_config=config,
        max_iterations=1,
        soft_warn_at_iteration=0,
    )
    original = loop._semantic_tool_router
    if original is not None:
        original.close()
    router = Router()
    loop._semantic_tool_router = router

    loop.tools.register(
        _LoopTool("mcp_dynamic_search", "Search a newly connected source. "),
        toolset="mcp",
    )

    assert router.ready.wait(timeout=2)
    assert router.snapshots[-1].generation == loop.tools.generation
    loop.stop()


def test_agent_loop_reuses_semantic_snapshot_within_registry_generation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path / "home"))

    class Provider(LLMProvider):
        def get_default_model(self) -> str:
            return "test/model"

        async def chat(self, *args, **kwargs) -> LLMResponse:
            return LLMResponse(content="unused")

    config = Config()
    config.tools.routing.discovery.semantic_routing_enabled = True
    config.tools.routing.discovery.semantic_routing_consent = "enabled"
    loop = AgentLoop(
        bus=MessageBus(),
        provider=Provider(api_key="test"),
        workspace=tmp_path,
        main_config=config,
        max_iterations=1,
        soft_warn_at_iteration=0,
    )
    loop.tools.register(
        _LoopTool("mcp_cached_search", "Search one connected source. "),
        toolset="mcp",
    )

    first = loop._semantic_catalog_snapshot()
    second = loop._semantic_catalog_snapshot()

    assert first is not None
    assert second is first
    loop.stop()


def test_small_eager_external_catalog_does_not_warm_semantic_model(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path / "home"))

    class Provider(LLMProvider):
        def get_default_model(self) -> str:
            return "test/model"

        async def chat(self, *args, **kwargs) -> LLMResponse:
            return LLMResponse(content="unused")

    class SmallTool(_LoopTool):
        @property
        def description(self) -> str:
            return "One small external action."

        @property
        def parameters(self) -> dict:
            return {"type": "object", "properties": {}}

    class Router:
        def __init__(self) -> None:
            self.prepare_calls = 0

        def prepare(self, snapshot) -> bool:
            self.prepare_calls += 1
            return True

        def close(self) -> None:
            pass

    config = Config()
    config.tools.routing.discovery.semantic_routing_enabled = True
    config.tools.routing.discovery.semantic_routing_consent = "enabled"
    loop = AgentLoop(
        bus=MessageBus(),
        provider=Provider(api_key="test"),
        workspace=tmp_path,
        main_config=config,
        max_iterations=1,
        soft_warn_at_iteration=0,
    )
    original = loop._semantic_tool_router
    if original is not None:
        original.close()
    router = Router()
    loop._semantic_tool_router = router
    loop.tools.register(SmallTool("mcp_small_action", "unused"), toolset="mcp")

    loop._prepare_semantic_tool_index()

    assert router.prepare_calls == 0
    loop.stop()
