"""Real MCP transport consent, typed forms, ownership and cancellation."""

from __future__ import annotations

import asyncio
import json
import sys
import time
from types import SimpleNamespace

import pytest

from flowly.agent.tools.registry import ToolRegistry
from flowly.clarify.manager import ClarifyManager
from flowly.exec.approval_manager import ApprovalManager

SERVER = r'''
import asyncio
import json
from mcp import types
from mcp.server.lowlevel import Server

count = 0
schema = {"type": "object", "properties": {
    "color": {"type": "string", "enum": ["red", "blue"]},
    "count": {"type": "integer", "minimum": 1, "maximum": 4},
    "enabled": {"type": "boolean"},
    "note": {"type": "string"},
}, "required": ["color", "count", "enabled"]}

async def listing(ctx, params):
    return types.ListToolsResult(tools=[types.Tool(
        name=name, description=name,
        inputSchema={"type": "object", "properties": {
            "label": {"type": "string"}, "session_key": {"type": "string"},
        }}, annotations=types.ToolAnnotations(readOnlyHint=name == "read"),
    ) for name in ("read", "write", "form")])

async def call(ctx, params):
    global count
    args = params.arguments or {}
    label = args.get("label", "")
    if params.name == "write":
        count += 1
        data = {"writes": count}
    elif params.name == "read":
        data = {"writes": count}
    else:
        state = "opaque\nstate/" + label
        if ctx.protocol_version >= "2026-07-28":
            if not params.input_responses or label == "repeat":
                return types.InputRequiredResult(
                    input_requests={"question": types.ElicitRequest(params=types.ElicitRequestFormParams(
                        message="Choose options for " + label, requested_schema=schema,
                    ))}, request_state=state,
                )
            assert params.request_state == state
            answer = params.input_responses["question"]
        else:
            answer = await ctx.session.elicit_form("Choose options for " + label, schema, ctx.request_id)
        data = {"answer": answer.model_dump(), "label": label,
                "remote_session_key": args.get("session_key")}
    return types.CallToolResult(content=[types.TextContent(text=json.dumps(data))])

async def main():
    from mcp.server.stdio import stdio_server

    server = Server("input-test", on_list_tools=listing, on_call_tool=call)
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())

asyncio.run(main())
'''


@pytest.fixture
def surfaces(tmp_path, monkeypatch):
    from flowly.clarify import manager as clarify_module
    from flowly.exec import approval_manager as approval_module

    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path))
    approvals, clarify = ApprovalManager(), ClarifyManager()
    monkeypatch.setattr(approval_module, "_manager", approvals)
    monkeypatch.setattr(clarify_module, "_manager", clarify)
    yield approvals, clarify
    from flowly.mcp import shutdown_mcp_servers

    shutdown_mcp_servers()


def discover(*, mode="auto", trust="full", timeout=3):
    from flowly.mcp import discover_mcp_tools

    registry = ToolRegistry()
    names = discover_mcp_tools(servers={"forms": {
        "command": sys.executable, "args": ["-c", SERVER], "protocol": mode,
        "osv_check": False, "timeout": 8, "connect_timeout": 10,
        "supports_parallel_tool_calls": True, "max_parallel_tool_calls": 4,
        "trust": trust, "elicitation": {"enabled": True, "timeout": timeout},
    }}, tool_registry=registry)
    assert "mcp_forms_form" in names
    return registry


def data(value):
    return json.loads(json.loads(value)["result"])


@pytest.mark.parametrize("mode", ["auto", "legacy", "stateless"])
def test_real_forms_route_parallel_calls_to_their_own_surface(surfaces, mode):
    approvals, clarify = surfaces
    registry = discover(mode=mode)

    async def run():
        owner_loop = asyncio.get_running_loop()
        seen = []

        async def approve(pending):
            assert asyncio.get_running_loop() is owner_loop
            seen.append((pending.session_key, pending.request.command))
            assert pending.supports_always is False
            await asyncio.sleep(0.02)
            approvals.resolve(pending.id, "allow-once")

        async def answer(pending):
            assert asyncio.get_running_loop() is owner_loop
            assert pending.session_key in {"web:first", "web:second"}
            for field, value in {"color": "blue", "count": "2", "enabled": "true", "note": "/skip"}.items():
                if f"— {field}:" in pending.question:
                    clarify.resolve(pending.id, value)
                    return
            pytest.fail("Unexpected form field")

        approvals.add_notify_callback(approve)
        clarify.add_notify_callback(answer)
        results = await asyncio.gather(*[
            registry.execute("mcp_forms_form", {"label": label, "session_key": "web:forged"},
                             session_key=f"web:{label}")
            for label in ("first", "second")
        ])
        for label, result in zip(("first", "second"), results):
            result = data(result)
            assert result["answer"]["action"] == "accept"
            assert result["answer"]["content"] == {"color": "blue", "count": 2, "enabled": True}
            assert result["remote_session_key"] == "web:forged"
            assert (f"web:{label}", f"MCP server forms: Choose options for {label}") in seen
        assert approvals.list_pending() == clarify.list_pending() == []

    asyncio.run(run())


def test_untrusted_writes_need_real_owner_consent_but_readonly_does_not(surfaces):
    approvals, _clarify = surfaces
    registry = discover(trust="untrusted")

    async def run():
        # Neither a schema argument nor an absent user can manufacture ownership.
        denied = await registry.execute("mcp_forms_write", {"session_key": "web:forged"})
        assert "not approved" in denied
        assert data(await registry.execute("mcp_forms_read", {}))["writes"] == 0
        decisions = iter(["deny", "allow-once"])

        async def approve(pending):
            assert pending.session_key == "web:owner"
            approvals.resolve(pending.id, next(decisions))

        approvals.add_notify_callback(approve)
        denied = await registry.execute("mcp_forms_write", {}, session_key="web:owner")
        assert "not approved" in denied
        assert data(await registry.execute("mcp_forms_read", {}))["writes"] == 0
        allowed = await registry.execute("mcp_forms_write", {}, session_key="web:owner")
        assert data(allowed)["writes"] == 1

    asyncio.run(run())


@pytest.mark.parametrize("mode", ["auto", "legacy"])
def test_mcp_cancellation_retires_prompt_even_during_surface_notification(surfaces, mode):
    approvals, _clarify = surfaces
    registry = discover(mode=mode)

    async def run():
        entered, closed = asyncio.Event(), asyncio.Event()

        async def notify(_pending):
            entered.set()
            await asyncio.Event().wait()

        async def close(_id, reason, session_key):
            assert reason == "cancelled"
            assert session_key == "web:owner"
            closed.set()

        approvals.add_notify_callback(notify)
        approvals.add_close_callback(close)
        call = asyncio.create_task(registry.execute("mcp_forms_form", {}, session_key="web:owner"))
        await asyncio.wait_for(entered.wait(), 3)
        call.cancel()
        with pytest.raises(asyncio.CancelledError):
            await call
        await asyncio.wait_for(closed.wait(), 3)
        assert approvals.list_pending() == []
        assert data(await registry.execute("mcp_forms_read", {}))["writes"] == 0

    asyncio.run(run())


def test_mcp_form_timeout_is_fail_closed(surfaces):
    registry = discover(timeout=0.1)
    result = asyncio.run(registry.execute("mcp_forms_form", {}, session_key="web:owner"))
    assert data(result)["answer"]["action"] in {"cancel", "decline"}
    assert surfaces[0].list_pending() == []


def test_declined_form_returns_no_collected_data(surfaces):
    approvals, clarify = surfaces
    registry = discover()

    async def run():
        async def deny(pending):
            approvals.resolve(pending.id, "deny")

        async def unexpected(_pending):
            pytest.fail("A declined form must not collect user data")

        approvals.add_notify_callback(deny)
        clarify.add_notify_callback(unexpected)
        result = data(await registry.execute("mcp_forms_form", {}, session_key="web:owner"))
        assert result["answer"]["action"] == "decline"
        assert result["answer"]["content"] is None

    asyncio.run(run())


def test_invalid_form_field_is_reasked_then_declined(surfaces):
    approvals, clarify = surfaces
    registry = discover()

    async def run():
        questions = []

        async def approve(pending):
            approvals.resolve(pending.id, "allow-once")

        async def invalid(pending):
            questions.append(pending.question)
            clarify.resolve(pending.id, "not an enum value")

        approvals.add_notify_callback(approve)
        clarify.add_notify_callback(invalid)
        result = data(await registry.execute("mcp_forms_form", {}, session_key="web:owner"))
        assert result["answer"]["action"] == "decline"
        assert len(questions) == 2
        assert "Previous answer" in questions[-1]

    asyncio.run(run())


def test_cron_origin_survives_both_loop_hops_and_does_not_wait_for_user(surfaces):
    from flowly.cron.context import cron_context

    registry = discover()

    async def run():
        with cron_context():
            result = await registry.execute("mcp_forms_form", {}, session_key="web:cron")
        assert data(result)["answer"]["action"] == "decline"
        assert surfaces[0].list_pending() == []

    asyncio.run(run())


def test_input_required_continuation_rounds_are_bounded(surfaces):
    registry = discover()
    result = asyncio.run(registry.execute("mcp_forms_form", {"label": "repeat"}))
    assert "more than 8 rounds" in result


@pytest.mark.parametrize("schema", [
    {"type": "object", "properties": {"password": {"type": "string"}}},
    {"type": "object", "properties": {"nested": {"type": "object"}}},
    {"type": "object", "$ref": "https://example.org/schema"},
    {"type": "object", "properties": {str(i): {"type": "string"} for i in range(17)}},
])
def test_unsafe_or_unbounded_forms_are_declined(schema):
    from flowly.mcp.interaction import MCPInteraction

    assert MCPInteraction._form_schema(schema) is None


@pytest.mark.asyncio
async def test_url_and_ownerless_elicitation_decline_without_opening_browser():
    from flowly.mcp.interaction import MCPInteraction

    interaction = MCPInteraction("test", {})
    for params in [SimpleNamespace(mode="url", url="https://example.org"),
                   SimpleNamespace(mode="form", requested_schema={"type": "object", "properties": {}})]:
        assert (await interaction.elicit(None, params)).action == "decline"


@pytest.mark.asyncio
async def test_clarify_delivery_timeout_cleans_up_pending_question():
    from flowly.clarify.types import ClarifyRequest

    manager = ClarifyManager()
    closed = []

    async def notify(_pending):
        await asyncio.Event().wait()

    async def close(_id, reason, _session):
        closed.append(reason)

    manager.add_notify_callback(notify)
    manager.add_close_callback(close)
    now = time.time()
    assert await manager.request_and_wait(ClarifyRequest(
        id="slow", question="question", created_at=now, expires_at=now + 0.01,
    )) is None
    assert manager.list_pending() == []
    assert closed == ["timeout"]
