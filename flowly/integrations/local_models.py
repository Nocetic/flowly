"""Model servers on this computer: Ollama and LM Studio.

Both speak the OpenAI chat API, so the agent runs on them through the
``ollama`` and ``vllm`` (generic local) provider slots. This module finds
them at their default addresses, lists the models they hold, and says what
the app needs to know before one is picked:

- whether the model can call tools (an agent that cannot is a chat box);
- how much context it will actually get. A local server runs a model with
  its own context length, often 4096 tokens (Ollama's default below 24 GiB
  of VRAM, LM Studio's for a model loaded on demand). The agent's own
  instructions and tools are longer than that, and the OpenAI API has no
  way to ask for more: the server silently drops the start of the prompt.
  So the length is read from the server (Ollama ``/api/ps`` for a loaded
  model, a ``num_ctx`` the model was built with; LM Studio's loaded length)
  and reported against :data:`MIN_CONTEXT`.

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

#: Below this the agent's instructions, tools and the reply reserve leave no
#: room for the conversation (compaction keeps 20k tokens free on its own).
MIN_CONTEXT = 32_768
#: What Ollama's own documentation asks of agents and coding tools.
RECOMMENDED_CONTEXT = 65_536

_SCAN_TIMEOUT = httpx.Timeout(2.0, connect=0.5)
_SHOW_CONCURRENCY = 4
_MAX_MODELS = 60
_LOAD_TIMEOUT = httpx.Timeout(180.0, connect=2.0)


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

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "tools": self.tools,
            "maxContext": self.max_context,
            "context": self.context,
            "contextOk": None if self.context is None else self.context >= MIN_CONTEXT,
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
#: after Ollama unloads it (keep-alive) — it loads again with the same one.
_SEEN_CONTEXT: dict[str, int] = {}


def _positive_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


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
    )


async def _scan_ollama(client: httpx.AsyncClient, server: LocalServer) -> list[LocalModel]:
    tags = (await client.get(f"{server.root}/api/tags")).raise_for_status().json()
    entries = [m for m in tags.get("models") or [] if isinstance(m, dict) and isinstance(m.get("name"), str)]
    entries = entries[:_MAX_MODELS]
    running: dict[str, int] = {}
    try:
        ps = (await client.get(f"{server.root}/api/ps")).raise_for_status().json()
        for item in ps.get("models") or []:
            if isinstance(item, dict) and isinstance(item.get("name"), str):
                running[item["name"]] = _positive_int(item.get("context_length")) or 0
    except (httpx.HTTPError, ValueError):
        pass
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


# ── LM Studio ───────────────────────────────────────────────────────────────


def _lmstudio_model(item: dict) -> LocalModel | None:
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


async def _scan_lmstudio(client: httpx.AsyncClient, server: LocalServer) -> list[LocalModel]:
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


# ── scanning ────────────────────────────────────────────────────────────────


async def scan_server(server: LocalServer, client: httpx.AsyncClient | None = None) -> dict[str, Any]:
    """One server: whether it is installed and running, and its models."""
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
        scan = _scan_ollama if server.id == "ollama" else _scan_lmstudio
        models = await scan(client, server)
        result["running"] = True
        result["models"] = [model.to_dict() for model in models]
    except (httpx.ConnectError, httpx.ConnectTimeout):
        pass  # not running
    except (httpx.HTTPError, ValueError) as exc:
        # Something answers there but not as this server does.
        result["error"] = type(exc).__name__
    finally:
        if owned:
            await client.aclose()
    result["installed"] = result["running"] or _installed(server)
    return result


async def scan(client: httpx.AsyncClient | None = None) -> list[dict[str, Any]]:
    """Every known server on this computer, side by side."""
    return list(await asyncio.gather(*(scan_server(server, client) for server in SERVERS)))


async def load(server: LocalServer, model: str, client: httpx.AsyncClient | None = None) -> int | None:
    """Load ``model`` into memory now, so the first message does not wait and
    a model too big for this computer fails here, not mid-conversation.
    Returns the context the server runs it with, when it says.

    Ollama loads on an empty generate request. LM Studio loads a model the
    first time it is asked (just in time), so there is nothing to do here.
    """
    if server.id != "ollama":
        return None
    owned = client is None
    client = client or httpx.AsyncClient(timeout=_LOAD_TIMEOUT)
    try:
        try:
            response = await client.post(f"{server.root}/api/generate", json={"model": model})
        except httpx.HTTPError as exc:
            raise LocalServerError("LOAD_FAILED", f"{server.name} could not load {model}: {type(exc).__name__}") from exc
        if response.status_code >= 400:
            detail = ""
            try:
                detail = str(response.json().get("error") or "")[:300]
            except ValueError:
                pass
            raise LocalServerError("LOAD_FAILED", detail or f"{server.name} could not load {model} (HTTP {response.status_code})")
        try:
            ps = (await client.get(f"{server.root}/api/ps")).raise_for_status().json()
        except (httpx.HTTPError, ValueError):
            return None
        for item in ps.get("models") or []:
            if isinstance(item, dict) and item.get("name") == model:
                context = _positive_int(item.get("context_length"))
                if context:
                    _SEEN_CONTEXT[model] = context
                return context
        return None
    finally:
        if owned:
            await client.aclose()
