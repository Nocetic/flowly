"""Model servers on this computer: Ollama and LM Studio.

Both speak the OpenAI chat API, so the agent runs on them through the
``ollama`` and ``vllm`` (generic local) provider slots. This module finds
them at their default addresses, lists the models they hold, and says what
the app needs to know before one is picked:

- whether the model can call tools (an agent that cannot is a chat box);
- whether the context the server runs it with holds the agent. Every request
  carries the agent's instructions and tool schemas, measured on each request
  (flowly/compaction/window_need.py; 55-68K tokens on a Desktop agent), and a
  local server runs a model with its own context length, often 4-8K. The
  OpenAI API has no field to ask for more, so a request that does not fit is
  refused before it is sent, and the agent cannot answer at all.

So a model is run only with enough context:

- LM Studio is told the context to load the model with (its REST API,
  ``/api/v1/models/load``); a model it holds with less is unloaded and loaded
  again.
- Ollama decides the context itself (its app setting or
  ``OLLAMA_CONTEXT_LENGTH``; its OpenAI route ignores ``num_ctx``, see
  ollama/openai/openai.go). The model is loaded and the context Ollama gives
  it is read back (``/api/ps``); when it is too small nothing is switched and
  the owner is told what to set.
- A model whose trained window is below the need is refused outright.

Only the servers' fixed loopback addresses are probed, with a short timeout,
so a scan on a machine without them (a cloud agent) costs nothing.
"""

from __future__ import annotations

import asyncio
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from flowly.compaction.window_need import load_size, required_context
from flowly.providers.effort import NO_THINKING_CONTROL, ThinkingControl, lmstudio_thinking, ollama_thinking

_SCAN_TIMEOUT = httpx.Timeout(2.0, connect=0.5)
_SHOW_CONCURRENCY = 4
_MAX_MODELS = 60
#: Reading a large model from disk into memory.
_LOAD_TIMEOUT = httpx.Timeout(300.0, connect=2.0)


@dataclass(frozen=True)
class LocalServer:
    id: str
    name: str
    #: The provider slot the agent runs it through.
    slot: str
    #: Its OpenAI-compatible endpoint, as the provider slot stores it.
    base: str
    #: Its native API root.
    root: str


SERVERS: tuple[LocalServer, ...] = (
    LocalServer("ollama", "Ollama", "ollama", "http://127.0.0.1:11434/v1", "http://127.0.0.1:11434"),
    LocalServer("lmstudio", "LM Studio", "vllm", "http://127.0.0.1:1234/v1", "http://127.0.0.1:1234"),
)


def server_by_id(server_id: str) -> LocalServer | None:
    return next((server for server in SERVERS if server.id == server_id), None)


def agent_need() -> tuple[int, bool]:
    """``(tokens, measured)``: the context this agent needs, from its own
    requests when there were any (window_need.py), with the configured reply
    reserve."""
    reserve = 20_000
    try:
        from flowly.config.loader import load_config

        reserve = int(load_config().agents.defaults.compaction.reserve_tokens_floor)
    except Exception:  # noqa: BLE001 - the schema default
        pass
    return required_context(reserve)


@dataclass
class LocalModel:
    id: str
    #: True / False when the server says; None when it does not.
    tools: bool | None = None
    #: The longest context the model was trained for.
    max_context: int | None = None
    #: The context the server runs it with now (None: not known until loaded).
    context: int | None = None
    loaded: bool = False
    size: int | None = None
    parameters: str | None = None
    #: An Ollama cloud model: it runs on Ollama's servers, not this computer.
    cloud: bool = False
    #: How its thinking can be set (flowly/providers/effort.py).
    thinking: ThinkingControl = NO_THINKING_CONTROL

    def to_dict(self, required: int) -> dict[str, Any]:
        return {
            "id": self.id,
            "tools": self.tools,
            "maxContext": self.max_context,
            #: The model can hold the agent at all (its trained window).
            "maxContextOk": None if self.max_context is None else self.max_context >= required,
            "context": self.context,
            #: The server runs it with enough now.
            "contextOk": None if self.context is None else self.context >= required,
            "loaded": self.loaded,
            "size": self.size,
            "parameters": self.parameters,
            "cloud": self.cloud,
        }


class LocalServerError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _positive_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _error_text(response: httpx.Response) -> str:
    """A server's own reason for a refusal (both servers answer ``{"error": …}``)."""
    try:
        body = response.json()
    except ValueError:
        return response.text[:300].strip()
    error = body.get("error") if isinstance(body, dict) else None
    if isinstance(error, dict):
        error = error.get("message")
    return str(error or "")[:300].strip()


# ── what is installed ───────────────────────────────────────────────────────


def _installed(server: LocalServer) -> bool:
    """The app is on this computer (it may still be off)."""
    home = Path.home()
    if server.id == "ollama":
        return bool(shutil.which("ollama")) or Path("/Applications/Ollama.app").exists()
    return (home / ".lmstudio").is_dir() or Path("/Applications/LM Studio.app").exists()


# ── Ollama ──────────────────────────────────────────────────────────────────

_NUM_CTX = re.compile(r"^\s*num_ctx\s+(\d+)\s*$", re.MULTILINE)

#: The context a model ran with when last seen loaded, so it is still known
#: after Ollama unloads it (keep-alive): it loads again with the same one.
_SEEN_CONTEXT: dict[str, int] = {}


def _ollama_model(name: str, tag: dict, show: dict | None, running: dict[str, int]) -> LocalModel | None:
    capabilities = show.get("capabilities") if isinstance(show, dict) else None
    if isinstance(capabilities, list) and capabilities and "completion" not in capabilities:
        return None  # an embedding model: it cannot hold a conversation
    model_info = show.get("model_info") if isinstance(show, dict) else None
    max_context = None
    if isinstance(model_info, dict):
        max_context = next(
            (_positive_int(v) for k, v in model_info.items() if k.endswith(".context_length") and _positive_int(v)),
            None,
        )
    built = None
    params = show.get("parameters") if isinstance(show, dict) else None
    if isinstance(params, str):
        found = _NUM_CTX.search(params)
        built = int(found.group(1)) if found else None
    loaded = name in running
    context = running.get(name) or built or _SEEN_CONTEXT.get(name)
    if loaded and running.get(name):
        _SEEN_CONTEXT[name] = running[name]
    details = tag.get("details") if isinstance(tag.get("details"), dict) else {}
    return LocalModel(
        id=name,
        tools=("tools" in capabilities) if isinstance(capabilities, list) and capabilities else None,
        max_context=max_context,
        context=context,
        loaded=loaded,
        size=_positive_int(tag.get("size")),
        parameters=details.get("parameter_size") if isinstance(details.get("parameter_size"), str) else None,
        cloud=name.endswith(":cloud") or name.endswith("-cloud") or tag.get("remote_host") is not None,
        thinking=ollama_thinking(name, capabilities),
    )


async def _ollama_running(client: httpx.AsyncClient, server: LocalServer) -> dict[str, int]:
    running: dict[str, int] = {}
    try:
        ps = (await client.get(f"{server.root}/api/ps")).raise_for_status().json()
    except (httpx.HTTPError, ValueError):
        return running
    for item in ps.get("models") or []:
        if isinstance(item, dict) and isinstance(item.get("name"), str):
            running[item["name"]] = _positive_int(item.get("context_length")) or 0
    return running


async def _scan_ollama(client: httpx.AsyncClient, server: LocalServer) -> list[LocalModel]:
    tags = (await client.get(f"{server.root}/api/tags")).raise_for_status().json()
    entries = [m for m in tags.get("models") or [] if isinstance(m, dict) and isinstance(m.get("name"), str)]
    entries = entries[:_MAX_MODELS]
    running = await _ollama_running(client, server)
    gate = asyncio.Semaphore(_SHOW_CONCURRENCY)

    async def show(name: str) -> dict | None:
        async with gate:
            try:
                response = await client.post(f"{server.root}/api/show", json={"model": name})
                return response.raise_for_status().json()
            except (httpx.HTTPError, ValueError):
                return None

    shows = await asyncio.gather(*(show(entry["name"]) for entry in entries))
    models = [_ollama_model(entry["name"], entry, info, running) for entry, info in zip(entries, shows)]
    return [model for model in models if model is not None]


async def _load_ollama(client: httpx.AsyncClient, server: LocalServer, model: str) -> int | None:
    """Load ``model`` now (an empty generate request) and read back the
    context Ollama gave it. Ollama decides that context; Flowly cannot."""
    try:
        response = await client.post(f"{server.root}/api/generate", json={"model": model}, timeout=_LOAD_TIMEOUT)
    except httpx.HTTPError as exc:
        raise LocalServerError("LOAD_FAILED", f"{server.name} could not load {model}: {type(exc).__name__}") from exc
    if response.status_code >= 400:
        raise LocalServerError(
            "LOAD_FAILED", _error_text(response) or f"{server.name} could not load {model} (HTTP {response.status_code})")
    context = (await _ollama_running(client, server)).get(model) or None
    if context:
        _SEEN_CONTEXT[model] = context
    return context


# ── LM Studio ───────────────────────────────────────────────────────────────


def _lmstudio_v1_model(item: dict) -> LocalModel | None:
    """One entry of ``/api/v1/models`` (LM Studio's REST API)."""
    key = item.get("key")
    if not isinstance(key, str) or not key or item.get("type") != "llm":
        return None  # embedding and decision models cannot hold a conversation
    instances = [i for i in item.get("loaded_instances") or [] if isinstance(i, dict)]
    contexts = [_positive_int((i.get("config") or {}).get("context_length")) for i in instances]
    context = max((c for c in contexts if c), default=None)
    capabilities = item.get("capabilities") if isinstance(item.get("capabilities"), dict) else {}
    tools = capabilities.get("trained_for_tool_use")
    return LocalModel(
        id=key,
        tools=tools if isinstance(tools, bool) else None,
        max_context=_positive_int(item.get("max_context_length")),
        context=context,
        loaded=bool(instances),
        size=_positive_int(item.get("size_bytes")),
        parameters=item.get("params_string") if isinstance(item.get("params_string"), str) else None,
        thinking=lmstudio_thinking(capabilities),
    )


def _lmstudio_model(item: dict) -> LocalModel | None:
    """One entry of the older ``/api/v0/models`` (LM Studio 0.3.6+)."""
    model_id = item.get("id")
    if not isinstance(model_id, str) or not model_id:
        return None
    if item.get("type") == "embeddings":
        return None
    capabilities = item.get("capabilities")
    loaded = item.get("state") == "loaded"
    return LocalModel(
        id=model_id,
        tools=("tool_use" in capabilities) if isinstance(capabilities, list) else None,
        max_context=_positive_int(item.get("max_context_length")),
        context=_positive_int(item.get("loaded_context_length")) if loaded else None,
        loaded=loaded,
    )


async def _lmstudio_v1_entries(client: httpx.AsyncClient, server: LocalServer) -> list[dict] | None:
    """``/api/v1/models``' entries; None when this LM Studio has no v1 API."""
    response = await client.get(f"{server.root}/api/v1/models")
    if response.status_code == 404:
        return None
    body = response.raise_for_status().json()
    return [item for item in body.get("models") or [] if isinstance(item, dict)]


async def _scan_lmstudio(client: httpx.AsyncClient, server: LocalServer) -> list[LocalModel]:
    entries = await _lmstudio_v1_entries(client, server)
    if entries is not None:
        models = [_lmstudio_v1_model(item) for item in entries[:_MAX_MODELS]]
        return [model for model in models if model is not None]
    response = await client.get(f"{server.root}/api/v0/models")
    if response.status_code == 404:
        # Before LM Studio 0.3.6 only the OpenAI list exists: ids, no types.
        listed = (await client.get(f"{server.base}/models")).raise_for_status().json()
        items = [
            {"id": item.get("id")}
            for item in listed.get("data") or []
            if isinstance(item, dict) and not str(item.get("id", "")).startswith("text-embedding")
        ]
    else:
        items = [item for item in response.raise_for_status().json().get("data") or [] if isinstance(item, dict)]
    models = [_lmstudio_model(item) for item in items[:_MAX_MODELS]]
    return [model for model in models if model is not None]


async def _load_lmstudio(client: httpx.AsyncClient, server: LocalServer, model: str, required: int) -> int | None:
    """Have LM Studio hold ``model`` with at least ``required`` tokens of
    context: kept when it already does; else every instance it holds with less
    is unloaded and the model loaded again with enough. Returns the context it
    then runs with. An LM Studio without the v1 API loads on demand, with its
    own context (None)."""
    entries = await _lmstudio_v1_entries(client, server)
    if entries is None:
        return None
    entry = next((item for item in entries if item.get("key") == model), None)
    if entry is None:
        raise LocalServerError("NOT_FOUND", f"{server.name} has no model named {model}")
    found = _lmstudio_v1_model(entry)
    if found is None:
        raise LocalServerError("NOT_FOUND", f"{model} is not a chat model")
    if found.max_context is not None and found.max_context < required:
        raise LocalServerError(
            "CONTEXT_TOO_SMALL",
            f"{model} holds at most {found.max_context:,} tokens of context; your agent needs {required:,}.")
    instances = [i for i in entry.get("loaded_instances") or [] if isinstance(i, dict)]
    for instance in instances:
        context = _positive_int((instance.get("config") or {}).get("context_length"))
        if context and context >= required:
            return context
    for instance in instances:
        instance_id = instance.get("id")
        if not isinstance(instance_id, str) or not instance_id:
            continue
        try:
            response = await client.post(
                f"{server.root}/api/v1/models/unload", json={"instance_id": instance_id},
                timeout=httpx.Timeout(60.0, connect=2.0))
        except httpx.HTTPError as exc:
            raise LocalServerError("LOAD_FAILED", f"{server.name} could not unload {model}: {type(exc).__name__}") from exc
        if response.status_code >= 400:
            raise LocalServerError("LOAD_FAILED", _error_text(response) or f"{server.name} could not unload {model}")
    payload = {"model": model, "context_length": load_size(required, found.max_context), "echo_load_config": True}
    try:
        response = await client.post(f"{server.root}/api/v1/models/load", json=payload, timeout=_LOAD_TIMEOUT)
    except httpx.HTTPError as exc:
        raise LocalServerError("LOAD_FAILED", f"{server.name} could not load {model}: {type(exc).__name__}") from exc
    if response.status_code >= 400:
        raise LocalServerError(
            "LOAD_FAILED", _error_text(response) or f"{server.name} could not load {model} (HTTP {response.status_code})")
    try:
        config = response.json().get("load_config")
    except (ValueError, AttributeError):
        config = None
    applied = _positive_int(config.get("context_length")) if isinstance(config, dict) else None
    if applied:
        return applied
    refreshed = await _lmstudio_v1_entries(client, server) or []
    entry = next((item for item in refreshed if item.get("key") == model), None)
    reread = _lmstudio_v1_model(entry) if entry else None
    return reread.context if reread else None


# ── the server behind a running agent ───────────────────────────────────────


async def runtime_window(
    slot: str, base: str, model: str, *, required: int, client: httpx.AsyncClient | None = None,
) -> int | None:
    """The context the server at ``base`` runs ``model`` with, made enough
    where Flowly can: on LM Studio a model it does not hold, or holds with
    less than ``required``, is loaded with enough (as LM Studio would load it
    on the next request anyway, with its own small default). On Ollama it is
    read, never changed. None for any other server, or when it does not say.
    Raises :class:`LocalServerError` when LM Studio refuses."""
    root = base.rstrip("/")
    root = root[: -len("/v1")] if root.endswith("/v1") else root
    owned = client is None
    client = client or httpx.AsyncClient(timeout=_SCAN_TIMEOUT)
    try:
        if slot == "ollama":
            server = LocalServer("ollama", "Ollama", "ollama", base, root)
            context = (await _ollama_running(client, server)).get(model) or None
            if context:
                _SEEN_CONTEXT[model] = context
            return context or _SEEN_CONTEXT.get(model)
        if slot == "vllm":
            server = LocalServer("lmstudio", "LM Studio", "vllm", base, root)
            if await _lmstudio_v1_entries(client, server) is None:
                return None  # vLLM, llama.cpp …: nothing to ask
            return await _load_lmstudio(client, server, model, required)
        return None
    finally:
        if owned:
            await client.aclose()


# ── scanning and loading ────────────────────────────────────────────────────


async def scan_server(
    server: LocalServer, client: httpx.AsyncClient | None = None, *, required: int | None = None,
) -> dict[str, Any]:
    """One server: whether it is installed and running, and its models,
    each judged against the context the agent needs (``required``)."""
    need = required if required is not None else agent_need()[0]
    result: dict[str, Any] = {
        "id": server.id,
        "name": server.name,
        "slot": server.slot,
        "base": server.base,
        "installed": False,
        "running": False,
        "models": [],
    }
    owned = client is None
    client = client or httpx.AsyncClient(timeout=_SCAN_TIMEOUT)
    try:
        scan_one = _scan_ollama if server.id == "ollama" else _scan_lmstudio
        models = await scan_one(client, server)
        result["running"] = True
        result["models"] = [model.to_dict(need) for model in models]
    except (httpx.ConnectError, httpx.ConnectTimeout):
        pass  # not running
    except (httpx.HTTPError, ValueError, AttributeError) as exc:
        # Something answers there but not as this server does.
        result["error"] = type(exc).__name__
    finally:
        if owned:
            await client.aclose()
    result["installed"] = result["running"] or _installed(server)
    return result


async def scan(client: httpx.AsyncClient | None = None, *, required: int | None = None) -> list[dict[str, Any]]:
    """Every known server on this computer, side by side."""
    need = required if required is not None else agent_need()[0]
    return list(await asyncio.gather(*(scan_server(server, client, required=need) for server in SERVERS)))


async def load(
    server: LocalServer, model: str, *, required: int, client: httpx.AsyncClient | None = None,
) -> int | None:
    """Load ``model`` so the first message does not wait and a model too big
    for this computer fails here, not mid-conversation. LM Studio loads it
    with ``required`` tokens of context (or more); Ollama with its own. Returns
    the context the server then runs it with, when it says."""
    owned = client is None
    client = client or httpx.AsyncClient(timeout=_SCAN_TIMEOUT)
    try:
        if server.id == "ollama":
            return await _load_ollama(client, server, model)
        try:
            return await _load_lmstudio(client, server, model, required)
        except (httpx.HTTPError, ValueError) as exc:
            raise LocalServerError("LOAD_FAILED", f"{server.name} did not answer: {type(exc).__name__}") from exc
    finally:
        if owned:
            await client.aclose()
