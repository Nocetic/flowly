"""D2: the embedding key-routing fix.

A non-OpenAI active-provider key must not be used as the embedding key — the
"auto" resolver would mis-detect it as a Gemini key, the embedding call would
401, and search would silently fall back to keyword-only while claiming vectors.
"""

from __future__ import annotations

from types import SimpleNamespace

from flowly.memory.embeddings import _resolve_provider_and_model


def test_auto_resolver_misroutes_non_openai_key():
    # sk- → OpenAI; any other non-empty key under "auto" → Gemini. That mis-route
    # is exactly why the loop must not hand a non-OpenAI active key to embeddings.
    assert _resolve_provider_and_model("auto", "", "sk-abc", None)[0] == "openai"
    assert _resolve_provider_and_model("auto", "", "xai-abc", None)[0] == "gemini"
    # No key + no configured providers → no vector provider (honest keyword-only).
    assert _resolve_provider_and_model("auto", "", "", None) == (None, None)


def test_build_memory_manager_only_seeds_openai_key(tmp_path, monkeypatch):
    import flowly.memory.manager as mgr
    from flowly.agent.loop import AgentLoop

    captured: dict = {}
    monkeypatch.setattr(mgr, "get_manager", lambda **kw: captured.update(kw) or "MGR")

    def _fake(active_key: str):
        ms = SimpleNamespace(
            api_key="", provider="auto", model="", api_base=None,
            chunk_tokens=400, overlap_tokens=80, max_results=6, min_score=0.35,
            vector_weight=0.7, text_weight=0.3,
        )
        return SimpleNamespace(
            _memory_search_config=ms,
            _main_config=SimpleNamespace(get_api_key=lambda: active_key),
            _state_dir=tmp_path,
            workspace=tmp_path,
        )

    captured.clear()
    AgentLoop._build_memory_manager(_fake("xai-abc123"))
    assert captured["api_key"] == ""           # non-OpenAI active key not seeded

    captured.clear()
    AgentLoop._build_memory_manager(_fake("flw_abc123"))
    assert captured["api_key"] == ""           # Flowly proxy key not seeded

    captured.clear()
    AgentLoop._build_memory_manager(_fake("sk-abc123"))
    assert captured["api_key"] == "sk-abc123"  # genuine OpenAI key is seeded
