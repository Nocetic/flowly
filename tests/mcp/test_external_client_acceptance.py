"""Opt-in installed-client acceptance using local fixed-response model APIs.

Set FLOWLY_MCP_EXTERNAL_CLIENT_SPEC to an isolated launch-adapter JSON file.
No external executable, personal configuration or paid API is selected by default.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import shutil
import socket
import threading
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from aiohttp import web

from flowly.agent.tool_context import tool_execution_scope
from flowly.mcp.server.managed_tools import managed_tool_launch
from flowly.mcp.server.tool_bridge import GRANT_ENV
from tests.mcp import test_live_tool_bridge as support

runtime = support.runtime
MODEL = "acceptance-model"
TITLE = "External client acceptance card"
FINAL = "FLOWLY_EXTERNAL_ACCEPTANCE_OK"


def expand(value, replacements):
    if isinstance(value, dict):
        return {key: expand(item, replacements) for key, item in value.items()}
    if isinstance(value, list):
        return [expand(item, replacements) for item in value]
    if isinstance(value, str):
        if value in replacements:
            return replacements[value]
        for key, replacement in replacements.items():
            if isinstance(replacement, str):
                value = value.replace(key, replacement)
    return value


class ModelPeer:
    def __init__(self, style, actions):
        self.style = style
        self.actions = actions
        self.requests = []
        self.models = []
        self.failure = None

    async def handle(self, request):
        body = await request.json()
        if "count_tokens" in request.path or "countTokens" in request.path:
            return web.json_response({"input_tokens": 10, "totalTokens": 10})
        self.requests.append(body)
        self.models.append(body.get("model") if self.style == "messages" else
                           request.path.split("/models/")[-1].split(":")[0])
        if len(self.requests) > 5:
            return web.json_response({"error": "Acceptance request budget exceeded"}, status=400)
        if self.style == "messages":
            definitions = body.get("tools", [])
        else:
            definitions = [definition for group in body.get("tools", [])
                           for definition in group.get("functionDeclarations", [])]
        names = [tool.get("name", "") for tool in definitions]
        finish = len(self.requests) > len(self.actions)
        expected, arguments = self.actions[min(len(self.requests), len(self.actions)) - 1]
        matches = [name for name in names if name.endswith(expected)]
        if not finish and len(matches) != 1:
            self.failure = {"expected": expected, "names": names}
            return web.json_response({"error": "Expected granted tool is absent or ambiguous"}, status=400)
        name = matches[0] if matches else ""
        if self.style == "messages":
            block = {"type": "text", "text": FINAL} if finish else {
                "type": "tool_use", "id": f"acceptance_{len(self.requests)}", "name": name, "input": arguments,
            }
            result = {
                "id": f"message_{len(self.requests)}", "type": "message", "role": "assistant",
                "model": body.get("model"), "content": [block],
                "stop_reason": "end_turn" if finish else "tool_use", "stop_sequence": None,
                "usage": {"input_tokens": 10, "output_tokens": 10},
            }
            if not body.get("stream"):
                return web.json_response(result)
            events = [
                ("message_start", {"type": "message_start", "message": result | {"content": [], "stop_reason": None}}),
                ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": (
                    {"type": "text", "text": ""} if finish else block | {"input": {}}
                )}),
                ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": (
                    {"type": "text_delta", "text": FINAL} if finish else
                    {"type": "input_json_delta", "partial_json": json.dumps(arguments)}
                )}),
                ("content_block_stop", {"type": "content_block_stop", "index": 0}),
                ("message_delta", {"type": "message_delta", "delta": {
                    "stop_reason": result["stop_reason"], "stop_sequence": None,
                }, "usage": {"output_tokens": 10}}),
                ("message_stop", {"type": "message_stop"}),
            ]
            text = "".join(f"event: {kind}\ndata: {json.dumps(data)}\n\n" for kind, data in events)
        else:
            part = {"text": FINAL} if finish else {"functionCall": {"name": name, "args": arguments}}
            result = {
                "candidates": [{"content": {"role": "model", "parts": [part]}, "finishReason": "STOP", "index": 0}],
                "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 10, "totalTokenCount": 20},
            }
            if "streamGenerateContent" not in request.path:
                return web.json_response(result)
            text = f"data: {json.dumps(result)}\n\n"
        return web.Response(text=text, content_type="text/event-stream")


@pytest.fixture
def adapter():
    spec_path = os.environ.get("FLOWLY_MCP_EXTERNAL_CLIENT_SPEC")
    if not spec_path:
        pytest.skip("requires an explicit isolated external-client launch adapter")
    spec = json.loads(Path(spec_path).read_text())
    assert spec["style"] in {"messages", "generate_content"}
    assert "$API" in spec["env"].values(), "The model API must be routed to the local fixture"
    if spec["command"][0] == "$CLIENT":
        executable = os.environ.get("FLOWLY_MCP_EXTERNAL_CLIENT_EXECUTABLE", "")
        resolved = shutil.which(executable) if executable else None
        assert resolved, "An installed FLOWLY_MCP_EXTERNAL_CLIENT_EXECUTABLE is required"
        spec["command"][0] = resolved
    return spec


@asynccontextmanager
async def model_api(peer):
    app = web.Application()
    app.router.add_post("/{path:.*}", peer.handle)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    try:
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        yield f"http://127.0.0.1:{runner.addresses[0][1]}"
    finally:
        await runner.cleanup()


async def run_adapter(adapter, root, api_url, mcp_servers, *, extra_env=None):
    state, project = root / "external-state", root / "external-project"
    state.mkdir()
    project.mkdir()
    replacements = {
        "$API": api_url, "$STATE": str(state), "$PROJECT": str(project),
        "$MCP_SERVERS": mcp_servers, "$MODEL": MODEL,
        "$MCP_CONFIG": json.dumps({"mcpServers": mcp_servers}),
    }
    for filename, contents in adapter.get("files", {}).items():
        destination = state / filename
        assert destination.resolve().is_relative_to(state.resolve())
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(expand(contents, replacements)))
        destination.chmod(0o600)
    # Do not inherit account keys, plugins, IDE sockets, proxies or shell
    # startup overrides from the test runner's environment.
    env = {key: os.environ[key] for key in ("PATH", "SYSTEMROOT", "WINDIR", "TMPDIR") if key in os.environ}
    env.update(expand(adapter["env"], replacements))
    env.update(extra_env or {})
    process = await asyncio.create_subprocess_exec(
        *expand(adapter["command"], replacements), cwd=project, env=env,
        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=60)
        output = stdout.decode(errors="replace")
        assert process.returncode == 0, (process.returncode, output, stderr.decode(errors="replace"))
        assert FINAL in output
        return state, output
    finally:
        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.communicate(), 5)
            except TimeoutError:
                process.kill()
                await process.communicate()


def transcript(peer):
    assert peer.failure is None, peer.failure
    assert len(peer.requests) == len(peer.actions) + 1
    assert set(peer.models) == {MODEL}
    return json.dumps(peer.requests)


@pytest.mark.parametrize("writes", [False, True], ids=["readonly", "write-and-read"])
async def test_installed_client_uses_live_grants_without_personal_state_or_paid_models(runtime, tmp_path, adapter, writes):
    actions = ([("board_add", {"title": TITLE})] if writes else []) + [("board_list", {})]
    peer = ModelPeer(adapter["style"], actions)
    names = {"board_list", "board_add"} if writes else {"board_list"}
    async with model_api(peer) as api_url:
        with tool_execution_scope("cli:first", allowed_tools=frozenset(names)):
            async with managed_tool_launch(runtime, "cli:first", allow_writes=writes) as launch:
                mcp_servers = {"flowly": {
                    "command": launch.command, "args": launch.args,
                    # Scoped credentials travel only in the launch environment.
                    "env": {key: value for key, value in launch.env.items() if key != GRANT_ENV},
                } | adapter.get("mcp_options", {})}
                state, output = await run_adapter(adapter, tmp_path, api_url, mcp_servers, extra_env={
                    GRANT_ENV: launch.env[GRANT_ENV],
                })
                seen = transcript(peer)
                cards = runtime.store.list_cards()
                if writes:
                    assert len(cards) == 1 and cards[0].title == TITLE
                    assert cards[0].origin_chat_id == "first"
                    assert cards[0].id in seen  # Real tool results reached the model client.
                else:
                    assert not cards
                    assert "board_add" not in seen
                credential = json.loads(launch.env[GRANT_ENV])["token"]
                assert credential not in seen and credential not in output
                for path in state.rglob("*"):
                    if path.is_file():
                        assert credential.encode() not in path.read_bytes(), str(path)
        assert not runtime._external_tool_bridge._grants


@asynccontextmanager
async def public_http_server():
    import uvicorn

    from flowly.mcp.server import readplane
    from flowly.mcp.server.serve import create_server

    readplane.get_session_reader.cache_clear()
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    url = f"http://127.0.0.1:{listener.getsockname()[1]}/mcp"
    token = secrets.token_urlsafe(32)
    mcp = create_server(auth_token=token, resource_url=url)
    app = mcp.streamable_http_app(streamable_http_path="/mcp", json_response=True, stateless_http=True)
    server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
    thread = threading.Thread(target=lambda: server.run(sockets=[listener]), daemon=True)
    thread.start()
    try:
        async with asyncio.timeout(5):
            while not server.started:
                assert thread.is_alive()
                await asyncio.sleep(0.01)
        yield url, token
    finally:
        server.should_exit = True
        await asyncio.to_thread(thread.join, 5)
        assert not thread.is_alive()
        listener.close()
        reader = readplane.get_session_reader()
        if reader._indexer:
            reader._indexer.close()
        readplane.get_session_reader.cache_clear()


async def test_installed_client_reads_public_authenticated_http_server(runtime, tmp_path, adapter):
    import aiohttp

    nonce = "http-acceptance-" + secrets.token_hex(12)
    session = runtime.sessions.get_or_create("cli:first")
    session.add_message("user", nonce)
    runtime.sessions.save(session)
    actions = [("conversation_get", {"session_key": "cli:first"}), ("messages_read", {"session_key": "cli:first"})]
    peer = ModelPeer(adapter["style"], actions)
    async with public_http_server() as (url, token), model_api(peer) as api_url:
        async with aiohttp.ClientSession() as http:
            async with http.post(url, json={"jsonrpc": "2.0", "id": 1, "method": "initialize"}) as response:
                assert response.status == 401
        assert "http_mcp_options" in adapter
        mcp_servers = {"flowly": expand(adapter["http_mcp_options"], {"$MCP_URL": url, "$MCP_TOKEN": token})}
        _state, output = await run_adapter(adapter, tmp_path, api_url, mcp_servers)
        seen = transcript(peer)
        assert nonce in seen
        assert token not in seen and token not in output
        assert not runtime.store.list_cards()


async def test_installed_client_consumes_generated_image_and_audio_results(runtime, tmp_path, adapter):
    import base64
    import wave

    from PIL import Image

    from flowly.agent.reply_media import media_envelope
    from flowly.agent.tools.base import Tool

    image_path, audio_path = runtime.workspace / "result.png", runtime.workspace / "result.wav"
    Image.new("RGB", (16, 16), color=(30, 90, 150)).save(image_path)
    with wave.open(str(audio_path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(8000)
        output.writeframes(b"\x00\x00" * 80)
    executed = []

    class MediaTool(Tool):
        description = "Generate local acceptance media"
        parameters = {"type": "object", "properties": {}}

        def __init__(self, name, path):
            self._name, self.path = name, path

        @property
        def name(self):
            return self._name

        async def execute(self):
            executed.append(self.name)
            return media_envelope([str(self.path)], "Native media output " + self.path.name)

    for name, path in (("image_generate", image_path), ("voice_generate", audio_path)):
        runtime.tools.register(MediaTool(name, path))
    actions = [("image_generate", {}), ("voice_generate", {})]
    peer = ModelPeer(adapter["style"], actions)
    async with model_api(peer) as api_url:
        with tool_execution_scope("cli:first", allowed_tools=frozenset(name for name, _ in actions)):
            async with managed_tool_launch(runtime, "cli:first", allow_writes=True) as launch:
                servers = {"flowly": {
                    "command": launch.command, "args": launch.args,
                    "env": {key: value for key, value in launch.env.items() if key != GRANT_ENV},
                } | adapter.get("mcp_options", {})}
                state, _output = await run_adapter(adapter, tmp_path, api_url, servers, extra_env={GRANT_ENV: launch.env[GRANT_ENV]})
                seen = transcript(peer)
                assert executed == [name for name, _ in actions]
                assert "Native media output result.png" in seen
                assert "Native media output result.wav" in seen
                assert "image/png" in seen
                assert "audio/wav" in seen
                assert '"is_error": true' not in seen
                # The result must carry usable data, not only a success caption.
                # A client may inline it for its model or materialize an artifact.
                for path in (image_path, audio_path):
                    raw = path.read_bytes()
                    assert base64.b64encode(raw).decode() in seen or any(
                        candidate.read_bytes() == raw for candidate in state.rglob("*") if candidate.is_file()
                    ), f"The external client discarded {path.suffix} content"
