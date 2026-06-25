"""Tests for the Flowly-tools MCP callback + Codex config migration.

Covers the three new pieces that give the codex_session runtime
reference parity with the upstream "App-Server Runtime" feature:

  * ``flowly.codex.tools_mcp_server`` — the hand-rolled stdio MCP
    server that exposes a curated subset of Flowly tools to Codex.
  * ``flowly.codex.tool_migration`` — idempotent ``~/.codex/config.toml``
    managed-block writer that registers the callback.
  * ``CodexSession``'s ``mcpServer/elicitation/request`` handling —
    auto-accept for our own ``flowly-tools`` server, decline otherwise.
"""

from __future__ import annotations

import asyncio

import pytest

from flowly.codex import tool_migration as tm
from flowly.codex.session import CodexSession, CodexSessionConfig
from flowly.codex.tools_mcp_server import _StdioMCPServer

# ---------------------------------------------------------------------------
# Minimal self-contained fake transport (avoids cross-test-module coupling)
# ---------------------------------------------------------------------------


class _MiniFakeClient:
    """Just enough of CodexAppServerClient to drive one scripted turn."""

    def __init__(self) -> None:
        self._responses: dict[str, list] = {}
        self._server_requests: list[dict] = []
        self._notifications: list[dict] = []
        self.responses_sent: list[tuple] = []
        self.exit_code = None

    def script_response(self, method: str, result) -> None:
        self._responses.setdefault(method, []).append(result)

    def script_server_request(self, req_id: int, method: str, params: dict) -> None:
        self._server_requests.append({"id": req_id, "method": method, "params": params})

    def script_notification(self, method: str, params: dict | None = None) -> None:
        self._notifications.append({"method": method, "params": params or {}})

    async def request(self, method, params=None, *, timeout=None):
        if self._responses.get(method):
            return self._responses[method].pop(0)
        return {}

    async def respond(self, req_id, result) -> None:
        self.responses_sent.append((req_id, result))

    async def respond_error(self, req_id, code, message, data=None) -> None:
        self.responses_sent.append((req_id, {"error": message}))

    async def take_server_request(self, timeout=0):
        return self._server_requests.pop(0) if self._server_requests else None

    async def take_notification(self, timeout=0):
        return self._notifications.pop(0) if self._notifications else None

    def is_alive(self) -> bool:
        return True

    def stderr_tail(self, n: int = 20):
        return []

    async def close(self) -> None:
        pass


@pytest.fixture
def mini_session(monkeypatch):
    def _factory(approval_callback=None):
        fake = _MiniFakeClient()
        session = CodexSession(
            config=CodexSessionConfig(codex_bin="codex-stub", turn_timeout_s=5.0,
                                      post_tool_quiet_timeout_s=2.0),
            approval_callback=approval_callback,
        )

        async def fake_ensure_client():
            session._client = fake
            return fake

        monkeypatch.setattr(session, "ensure_client", fake_ensure_client)
        return session, fake

    return _factory


# ---------------------------------------------------------------------------
# MCP server protocol
# ---------------------------------------------------------------------------


class _StubTool:
    def __init__(self, name: str, result: str = "ok") -> None:
        self._name = name
        self._result = result
        self.calls: list[dict] = []

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return f"{self._name} description"

    @property
    def parameters(self) -> dict:
        return {"type": "object", "properties": {"q": {"type": "string"}}}

    async def execute(self, **kwargs) -> str:
        self.calls.append(kwargs)
        return self._result


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class TestMCPServerProtocol:
    def test_initialize_echoes_client_protocol_version(self):
        srv = _StdioMCPServer({})
        reply = _run(srv._handle({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18"},
        }))
        assert reply["id"] == 1
        assert reply["result"]["protocolVersion"] == "2025-06-18"
        assert reply["result"]["serverInfo"]["name"] == "flowly-tools"
        assert "tools" in reply["result"]["capabilities"]

    def test_initialize_falls_back_when_no_version(self):
        srv = _StdioMCPServer({})
        reply = _run(srv._handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}))
        assert reply["result"]["protocolVersion"]  # non-empty fallback

    def test_notification_gets_no_reply(self):
        srv = _StdioMCPServer({})
        reply = _run(srv._handle({"jsonrpc": "2.0", "method": "notifications/initialized"}))
        assert reply is None

    def test_tools_list_returns_schemas(self):
        srv = _StdioMCPServer({"web_search": _StubTool("web_search")})
        reply = _run(srv._handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}))
        tools = reply["result"]["tools"]
        assert len(tools) == 1
        assert tools[0]["name"] == "web_search"
        assert tools[0]["inputSchema"]["type"] == "object"

    def test_tools_call_dispatches(self):
        tool = _StubTool("web_search", result="hits")
        srv = _StdioMCPServer({"web_search": tool})
        reply = _run(srv._handle({
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "web_search", "arguments": {"q": "x"}},
        }))
        assert tool.calls == [{"q": "x"}]
        assert reply["result"]["content"][0]["text"] == "hits"
        assert reply["result"]["isError"] is False

    def test_tools_call_unknown_tool_is_error(self):
        srv = _StdioMCPServer({})
        reply = _run(srv._handle({
            "jsonrpc": "2.0", "id": 4, "method": "tools/call",
            "params": {"name": "nope", "arguments": {}},
        }))
        assert reply["result"]["isError"] is True

    def test_tools_call_error_string_marks_iserror(self):
        srv = _StdioMCPServer({"t": _StubTool("t", result="Error: boom")})
        reply = _run(srv._handle({
            "jsonrpc": "2.0", "id": 5, "method": "tools/call",
            "params": {"name": "t", "arguments": {}},
        }))
        assert reply["result"]["isError"] is True

    def test_unknown_method_returns_jsonrpc_error(self):
        srv = _StdioMCPServer({})
        reply = _run(srv._handle({"jsonrpc": "2.0", "id": 6, "method": "frobnicate"}))
        assert reply["error"]["code"] == -32601

    def test_ping(self):
        srv = _StdioMCPServer({})
        reply = _run(srv._handle({"jsonrpc": "2.0", "id": 7, "method": "ping"}))
        assert reply["result"] == {}


# ---------------------------------------------------------------------------
# Config migration
# ---------------------------------------------------------------------------


class TestMigration:
    def test_render_block_has_markers_and_server(self):
        block = tm.render_managed_block(python_bin="/py", env={"A": "B"})
        assert tm._MARKER in block
        assert tm._END_MARKER in block
        assert "[mcp_servers.flowly-tools]" in block
        assert 'command = "/py"' in block
        assert 'flowly.codex.tools_mcp_server' in block

    def test_migrate_writes_config(self, tmp_path):
        target = tm.migrate_flowly_tools_to_codex(
            codex_home=str(tmp_path), python_bin="/usr/bin/python3",
        )
        assert target.exists()
        text = target.read_text()
        assert "[mcp_servers.flowly-tools]" in text
        assert text.count(tm._MARKER) == 1

    def test_migrate_pins_pythonpath_to_flowly_root(self, tmp_path):
        # The callback subprocess is spawned by codex with a foreign cwd
        # and no PYTHONPATH; the managed block must pin the running
        # flowly's package root so the import resolves deterministically
        # even when a worktree venv's editable-install points elsewhere.
        from pathlib import Path

        import flowly
        root = str(Path(flowly.__file__).resolve().parent.parent)
        target = tm.migrate_flowly_tools_to_codex(
            codex_home=str(tmp_path), python_bin="/usr/bin/python3",
        )
        text = target.read_text()
        assert "PYTHONPATH" in text
        assert root in text

    def test_migrate_is_idempotent(self, tmp_path):
        tm.migrate_flowly_tools_to_codex(codex_home=str(tmp_path), python_bin="/p")
        target = tm.migrate_flowly_tools_to_codex(codex_home=str(tmp_path), python_bin="/p")
        text = target.read_text()
        # Exactly one managed block after two runs.
        assert text.count(tm._MARKER) == 1
        assert text.count("[mcp_servers.flowly-tools]") == 1

    def test_migrate_preserves_user_content(self, tmp_path):
        cfg = tmp_path / "config.toml"
        cfg.write_text(
            'model = "gpt-5.5"\n\n'
            '[projects."/home/x"]\ntrust_level = "trusted"\n'
        )
        tm.migrate_flowly_tools_to_codex(codex_home=str(tmp_path), python_bin="/p")
        text = cfg.read_text()
        assert 'model = "gpt-5.5"' in text
        assert '[projects."/home/x"]' in text
        assert "[mcp_servers.flowly-tools]" in text
        # Managed block lands before the first table so root keys stay
        # root-scoped — i.e. mcp_servers header precedes the projects one.
        assert text.index("[mcp_servers.flowly-tools]") < text.index('[projects."/home/x"]')

    def test_migrate_inserts_root_key_before_first_table(self, tmp_path):
        # The root-scoped `model` key must remain above any table header.
        cfg = tmp_path / "config.toml"
        cfg.write_text('[a]\nx = 1\n')
        tm.migrate_flowly_tools_to_codex(codex_home=str(tmp_path), python_bin="/p")
        text = cfg.read_text()
        assert text.index(tm._MARKER) < text.index("[a]")


# ---------------------------------------------------------------------------
# Elicitation handling (session server-request dialect)
# ---------------------------------------------------------------------------


class TestElicitationHandling:
    @pytest.mark.asyncio
    async def test_flowly_tools_elicitation_auto_accepts(
        self, mini_session,
    ) -> None:
        session, fake = mini_session()
        fake.script_response("thread/start", {"threadId": "thr_a"})
        fake.script_server_request(
            42, "mcpServer/elicitation/request", {"serverName": "flowly-tools"},
        )
        fake.script_notification("turn/completed", {})

        await session.run_turn("call a flowly tool")
        assert fake.responses_sent
        req_id, result = fake.responses_sent[0]
        assert req_id == 42
        assert result["action"] == "accept"

    @pytest.mark.asyncio
    async def test_other_server_elicitation_declines(
        self, mini_session,
    ) -> None:
        session, fake = mini_session()
        fake.script_response("thread/start", {"threadId": "thr_a"})
        fake.script_server_request(
            43, "mcpServer/elicitation/request", {"serverName": "some-other-mcp"},
        )
        fake.script_notification("turn/completed", {})

        await session.run_turn("call some other mcp tool")
        assert fake.responses_sent
        req_id, result = fake.responses_sent[0]
        assert req_id == 43
        assert result["action"] == "decline"

    @pytest.mark.asyncio
    async def test_elicitation_bypasses_approval_callback_dialect(
        self, mini_session,
    ) -> None:
        # Even with an approval_callback wired (which speaks the
        # {"decision": ...} dialect), elicitation must use {"action": ...}.
        async def callback(req):
            return {"decision": "approved"}

        session, fake = mini_session(approval_callback=callback)
        fake.script_response("thread/start", {"threadId": "thr_a"})
        fake.script_server_request(
            44, "mcpServer/elicitation/request", {"serverName": "flowly-tools"},
        )
        fake.script_notification("turn/completed", {})

        await session.run_turn("x")
        _req_id, result = fake.responses_sent[0]
        assert "action" in result and "decision" not in result
