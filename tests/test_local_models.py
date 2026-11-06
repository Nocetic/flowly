"""Model servers on the agent's computer (flowly/integrations/local_models.py).

The servers are faked at the HTTP layer with the shapes their documentation
gives: Ollama's /api/tags, /api/ps, /api/show, /api/generate
(docs/openapi.yaml) and LM Studio's REST API /api/v1/models,
/api/v1/models/load, /api/v1/models/unload (lmstudio.ai/docs/developer/rest),
with the older /api/v0/models and /v1/models it falls back to. The v1 entry
below is the one a real LM Studio 0.4.28 returned on 2026-11-06.

The agent's need is fixed here at NEED (what a Desktop agent measured:
68K of overhead + 20K reply reserve + 8K of conversation, rounded).
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from flowly.compaction import window_need
from flowly.integrations import local_models
from flowly.integrations.local_models import SERVERS, load, scan, scan_server, server_by_id

OLLAMA = server_by_id("ollama")
LMSTUDIO = server_by_id("lmstudio")
NEED = 96_256


@pytest.fixture(autouse=True)
def _clean():
    local_models._SEEN_CONTEXT.clear()
    window_need.reset_for_tests()
    yield
    local_models._SEEN_CONTEXT.clear()
    window_need.reset_for_tests()


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _run(coro):
    return asyncio.run(coro)


# ── Ollama ──────────────────────────────────────────────────────────────────


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


QWEN = {
    "capabilities": ["completion", "tools", "thinking"],
    "model_info": {"general.architecture": "qwen3", "qwen3.context_length": 131072},
    "parameters": "temperature 0.6",
}
SMALL = {
    "capabilities": ["completion", "tools"],
    "model_info": {"llama.context_length": 40960},
}
GEMMA_BUILT = {
    "capabilities": ["completion", "vision"],
    "model_info": {"gemma4.context_length": 131072},
    "parameters": "temperature 0.7\nnum_ctx 131072",
}
EMBED = {"capabilities": ["embedding"], "model_info": {"bert.context_length": 2048}}


def test_ollama_models_are_judged_against_what_the_agent_needs():
    handler, _ = _ollama_app(
        running={"qwen3:8b": 4096},
        shows={"qwen3:8b": QWEN, "gemma4:latest": GEMMA_BUILT, "small:7b": SMALL, "nomic-embed-text:latest": EMBED},
    )

    async def go():
        async with _client(handler) as client:
            return await scan_server(OLLAMA, client, required=NEED)

    result = _run(go())
    assert result["running"] is True and result["installed"] is True
    models = {m["id"]: m for m in result["models"]}
    # The embedding model cannot hold a conversation.
    assert set(models) == {"qwen3:8b", "gemma4:latest", "small:7b"}
    qwen = models["qwen3:8b"]
    assert qwen["tools"] is True and qwen["maxContext"] == 131072 and qwen["maxContextOk"] is True
    # Loaded at Ollama's small default: the trained window does not count.
    assert qwen["loaded"] is True and qwen["context"] == 4096 and qwen["contextOk"] is False
    gemma = models["gemma4:latest"]
    assert gemma["tools"] is False
    # Not loaded, but built with num_ctx: that is what it will run with.
    assert gemma["context"] == 131072 and gemma["contextOk"] is True
    # Trained for 40K: it can never hold this agent.
    assert models["small:7b"]["maxContextOk"] is False


def test_an_unknown_context_is_unknown_not_the_trained_window():
    handler, _ = _ollama_app(shows={"qwen3:8b": QWEN})

    async def go():
        async with _client(handler) as client:
            return await scan_server(OLLAMA, client, required=NEED)

    qwen = _run(go())["models"][0]
    assert qwen["context"] is None and qwen["contextOk"] is None and qwen["maxContextOk"] is True


def test_a_server_that_is_not_running_is_just_off(monkeypatch):
    monkeypatch.setattr(local_models, "_installed", lambda server: False)

    def refused(request):
        raise httpx.ConnectError("refused", request=request)

    async def go():
        async with _client(refused) as client:
            return await scan(client, required=NEED)

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
            return await scan_server(LMSTUDIO, client, required=NEED)

    result = _run(go())
    assert result["installed"] is True and result["running"] is False


def test_something_else_on_the_port_is_an_error_not_a_server():
    def html(request):
        return httpx.Response(200, text="<html>hello</html>")

    async def go():
        async with _client(html) as client:
            return await scan_server(OLLAMA, client, required=NEED)

    result = _run(go())
    assert result["running"] is False and result["error"]


def test_loading_an_ollama_model_reports_its_context_and_remembers_it():
    handler, calls = _ollama_app(shows={"qwen3:8b": QWEN}, loads_with=131072)

    async def go():
        async with _client(handler) as client:
            context = await load(OLLAMA, "qwen3:8b", required=NEED, client=client)
            # Ollama unloads it later; the context it ran with stays known.
            after = await scan_server(OLLAMA, client, required=NEED)
            return context, after

    context, after = _run(go())
    assert context == 131072
    assert "POST /api/generate" in calls
    assert after["models"][0]["context"] == 131072 and after["models"][0]["contextOk"] is True


def test_a_model_too_big_fails_to_load_with_ollamas_reason():
    handler, _ = _ollama_app(shows={"big:70b": QWEN}, generate_status=500,
                             generate_error="model requires more system memory (40 GiB) than is available (12 GiB)")

    async def go():
        async with _client(handler) as client:
            await load(OLLAMA, "big:70b", required=NEED, client=client)

    with pytest.raises(local_models.LocalServerError) as error:
        _run(go())
    assert error.value.code == "LOAD_FAILED"
    assert "more system memory" in error.value.message


# ── LM Studio ───────────────────────────────────────────────────────────────


def _lms_entry(key: str = "google/gemma-4-e2b", *, contexts: tuple[int, ...] = (), max_context: int = 131072,
               kind: str = "llm", tools: bool = True) -> dict:
    """An /api/v1/models entry as LM Studio 0.4.28 returns it."""
    entry = {
        "type": kind, "publisher": "google", "key": key, "display_name": "Gemma 4 E2B",
        "architecture": "gemma4", "quantization": {"name": "Q4_K_M", "bits_per_weight": 4},
        "size_bytes": 4414788475, "params_string": "4.6B",
        "loaded_instances": [
            {"id": key if i == 0 else f"{key}:{i + 1}",
             "config": {"context_length": ctx, "eval_batch_size": 2048, "parallel": 4, "flash_attention": True}}
            for i, ctx in enumerate(contexts)
        ],
        "max_context_length": max_context, "format": "gguf",
    }
    if kind == "llm":
        entry["capabilities"] = {"vision": True, "trained_for_tool_use": tools,
                                 "reasoning": {"allowed_options": ["off", "on"], "default": "on"}}
    return entry


class _LMStudio:
    """LM Studio's v1 REST API: list, load (with a context length), unload."""

    def __init__(self, *entries: dict, load_status: int = 200, load_error: str = "",
                 clamp_to: int | None = None, v1: bool = True):
        self.entries = {e["key"]: e for e in entries}
        self.load_status, self.load_error, self.clamp_to, self.v1 = load_status, load_error, clamp_to, v1
        self.calls: list[tuple[str, dict | None]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        self.calls.append((f"{request.method} {request.url.path}", body))
        if request.url.port != 1234:
            raise httpx.ConnectError("refused", request=request)
        path = request.url.path
        if not self.v1 and path.startswith("/api/v1/"):
            return httpx.Response(404)
        if path == "/api/v1/models" and request.method == "GET":
            return httpx.Response(200, json={"models": list(self.entries.values())})
        if path == "/api/v1/models/unload":
            for entry in self.entries.values():
                entry["loaded_instances"] = [i for i in entry["loaded_instances"] if i["id"] != body["instance_id"]]
            return httpx.Response(200, json={"instance_id": body["instance_id"]})
        if path == "/api/v1/models/load":
            if self.load_status >= 400:
                return httpx.Response(self.load_status, json={"error": {"message": self.load_error}})
            entry = self.entries[body["model"]]
            context = min(body["context_length"], self.clamp_to or body["context_length"])
            entry["loaded_instances"].append({"id": body["model"], "config": {"context_length": context}})
            return httpx.Response(200, json={"type": "llm", "instance_id": body["model"], "status": "loaded",
                                             "load_time_seconds": 3.1, "load_config": {"context_length": context}})
        if path == "/api/v0/models":
            return httpx.Response(404)
        if path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": key} for key in self.entries]})
        return httpx.Response(404)

    def posts(self) -> list[tuple[str, dict | None]]:
        return [call for call in self.calls if call[0].startswith("POST")]


def test_lm_studio_lists_chat_models_from_its_rest_api():
    app = _LMStudio(
        _lms_entry(contexts=(8192,)),
        _lms_entry("qwen/qwen3-4b", tools=False),
        _lms_entry("text-embedding-nomic-embed-text-v1.5", kind="embedding", max_context=2048),
    )

    async def go():
        async with _client(app) as client:
            return await scan_server(LMSTUDIO, client, required=NEED)

    models = {m["id"]: m for m in _run(go())["models"]}
    assert set(models) == {"google/gemma-4-e2b", "qwen/qwen3-4b"}
    gemma = models["google/gemma-4-e2b"]
    assert gemma["tools"] is True and gemma["loaded"] is True and gemma["parameters"] == "4.6B"
    assert gemma["context"] == 8192 and gemma["contextOk"] is False and gemma["maxContextOk"] is True
    assert models["qwen/qwen3-4b"]["tools"] is False and models["qwen/qwen3-4b"]["context"] is None


def test_an_older_lm_studio_falls_back_to_the_lists_it_has():
    def v0(request):
        if request.url.path == "/api/v0/models":
            return httpx.Response(200, json={"object": "list", "data": [
                {"id": "qwen2.5-7b-instruct", "type": "llm", "state": "loaded",
                 "max_context_length": 131072, "loaded_context_length": 8192, "capabilities": ["tool_use"]},
                {"id": "text-embedding-nomic-embed-text-v1.5", "type": "embeddings", "state": "not-loaded"},
            ]})
        return httpx.Response(404)

    def openai_only(request):
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "mistral-7b-instruct"}, {"id": "text-embedding-x"}]})
        return httpx.Response(404)

    async def go(handler):
        async with _client(handler) as client:
            return await scan_server(LMSTUDIO, client, required=NEED)

    v0_models = _run(go(v0))["models"]
    assert [m["id"] for m in v0_models] == ["qwen2.5-7b-instruct"] and v0_models[0]["context"] == 8192
    assert [m["id"] for m in _run(go(openai_only))["models"]] == ["mistral-7b-instruct"]


def test_lm_studio_loads_a_model_with_the_context_the_agent_needs():
    app = _LMStudio(_lms_entry())

    async def go():
        async with _client(app) as client:
            return await load(LMSTUDIO, "google/gemma-4-e2b", required=NEED, client=client)

    assert _run(go()) == 98_304
    assert app.posts() == [("POST /api/v1/models/load",
                            {"model": "google/gemma-4-e2b", "context_length": 98_304, "echo_load_config": True})]


def test_a_model_held_with_too_little_context_is_loaded_again_with_enough():
    """The case Hakan hit: LM Studio held gemma-4-e2b at 8192 tokens."""
    app = _LMStudio(_lms_entry(contexts=(8192,)))

    async def go():
        async with _client(app) as client:
            return await load(LMSTUDIO, "google/gemma-4-e2b", required=NEED, client=client)

    assert _run(go()) == 98_304
    assert [call[0] for call in app.posts()] == ["POST /api/v1/models/unload", "POST /api/v1/models/load"]
    assert app.posts()[0][1] == {"instance_id": "google/gemma-4-e2b"}
    assert [i["config"]["context_length"] for i in app.entries["google/gemma-4-e2b"]["loaded_instances"]] == [98_304]


def test_a_model_held_with_enough_context_is_left_alone():
    app = _LMStudio(_lms_entry(contexts=(131072,)))

    async def go():
        async with _client(app) as client:
            return await load(LMSTUDIO, "google/gemma-4-e2b", required=NEED, client=client)

    assert _run(go()) == 131072
    assert app.posts() == []


def test_a_model_whose_window_cannot_hold_the_agent_is_refused_before_anything_changes():
    app = _LMStudio(_lms_entry(contexts=(8192,), max_context=32768))

    async def go():
        async with _client(app) as client:
            await load(LMSTUDIO, "google/gemma-4-e2b", required=NEED, client=client)

    with pytest.raises(local_models.LocalServerError) as error:
        _run(go())
    assert error.value.code == "CONTEXT_TOO_SMALL" and "32,768" in error.value.message
    assert app.posts() == []  # what the owner loaded stays loaded


def test_a_load_lm_studio_refuses_says_why():
    app = _LMStudio(_lms_entry(), load_status=500, load_error="Insufficient memory to load the model")

    async def go():
        async with _client(app) as client:
            await load(LMSTUDIO, "google/gemma-4-e2b", required=NEED, client=client)

    with pytest.raises(local_models.LocalServerError) as error:
        _run(go())
    assert error.value.code == "LOAD_FAILED" and "Insufficient memory" in error.value.message


def test_an_lm_studio_without_the_rest_api_loads_on_demand():
    app = _LMStudio(_lms_entry(), v1=False)

    async def go():
        async with _client(app) as client:
            return await load(LMSTUDIO, "google/gemma-4-e2b", required=NEED, client=client)

    assert _run(go()) is None
    assert app.posts() == []


def test_only_the_fixed_loopback_addresses_are_known():
    for server in SERVERS:
        assert server.base.startswith("http://127.0.0.1:")
        assert server.root.startswith("http://127.0.0.1:")
    assert server_by_id("ollama").slot == "ollama"
    assert server_by_id("lmstudio").slot == "vllm"
    assert server_by_id("http://evil") is None


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
    async def fake_scan_server(server, client=None, *, required=None):
        return {"id": server.id, "name": server.name, "slot": server.slot, "base": server.base,
                "installed": True, "running": running, "models": models or []}

    loads: list[tuple[str, int]] = []

    async def fake_load(server, model, *, required, client=None):
        loads.append((model, required))
        if load_error:
            raise local_models.LocalServerError(*load_error)
        return load_context

    monkeypatch.setattr(local_models, "scan_server", fake_scan_server)
    monkeypatch.setattr(local_models, "load", fake_load)
    monkeypatch.setattr(local_models, "agent_need", lambda: (NEED, True))
    return loads


def test_use_local_runs_the_agent_on_an_ollama_model_in_one_step(tmp_path, monkeypatch):
    from flowly.channels import feature_rpc
    from flowly.config.loader import load_config
    from flowly.integrations.active_provider import resolve_active_provider

    _home(tmp_path, monkeypatch, {
        "providers": {"active": "openai_codex"},
        "agents": {"defaults": {"model": "gpt-5.5", "reasoningEffort": "high", "serviceTier": "priority"}},
    })
    loads = _fake_server(monkeypatch, models=[{"id": "qwen3:8b", "tools": True, "context": None, "maxContextOk": True}],
                         load_context=131072)
    monkeypatch.setattr(feature_rpc, "_provider_reload_cb", None)

    result = _run(feature_rpc.provider_use_local({"server": "ollama", "model": "qwen3:8b"}))

    assert loads == [("qwen3:8b", NEED)]
    assert result == {
        "ok": True, "server": "ollama", "key": "ollama", "model": "qwen3:8b", "tools": True,
        "context": 131072, "requiredContext": NEED, "contextOk": True, "willRestart": True,
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
    loads = _fake_server(monkeypatch, models=[{"id": "google/gemma-4-e2b", "tools": True, "context": 8192,
                                               "maxContextOk": True}], load_context=98_304)
    monkeypatch.setattr(feature_rpc, "_provider_reload_cb", None)

    result = _run(feature_rpc.provider_use_local({"server": "lmstudio", "model": "google/gemma-4-e2b"}))

    assert loads == [("google/gemma-4-e2b", NEED)]
    assert result["key"] == "vllm" and result["context"] == 98_304 and result["contextOk"] is True
    cfg = load_config()
    assert cfg.providers.active == "vllm"
    assert cfg.providers.vllm.api_base == "http://127.0.0.1:1234/v1"
    assert cfg.agents.defaults.model == "google/gemma-4-e2b"


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


@pytest.mark.parametrize("server,models,kwargs,code", [
    ("ollama", None, {"running": False}, "UNREACHABLE"),
    ("ollama", [{"id": "llama3.2:latest"}], {}, "NOT_FOUND"),
    ("ollama", [{"id": "qwen3:8b", "maxContext": 40960, "maxContextOk": False}], {}, "CONTEXT_TOO_SMALL"),
    ("ollama", [{"id": "qwen3:8b", "maxContextOk": True}],
     {"load_error": ("LOAD_FAILED", "model requires more system memory")}, "LOAD_FAILED"),
    # Ollama gave it its own small context: the agent could not answer on it.
    ("ollama", [{"id": "qwen3:8b", "maxContextOk": True}], {"load_context": 4096}, "CONTEXT_NOT_APPLIED"),
    # LM Studio loaded it with less than asked (it lacked the memory).
    ("lmstudio", [{"id": "google/gemma-4-e2b", "maxContextOk": True}], {"load_context": 8192}, "CONTEXT_NOT_APPLIED"),
])
def test_use_local_changes_nothing_unless_the_agent_can_run_on_it(tmp_path, monkeypatch, server, models, kwargs, code):
    from flowly.channels import feature_rpc

    path = _home(tmp_path, monkeypatch, {"agents": {"defaults": {"model": "gpt-5.5"}}})
    before = path.read_text(encoding="utf-8")
    _fake_server(monkeypatch, models=models, **kwargs)
    model = (models or [{"id": "qwen3:8b"}])[0]["id"] if code != "NOT_FOUND" else "qwen3:8b"
    with pytest.raises(feature_rpc.FeatureRpcError) as error:
        _run(feature_rpc.provider_use_local({"server": server, "model": model}))
    assert error.value.code == code
    if code == "CONTEXT_NOT_APPLIED":
        # Says how much it got, how much is needed, and what to set.
        assert f"{NEED:,}" in error.value.message and "98,304" in error.value.message
    assert path.read_text(encoding="utf-8") == before


def test_provider_local_reports_the_need_it_judges_against(monkeypatch):
    from flowly.channels import feature_rpc

    seen: list[int] = []

    async def fake_scan(client=None, *, required=None):
        seen.append(required)
        return [{"id": "ollama", "running": False}, {"id": "lmstudio", "running": False}]

    monkeypatch.setattr(local_models, "scan", fake_scan)
    monkeypatch.setattr(local_models, "agent_need", lambda: (NEED, True))
    result = _run(feature_rpc.provider_local({}))
    assert seen == [NEED]
    assert [s["id"] for s in result["servers"]] == ["ollama", "lmstudio"]
    assert result["requiredContext"] == NEED and result["requiredMeasured"] is True
    assert result["recommendedContext"] == 98_304 and result["minContext"] == NEED


def test_before_any_request_the_need_is_the_cautious_default(monkeypatch, tmp_path):
    _home(tmp_path, monkeypatch, {})
    assert local_models.agent_need() == (window_need.UNMEASURED_REQUIRED, False)
    window_need.record_fixed_overhead(68_013)
    need, measured = local_models.agent_need()
    assert measured is True and need == window_need.required_window(68_013, 20_000)


def test_local_slots_stay_hidden_in_older_apps(tmp_path, monkeypatch):
    """The iOS, Android and older Desktop lists show a ``url`` row only once it
    is set up or in use, but treat an unknown kind as one more provider. The
    local slots keep ``url``, and are not "set up" on a fresh config."""
    from flowly.channels import feature_rpc

    _home(tmp_path, monkeypatch, {})
    rows = {row["key"]: row for row in feature_rpc.provider_list()["providers"]}
    for key in ("ollama", "vllm"):
        assert rows[key]["kind"] == "url", key
        assert rows[key]["hasKey"] is False and rows[key]["isActive"] is False, key


def test_the_rpcs_are_dispatched():
    from flowly.channels import feature_rpc

    assert feature_rpc._DISPATCH["provider.local"][0] is feature_rpc.provider_local
    assert feature_rpc._DISPATCH["provider.use_local"][0] is feature_rpc.provider_use_local


def test_the_catalogue_carries_the_running_context_to_compaction(tmp_path, monkeypatch):
    """Compaction sizes the conversation from the catalogue: it must see the
    context the server runs the model with, not the trained window it would cut."""
    from flowly.integrations import model_catalog

    _home(tmp_path, monkeypatch, {})
    handler, _ = _ollama_app(running={"qwen3:8b": 131072}, shows={"qwen3:8b": QWEN, "nomic-embed-text:latest": EMBED})
    real_client = httpx.AsyncClient
    monkeypatch.setattr(model_catalog.httpx, "AsyncClient",
                        lambda *a, **k: real_client(transport=httpx.MockTransport(handler)))
    model_catalog.flush_cache()

    models = _run(model_catalog.fetch_models("ollama", force_refresh=True))

    assert [m.id for m in models] == ["qwen3:8b"]
    assert models[0].context_window == 131072 and "tools" in models[0].tags
    assert model_catalog.get_context_window("qwen3:8b") == 131072
    model_catalog.flush_cache()


def test_the_catalogue_reads_lm_studios_loaded_context(tmp_path, monkeypatch):
    from flowly.integrations import model_catalog

    _home(tmp_path, monkeypatch, {"providers": {"vllm": {"apiBase": "http://127.0.0.1:1234/v1"}}})
    app = _LMStudio(_lms_entry(contexts=(98_304,)),
                    _lms_entry("text-embedding-nomic-embed-text-v1.5", kind="embedding", max_context=2048))
    real_client = httpx.AsyncClient
    monkeypatch.setattr(model_catalog.httpx, "AsyncClient",
                        lambda *a, **k: real_client(transport=httpx.MockTransport(app)))
    model_catalog.flush_cache()

    models = _run(model_catalog.fetch_models("vllm", force_refresh=True))

    assert [m.id for m in models] == ["google/gemma-4-e2b"]
    assert models[0].context_window == 98_304
    model_catalog.flush_cache()


# ── a local server's catalogue stays current ────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["vllm", "ollama"])
async def test_a_model_deleted_on_the_local_server_leaves_the_picker(monkeypatch, key):
    """2026-11-07: Gemma was deleted in LM Studio and Bonsai downloaded, but
    Desktop's model list still showed Gemma. The gateway kept a local
    server's catalogue from its first read for as long as it ran."""
    from flowly.integrations import model_catalog

    model_catalog.flush_cache()
    on_server = [["google/gemma-4-e2b"]]
    now = [1000.0]

    async def fetch():
        return [model_catalog.Model(id=mid, name=mid) for mid in on_server[0]]

    monkeypatch.setitem(model_catalog._FETCHERS, key, fetch)
    monkeypatch.setattr(model_catalog, "_now", lambda: now[0])

    assert [m.id for m in await model_catalog.fetch_models(key)] == ["google/gemma-4-e2b"]
    on_server[0] = ["prism-ml/bonsai-27b"]
    now[0] += 1  # the same list opened twice in a row: no second scan
    assert [m.id for m in await model_catalog.fetch_models(key)] == ["google/gemma-4-e2b"]
    now[0] += model_catalog.LOCAL_CATALOGUE_TTL_SECONDS
    assert [m.id for m in await model_catalog.fetch_models(key)] == ["prism-ml/bonsai-27b"]
    model_catalog.flush_cache()


@pytest.mark.asyncio
async def test_a_cloud_catalogue_is_still_read_once(monkeypatch):
    from flowly.integrations import model_catalog

    model_catalog.flush_cache()
    reads = []

    async def fetch():
        reads.append(1)
        return [model_catalog.Model(id="x-ai/grok-4.7", name="Grok")]

    monkeypatch.setitem(model_catalog._FETCHERS, "openrouter", fetch)
    monkeypatch.setattr(model_catalog, "_now", lambda: 10_000_000.0 + len(reads) * 3600)
    await model_catalog.fetch_models("openrouter")
    await model_catalog.fetch_models("openrouter")
    assert len(reads) == 1
    model_catalog.flush_cache()
