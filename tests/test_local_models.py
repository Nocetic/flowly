"""Model servers on the agent's computer (flowly/integrations/local_models.py).

The servers are faked at the HTTP layer with the shapes their documentation
gives: Ollama's /api/tags, /api/ps, /api/show (docs/openapi.yaml) and LM
Studio's /api/v0/models.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from flowly.integrations import local_models
from flowly.integrations.local_models import MIN_CONTEXT, SERVERS, load, scan, scan_server, server_by_id

OLLAMA = server_by_id("ollama")
LMSTUDIO = server_by_id("lmstudio")


@pytest.fixture(autouse=True)
def _forget_seen_contexts():
    local_models._SEEN_CONTEXT.clear()
    yield
    local_models._SEEN_CONTEXT.clear()


def _ollama_app(*, running: dict[str, int] | None = None, shows: dict[str, dict] | None = None,
                generate_status: int = 200, generate_error: str = "", loads_with: int | None = None):
    running = dict(running or {})
    shows = shows or {}
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(f"{request.method} {request.url.path}")
        if request.url.host != "127.0.0.1" or request.url.port != 11434:
            raise httpx.ConnectError("refused", request=request)
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [
                {"name": name, "size": 4_000_000_000, "details": {"parameter_size": "8.0B"}} for name in shows
            ]})
        if request.url.path == "/api/ps":
            return httpx.Response(200, json={"models": [
                {"name": name, "context_length": ctx} for name, ctx in running.items()
            ]})
        if request.url.path == "/api/show":
            name = json.loads(request.content)["model"]
            return httpx.Response(200, json=shows[name]) if name in shows else httpx.Response(404, json={"error": "not found"})
        if request.url.path == "/api/generate":
            name = json.loads(request.content)["model"]
            if generate_status >= 400:
                return httpx.Response(generate_status, json={"error": generate_error})
            if loads_with:
                running[name] = loads_with
            return httpx.Response(200, json={"model": name, "done": True})
        return httpx.Response(404)

    return handler, calls


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _run(coro):
    return asyncio.run(coro)


QWEN = {
    "capabilities": ["completion", "tools", "thinking"],
    "model_info": {"general.architecture": "qwen3", "qwen3.context_length": 40960},
    "parameters": "temperature 0.6",
}
GEMMA_BUILT = {
    "capabilities": ["completion", "vision"],
    "model_info": {"gemma4.context_length": 131072},
    "parameters": "temperature 0.7\nnum_ctx 65536",
}
EMBED = {"capabilities": ["embedding"], "model_info": {"bert.context_length": 2048}}


def test_ollama_lists_chat_models_with_tools_and_the_context_it_really_runs():
    handler, _ = _ollama_app(
        running={"qwen3:8b": 4096},
        shows={"qwen3:8b": QWEN, "gemma4:latest": GEMMA_BUILT, "nomic-embed-text:latest": EMBED},
    )

    async def go():
        async with _client(handler) as client:
            return await scan_server(OLLAMA, client)

    result = _run(go())
    assert result["running"] is True and result["installed"] is True
    models = {m["id"]: m for m in result["models"]}
    # The embedding model cannot hold a conversation.
    assert set(models) == {"qwen3:8b", "gemma4:latest"}
    qwen = models["qwen3:8b"]
    assert qwen["tools"] is True
    assert qwen["maxContext"] == 40960
    # Loaded at Ollama's small default: the trained window does not count.
    assert qwen["loaded"] is True and qwen["context"] == 4096 and qwen["contextOk"] is False
    gemma = models["gemma4:latest"]
    assert gemma["tools"] is False
    # Not loaded, but built with num_ctx: that is what it will run with.
    assert gemma["loaded"] is False and gemma["context"] == 65536 and gemma["contextOk"] is True


def test_an_unknown_context_is_unknown_not_the_trained_window():
    handler, _ = _ollama_app(shows={"qwen3:8b": QWEN})

    async def go():
        async with _client(handler) as client:
            return await scan_server(OLLAMA, client)

    qwen = _run(go())["models"][0]
    assert qwen["context"] is None and qwen["contextOk"] is None and qwen["maxContext"] == 40960


def test_a_server_that_is_not_running_is_just_off(monkeypatch):
    monkeypatch.setattr(local_models, "_installed", lambda server: False)

    def refused(request):
        raise httpx.ConnectError("refused", request=request)

    async def go():
        async with _client(refused) as client:
            return await scan(client)

    servers = _run(go())
    assert [s["id"] for s in servers] == [s.id for s in SERVERS]
    for server in servers:
        assert server["running"] is False and server["installed"] is False
        assert server["models"] == [] and "error" not in server


def test_installed_but_off_is_reported(monkeypatch):
    monkeypatch.setattr(local_models, "_installed", lambda server: server.id == "lmstudio")

    def refused(request):
        raise httpx.ConnectError("refused", request=request)

    async def go():
        async with _client(refused) as client:
            return await scan_server(LMSTUDIO, client)

    result = _run(go())
    assert result["installed"] is True and result["running"] is False


def test_something_else_on_the_port_is_an_error_not_a_server():
    def html(request):
        return httpx.Response(200, text="<html>hello</html>")

    async def go():
        async with _client(html) as client:
            return await scan_server(OLLAMA, client)

    result = _run(go())
    assert result["running"] is False and result["error"]


def test_lm_studio_lists_llms_and_their_loaded_context():
    def handler(request):
        if request.url.path == "/api/v0/models":
            return httpx.Response(200, json={"object": "list", "data": [
                {"id": "qwen2.5-7b-instruct", "type": "llm", "state": "loaded",
                 "max_context_length": 32768, "loaded_context_length": 8192, "capabilities": ["tool_use"]},
                {"id": "qwen2-vl-7b-instruct", "type": "vlm", "state": "not-loaded", "max_context_length": 32768},
                {"id": "text-embedding-nomic-embed-text-v1.5", "type": "embeddings", "state": "not-loaded"},
            ]})
        return httpx.Response(404)

    async def go():
        async with _client(handler) as client:
            return await scan_server(LMSTUDIO, client)

    result = _run(go())
    models = {m["id"]: m for m in result["models"]}
    assert set(models) == {"qwen2.5-7b-instruct", "qwen2-vl-7b-instruct"}
    assert models["qwen2.5-7b-instruct"]["tools"] is True
    assert models["qwen2.5-7b-instruct"]["context"] == 8192
    assert models["qwen2.5-7b-instruct"]["contextOk"] is False
    # Loaded on demand later, with whatever LM Studio picks: not known yet.
    assert models["qwen2-vl-7b-instruct"]["context"] is None
    assert models["qwen2-vl-7b-instruct"]["tools"] is None


def test_an_older_lm_studio_falls_back_to_the_openai_list():
    def handler(request):
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [
                {"id": "mistral-7b-instruct"}, {"id": "text-embedding-nomic-embed-text-v1.5"},
            ]})
        return httpx.Response(404)

    async def go():
        async with _client(handler) as client:
            return await scan_server(LMSTUDIO, client)

    assert [m["id"] for m in _run(go())["models"]] == ["mistral-7b-instruct"]


def test_loading_an_ollama_model_reports_its_context_and_remembers_it():
    handler, calls = _ollama_app(shows={"qwen3:8b": QWEN}, loads_with=65536)

    async def go():
        async with _client(handler) as client:
            context = await load(OLLAMA, "qwen3:8b", client)
            # Ollama unloads it later; the context it ran with stays known.
            after = await scan_server(OLLAMA, client)
            return context, after

    context, after = _run(go())
    assert context == 65536
    assert "POST /api/generate" in calls
    assert after["models"][0]["context"] == 65536


def test_a_model_too_big_fails_to_load_with_ollamas_reason():
    handler, _ = _ollama_app(shows={"big:70b": QWEN}, generate_status=500,
                             generate_error="model requires more system memory (40 GiB) than is available (12 GiB)")

    async def go():
        async with _client(handler) as client:
            await load(OLLAMA, "big:70b", client)

    with pytest.raises(local_models.LocalServerError) as error:
        _run(go())
    assert error.value.code == "LOAD_FAILED"
    assert "more system memory" in error.value.message


def test_lm_studio_loads_on_demand_so_load_does_nothing():
    def handler(request):  # pragma: no cover - must not be called
        raise AssertionError("no request expected")

    async def go():
        async with _client(handler) as client:
            return await load(LMSTUDIO, "qwen2.5-7b-instruct", client)

    assert _run(go()) is None


def test_only_the_fixed_loopback_addresses_are_known():
    for server in SERVERS:
        assert server.base.startswith("http://127.0.0.1:")
        assert server.root.startswith("http://127.0.0.1:")
    assert server_by_id("ollama").slot == "ollama"
    assert server_by_id("lmstudio").slot == "vllm"
    assert server_by_id("http://evil") is None


def test_min_context_leaves_room_beside_compactions_reserve():
    from flowly.config.schema import CompactionConfig

    assert MIN_CONTEXT > CompactionConfig().reserve_tokens_floor


# ── the RPCs ────────────────────────────────────────────────────────────────


def _home(tmp_path, monkeypatch, data: dict):
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-cli-home"))
    from flowly.config.loader import get_config_path

    path = get_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _fake_server(monkeypatch, *, running=True, models=None, load_context=None, load_error=None):
    async def fake_scan_server(server, client=None):
        return {"id": server.id, "name": server.name, "slot": server.slot, "base": server.base,
                "installed": True, "running": running, "models": models or []}

    loads: list[str] = []

    async def fake_load(server, model, client=None):
        loads.append(model)
        if load_error:
            raise local_models.LocalServerError("LOAD_FAILED", load_error)
        return load_context

    monkeypatch.setattr(local_models, "scan_server", fake_scan_server)
    monkeypatch.setattr(local_models, "load", fake_load)
    return loads


def test_use_local_runs_the_agent_on_an_ollama_model_in_one_step(tmp_path, monkeypatch):
    from flowly.channels import feature_rpc
    from flowly.config.loader import load_config
    from flowly.integrations.active_provider import resolve_active_provider

    _home(tmp_path, monkeypatch, {
        "providers": {"active": "openai_codex"},
        "agents": {"defaults": {"model": "gpt-5.5", "reasoningEffort": "high", "serviceTier": "priority"}},
    })
    loads = _fake_server(monkeypatch, models=[{"id": "qwen3:8b", "tools": True, "context": None}], load_context=65536)
    monkeypatch.setattr(feature_rpc, "_provider_reload_cb", None)

    result = _run(feature_rpc.provider_use_local({"server": "ollama", "model": "qwen3:8b"}))

    assert loads == ["qwen3:8b"]
    assert result == {
        "ok": True, "server": "ollama", "key": "ollama", "model": "qwen3:8b", "tools": True,
        "context": 65536, "contextOk": True, "willRestart": True,
    }
    cfg = load_config()
    assert cfg.providers.active == "ollama"
    assert cfg.providers.ollama.api_base == "http://127.0.0.1:11434/v1"
    assert cfg.agents.defaults.model == "qwen3:8b"
    # A cloud model's effort and tier mean nothing to a local one.
    assert cfg.agents.defaults.reasoning_effort == ""
    assert cfg.agents.defaults.service_tier == ""
    active = resolve_active_provider(cfg)
    assert active is not None and active.key == "ollama" and active.api_base == "http://127.0.0.1:11434/v1"


def test_use_local_puts_lm_studio_on_the_generic_local_slot(tmp_path, monkeypatch):
    from flowly.channels import feature_rpc
    from flowly.config.loader import load_config

    _home(tmp_path, monkeypatch, {"agents": {"defaults": {"model": "anthropic/claude-sonnet-4.5"}}})
    _fake_server(monkeypatch, models=[{"id": "qwen2.5-7b-instruct", "tools": None, "context": 8192}])
    monkeypatch.setattr(feature_rpc, "_provider_reload_cb", None)

    result = _run(feature_rpc.provider_use_local({"server": "lmstudio", "model": "qwen2.5-7b-instruct"}))

    assert result["key"] == "vllm" and result["context"] == 8192 and result["contextOk"] is False
    cfg = load_config()
    assert cfg.providers.active == "vllm"
    assert cfg.providers.vllm.api_base == "http://127.0.0.1:1234/v1"
    assert cfg.agents.defaults.model == "qwen2.5-7b-instruct"


@pytest.mark.parametrize("params,code", [
    ({"server": "http://example.com", "model": "x"}, "INVALID"),
    ({"server": "ollama", "model": ""}, "INVALID"),
    ({"server": "ollama"}, "INVALID"),
])
def test_use_local_refuses_what_it_does_not_know(tmp_path, monkeypatch, params, code):
    from flowly.channels import feature_rpc

    _home(tmp_path, monkeypatch, {})
    _fake_server(monkeypatch, models=[{"id": "x"}])
    with pytest.raises(feature_rpc.FeatureRpcError) as error:
        _run(feature_rpc.provider_use_local(params))
    assert error.value.code == code


def test_use_local_changes_nothing_when_the_server_or_model_is_not_there(tmp_path, monkeypatch):
    from flowly.channels import feature_rpc

    path = _home(tmp_path, monkeypatch, {"agents": {"defaults": {"model": "gpt-5.5"}}})
    before = path.read_text(encoding="utf-8")

    _fake_server(monkeypatch, running=False)
    with pytest.raises(feature_rpc.FeatureRpcError) as error:
        _run(feature_rpc.provider_use_local({"server": "ollama", "model": "qwen3:8b"}))
    assert error.value.code == "UNREACHABLE"

    _fake_server(monkeypatch, models=[{"id": "llama3.2:latest"}])
    with pytest.raises(feature_rpc.FeatureRpcError) as error:
        _run(feature_rpc.provider_use_local({"server": "ollama", "model": "qwen3:8b"}))
    assert error.value.code == "NOT_FOUND"

    _fake_server(monkeypatch, models=[{"id": "qwen3:8b"}], load_error="model requires more system memory")
    with pytest.raises(feature_rpc.FeatureRpcError) as error:
        _run(feature_rpc.provider_use_local({"server": "ollama", "model": "qwen3:8b"}))
    assert error.value.code == "LOAD_FAILED" and "memory" in str(error.value)

    assert path.read_text(encoding="utf-8") == before


def test_provider_local_reports_both_servers_and_the_bar(monkeypatch):
    from flowly.channels import feature_rpc

    async def fake_scan(client=None):
        return [{"id": "ollama", "running": False}, {"id": "lmstudio", "running": False}]

    monkeypatch.setattr(local_models, "scan", fake_scan)
    result = _run(feature_rpc.provider_local({}))
    assert [s["id"] for s in result["servers"]] == ["ollama", "lmstudio"]
    assert result["minContext"] == MIN_CONTEXT
    assert result["recommendedContext"] >= MIN_CONTEXT


def test_local_slots_have_their_own_kind_in_the_provider_list(tmp_path, monkeypatch):
    from flowly.channels import feature_rpc

    _home(tmp_path, monkeypatch, {})
    rows = {row["key"]: row for row in feature_rpc.provider_list()["providers"]}
    assert rows["ollama"]["kind"] == "local" and rows["ollama"]["keyable"] is False
    assert rows["vllm"]["kind"] == "local" and rows["vllm"]["keyable"] is False


def test_the_rpcs_are_dispatched():
    from flowly.channels import feature_rpc

    assert feature_rpc._DISPATCH["provider.local"][0] is feature_rpc.provider_local
    assert feature_rpc._DISPATCH["provider.use_local"][0] is feature_rpc.provider_use_local


def test_the_catalogue_carries_the_running_context_to_compaction(tmp_path, monkeypatch):
    """Compaction sizes the conversation from the catalogue: it must see the
    context Ollama runs the model with, not the trained window it would cut."""
    from flowly.integrations import model_catalog

    _home(tmp_path, monkeypatch, {})
    handler, _ = _ollama_app(running={"qwen3:8b": 65536}, shows={"qwen3:8b": QWEN, "nomic-embed-text:latest": EMBED})
    real_client = httpx.AsyncClient
    monkeypatch.setattr(model_catalog.httpx, "AsyncClient",
                        lambda *a, **k: real_client(transport=httpx.MockTransport(handler)))
    model_catalog.flush_cache()

    models = _run(model_catalog.fetch_models("ollama", force_refresh=True))

    assert [m.id for m in models] == ["qwen3:8b"]
    assert models[0].context_window == 65536 and "tools" in models[0].tags
    assert model_catalog.get_context_window("qwen3:8b") == 65536
    model_catalog.flush_cache()
