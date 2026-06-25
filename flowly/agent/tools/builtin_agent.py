"""The `builtin_agent` tool — dispatch to the Assistant registry.

Tool name and `agent` parameter match the pre-P2.8 schema so existing
LLM trajectories keep working unchanged. Behind the scenes the enum
and description are derived from ``AssistantRegistry`` at each
schema-serialisation, so dropping a new ``~/.flowly/assistants/{name}.md``
file (or hitting the ``assistants.reload`` gateway RPC) exposes a new
specialist immediately without a code change.
"""

from __future__ import annotations

import json
from typing import Any, TYPE_CHECKING

from flowly.agent.tools.base import Tool

if TYPE_CHECKING:
    from flowly.agent.assistants import AssistantRegistry
    from flowly.agent.subagent import SubagentManager


class BuiltinAgentTool(Tool):
    """Delegate a task to one registered assistant.

    Sync mode (``wait=True``) is the default. The parent needs the
    child's output in-band as a tool_result so it can either deliver
    it to the user or incorporate it into its own next action.
    Previously this defaulted to async (``wait=False``) outside cron
    context, which produced a subtle failure mode: the parent received
    ``{status: accepted, run_id: ...}`` as the tool_result, didn't see
    the actual research report, assumed the child wasn't done, and
    started *its own* duplicate web search for the same topic.
    (Observed in production logs: `builtin_agent(researcher)` on
    "Elon Musk" → subagent ran 16 web_searches → parent then ran its
    own 2× web_search + 2× web_fetch for the same query.)

    Cron context still forces sync — that was already the case, left
    alone. Callers that genuinely want fire-and-forget should use the
    lower-level ``spawn`` tool, not ``builtin_agent``.
    """

    def __init__(
        self,
        manager: "SubagentManager",
        registry: "AssistantRegistry",
    ):
        self._manager = manager
        self._registry = registry
        self._origin_channel = "cli"
        self._origin_chat_id = "direct"

    def set_context(self, channel: str, chat_id: str) -> None:
        self._origin_channel = channel
        self._origin_chat_id = chat_id

    @property
    def name(self) -> str:
        return "builtin_agent"

    @property
    def description(self) -> str:
        entries = self._registry.all()
        if not entries:
            return "Delegate a task to a specialist. (No assistants registered.)"
        lines = [f"- {a.name}: {a.description}" for a in entries]
        return (
            "Delegate a task to ONE specialist (pick the best match, don't chain):\n"
            + "\n".join(lines)
            + "\n\nCRITICAL: Do NOT call researcher then writer — researcher "
            "ALREADY writes the report. Calling writer afterwards re-researches "
            "from scratch, wastes time, and produces a duplicate artifact.\n\n"
            "Duplicate-dispatch guard: if this specialist already ran "
            "successfully in this session in the last ~10 minutes, this tool "
            "will refuse and return a `recent_dispatch_exists` envelope "
            "pointing at the existing artifact. Read or promote that artifact "
            "instead of re-dispatching. If the new request truly is a "
            "different topic, prepend 'FRESH: ' to your task to bypass the "
            "guard."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        names = self._registry.names()
        # Schema validation needs a non-empty enum, but the registry may
        # legitimately be empty on fresh installs — fall back to an
        # unconstrained string so execute() can return a clear error.
        agent_schema: dict[str, Any] = {
            "type": "string",
            "description": "Which specialist agent to use",
        }
        if names:
            agent_schema["enum"] = names
        return {
            "type": "object",
            "properties": {
                "agent": agent_schema,
                "task": {
                    "type": "string",
                    "description": "The full task description — pass as-is",
                },
                "context": {
                    "type": "string",
                    "description": "Optional additional context",
                },
            },
            "required": ["agent", "task"],
        }

    async def execute(
        self,
        agent: str,
        task: str,
        context: str | None = None,
        **kwargs: Any,
    ) -> str:
        asst = self._registry.get(agent)
        if asst is None:
            return json.dumps({
                "status": "error",
                "error": f"Unknown agent '{agent}'. Available: {self._registry.names()}",
            })

        # Per-specialist model override (config: agents.assistant_models).
        #   <model id>  → run this specialist on that model
        #   "inherit"   → run it on the bot's selected model (self.model)
        #   absent      → the assistant's own default model
        eff_model = asst.model
        try:
            from flowly.config.loader import load_config
            _ov = (load_config().agents.assistant_models or {}).get(asst.name, "")
            if _ov == "inherit":
                eff_model = ""  # spawn() resolves "" → the manager's self.model
            elif _ov:
                eff_model = _ov
        except Exception:
            pass

        # Duplicate-dispatch guard. When the same specialist finished
        # successfully in this session within the last ~10 minutes, the
        # parent LLM almost certainly means to reference that result
        # (user said "kaydet / göster / more detail") rather than run
        # it again. Refuse the second dispatch and hand back the prior
        # artifact_id so the parent can artifact(get) or
        # artifact(promote) instead.
        #
        # Four escape valves, in order of importance:
        #   1. Task starting with "FRESH:" — LLM's deliberate bypass
        #      when it really is a new topic. The prefix is stripped
        #      below so it doesn't leak into the specialist's prompt.
        #   2. Cron context — scheduled jobs legitimately repeat. A
        #      cron tick that re-runs the same researcher every hour
        #      is not duplicate dispatch, it's the whole point.
        #   3. Only "ok" completions are recorded in the tracker —
        #      failed / interrupted runs never trigger the guard so
        #      retries always work.
        #   4. 10-minute freshness window — older artifacts don't
        #      block new dispatches (time moves on).
        parent_session_key = f"{self._origin_channel}:{self._origin_chat_id}"
        label = f"builtin:{asst.name}"
        strip_fresh = task.lstrip().startswith("FRESH:")
        if strip_fresh:
            task = task.lstrip().removeprefix("FRESH:").lstrip()
        in_cron = False
        try:
            from flowly.cron.context import in_cron_context
            in_cron = in_cron_context()
        except Exception:
            pass
        if not strip_fresh and not in_cron:
            recent = self._manager.last_completed_dispatch(
                parent_session_key=parent_session_key,
                label=label,
                max_age_seconds=600.0,
            )
            if recent:
                import time as _time
                mins_ago = int((_time.time() - recent["completed_at"]) / 60)
                mins_label = (
                    f"{mins_ago} minute(s) ago"
                    if mins_ago > 0 else "just now"
                )
                return json.dumps({
                    "status": "recent_dispatch_exists",
                    "artifact_id": recent["artifact_id"],
                    "label": recent["label"],
                    "title": recent["title"],
                    "completed_ago": mins_label,
                    "message": (
                        f"The '{asst.name}' specialist ran {mins_label} in "
                        f"this session and produced artifact "
                        f"'{recent['artifact_id']}' titled "
                        f"'{recent['title'][:80]}'. That full result is "
                        "already saved to the artifact store. "
                        f"Use artifact(action='get', artifact_id='{recent['artifact_id']}', "
                        "offset=0, limit=6000) to read sections of it, or "
                        f"artifact(action='promote', artifact_id='{recent['artifact_id']}') "
                        "if the user asks you to save/keep it (note: "
                        "subagent artifacts are already user-visible, so "
                        "'promote' is only needed for edge cases). "
                        "If this request really is a DIFFERENT topic that "
                        "needs fresh research from scratch, re-call "
                        f"builtin_agent with task starting 'FRESH: ' to "
                        "bypass this guard."
                    ),
                })

        full_task = f"{task}\n\n---\nAdditional context:\n{context}" if context else task

        # Per-assistant sync/async policy. Short specialists (coder,
        # reviewer, unconfigured user assistants) block the parent for
        # a sub-second turn — sync is correct, parent gets the real
        # output inline. Long specialists (researcher, writer — set
        # via ``async_dispatch: true``) would block the parent for
        # several minutes; dispatch them asynchronously instead: spawn
        # returns a "dispatched" envelope immediately, the parent acks
        # the user and ends its turn, and the specialist's result is
        # delivered as a system message that wakes the parent later.
        #
        # Cron always runs sync — a cron tick has no user to ack to
        # and cannot rely on announce-queue wake-ups landing inside
        # the same tick.
        wait_mode = not bool(getattr(asst, "async_dispatch", False))
        try:
            from flowly.cron.context import in_cron_context
            if in_cron_context():
                wait_mode = True
        except Exception:
            pass

        result = await self._manager.spawn(
            task=full_task,
            label=f"builtin:{asst.name}",
            origin_channel=self._origin_channel,
            origin_chat_id=self._origin_chat_id,
            model=eff_model,
            cleanup="keep",
            # timeout_seconds omitted — SubagentManager uses the
            # system-wide `_SUBAGENT_WALL_CLOCK_TIMEOUT` fallback.
            wait=wait_mode,
            assistant=asst,
        )

        # Reinforce the researcher doctrine in the sync tool result —
        # prevents the parent from chaining a writer call or starting
        # its own duplicate research after the specialist finished.
        # Only applies to sync mode: in async mode the dispatch envelope
        # is a JSON object and already carries `required_next_steps`;
        # appending a free-text note would break JSON.parse on the LLM
        # side and undo the async instructions.
        if (
            wait_mode
            and asst.name == "researcher"
            and isinstance(result, str)
            and result.strip()
        ):
            result = (
                result.rstrip()
                + "\n\n"
                + "[researcher: done. This IS the final research output — "
                "deliver it to the user as-is. Do NOT call `writer` to "
                "rewrite it, and do NOT run your own web_search / "
                "web_fetch on the same topic.]"
            )

        return result
