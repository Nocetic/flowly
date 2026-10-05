"""Read-only product investigation: fake providers, temporary stores, no live API.

Run from the Flowly root with PYTHONDONTWRITEBYTECODE=1 .venv/bin/python <this file>.
All runtime data is created under TemporaryDirectory and removed on exit.
"""
import asyncio
import ast
import json
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))


async def investigate(root):
    os.environ["FLOWLY_HOME"] = str(root / "isolated-home")
    from loguru import logger
    logger.remove()
    from flowly.agent.assistants import Assistant
    from flowly.agent.loop import _detect_builtin_agent_type
    from flowly.agent.subagent import SubagentManager
    from flowly.agent.subagent_registry import SubagentRegistry
    from flowly.agent.tools.artifact import ArtifactTool
    from flowly.agent.tools.shared_service import SharedArtifactTool
    from flowly.artifacts.store import get_store
    from flowly.bus.queue import MessageBus
    from flowly.providers.base import LLMProvider, LLMResponse, ToolCallRequest

    class FakeProvider(LLMProvider):
        def __init__(self, output, action=None):
            super().__init__(api_key="unused")
            self.output, self.action, self.calls = output, action, 0

        def get_default_model(self):
            return "fake/model"

        async def chat(self, **kwargs):
            self.calls += 1
            if self.action and self.calls == 1:
                return LLMResponse(content=None, tool_calls=[ToolCallRequest(
                    id="fake-call", name="artifact", arguments=self.action,
                )])
            return LLMResponse(content=self.output)

    evidence = {}
    # Execute the actual current interception AST, not a reimplementation.
    source = (REPO / "flowly/agent/loop.py").read_text()
    tree = ast.parse(source)
    node = next(n for n in ast.walk(tree) if isinstance(n, ast.If)
                and ast.unparse(n.test) == "_effective_tool_name == 'spawn' and (not _builtin_agent_dispatched)")
    ns = dict(_effective_tool_name="spawn", _builtin_agent_dispatched=False,
              _detect_builtin_agent_type=_detect_builtin_agent_type, logger=logger,
              call_args=dict(task="Research the Composer issue", model="gpt-6-luna",
                             label="Composer", timeout_seconds=700))
    exec(compile(ast.Module(body=[node], type_ignores=[]), "actual-spawn-interception", "exec"), ns)
    evidence["spawn_interception"] = dict(tool=ns["_effective_tool_name"], arguments=ns["call_args"])

    async def scenario(name, size, action=None, auto=False, cap=False):
        path = root / name
        path.mkdir()
        mgr = SubagentManager(provider=FakeProvider("x" * size, action), workspace=path,
                              bus=MessageBus(), state_dir=path,
                              registry=SubagentRegistry(path / "runs.json"))
        assistant = Assistant(name="probe", description="probe", model="fake/model",
                              system_prompt="Return test output.", auto_save_artifact=auto,
                              cap_to_artifact=cap)
        await mgr.spawn("Investigate Composer", origin_channel="web", origin_chat_id="chat-1",
                        assistant=assistant)
        await asyncio.gather(*list(mgr._running_tasks.values()))
        record = mgr.registry.all()[0]
        notice = await mgr.bus.consume_inbound()
        await mgr._events.flush()
        local = get_store(path)
        evidence[name] = dict(
            outcome=record.outcome, artifact_count=len(local.list()),
            artifact_ids_count=len(record.artifact_ids),
            saved_result_chars=mgr.registry.read_result(record.run_id)["totalChars"],
            notice_has_run_id=record.run_id in notice.content,
            notice_has_artifact_id=any(a["id"] in notice.content for a in local.list()),
        )
        return local

    await scenario("plain_3000", 3000)
    await scenario("list_then_autosave_3000", 3000, action={"action": "list"}, auto=True)
    await scenario("explicit_create_3000", 3000, action={
        "action": "create", "type": "markdown", "title": "Composer report", "content": "test result",
    })
    profile_store = await scenario("profile_cap_7000", 7000, cap=True)
    primary_store = get_store(root / "primary")

    class FakePrimaryGateway:
        async def send_shared_service_request(self, **kwargs):
            output = await ArtifactTool(primary_store).execute(**kwargs["arguments"])
            return {"ok": True, "output": output}

    shared = SharedArtifactTool(FakePrimaryGateway(), profile_store)
    artifact_id = profile_store.list()[0]["id"]
    listed = json.loads(await shared.execute(action="list"))
    fetched = json.loads(await shared.execute(action="get", artifact_id=artifact_id))
    evidence["profile_library_visibility"] = dict(
        primary_count=len(primary_store.list()), profile_count=len(profile_store.list()),
        shared_list_count=len(listed["artifacts"]),
        get_by_known_local_id_works=fetched["artifact"]["id"] == artifact_id,
    )
    return evidence


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="flowly-subagent-study-") as name:
        print(json.dumps(asyncio.run(investigate(Path(name))), indent=2, ensure_ascii=False))
