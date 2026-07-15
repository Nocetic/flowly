"""plan tool — the agent's surface for general plan mode.

The agent calls ``plan(action="propose", ...)`` to decompose a task into
steps and push it for the user's approval. That call BLOCKS until the user
approves / rejects / requests revisions (or it times out = not approved) —
same await-a-Future shape as ``clarify``. Once approved, the agent ticks
steps with ``update_step`` as it works and finishes with ``complete``.

Distinct from ``browser_plan`` (browser-coupled, evidence + validator). This
is the general, session-level plan that syncs to every client's composer.

Disable entirely with ``FLOWLY_PLAN_ENABLED=0`` (emergency kill switch).
"""

from __future__ import annotations

import json
import os
from typing import Any, Optional

from loguru import logger

from flowly.agent.tools.base import Tool
from flowly.plans.manager import PlanManager, get_plan_manager


def plan_tool_enabled() -> bool:
    val = os.environ.get("FLOWLY_PLAN_ENABLED", "1").strip().lower()
    return val not in {"0", "false", "no", "off"}


class PlanTool(Tool):
    def __init__(
        self,
        manager: Optional[PlanManager] = None,
        registry: Any = None,
        default_session_key: str = "default",
    ):
        self._manager = manager or get_plan_manager()
        self._registry = registry
        self._default_session_key = default_session_key

    # ── identity ────────────────────────────────────────────────────────

    @property
    def name(self) -> str:
        return "plan"

    @property
    def description(self) -> str:
        return (
            "Plan mode — for any task that is long or has several distinct "
            "steps, propose a plan and get the user's approval BEFORE doing "
            "the work. The plan appears above the user's input on every device "
            "with live ticks as you complete each step.\n\n"
            "ACTIONS:\n"
            "- propose(goal, steps[, title, detailsMd]): decompose the task and "
            "ask for approval. This BLOCKS until the user decides. steps = array "
            "of {id:int(1..N), content:str(imperative, e.g. 'Add the RPC "
            "handler'), activeForm?:str(gerund), note?:str}. title = short plan "
            "title. detailsMd = optional Markdown body shown in the plan card. "
            "The result tells you what the user chose:\n"
            "  • approved → execute the steps now, calling update_step as you go.\n"
            "  • revise  → the user's feedback is included; call propose AGAIN "
            "with updated steps (it continues the same plan).\n"
            "  • rejected → do NOT do the task; acknowledge and stop.\n"
            "  • timeout  → not approved; do not execute — tell the user you're "
            "waiting for approval.\n"
            "- view(): return the current plan.\n"
            "- update_step(id, status[, note]): status = pending|in_progress|"
            "completed|blocked|skipped. Mark a step in_progress before starting "
            "it and completed right after — this drives the live ticks.\n"
            "- complete([summary]): declare the whole plan done.\n"
            "- block([reason]): the plan can't proceed and needs the user.\n"
            "- abort(): discard the current plan.\n\n"
            "Until a plan is approved, side-effecting tools (running commands, "
            "writing files, sending messages, external services) are blocked — "
            "so propose first, then act."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
                        "propose",
                        "view",
                        "update_step",
                        "complete",
                        "block",
                        "abort",
                    ],
                },
                "goal": {
                    "type": "string",
                    "description": "The task in one sentence (for propose).",
                },
                "title": {
                    "type": "string",
                    "description": "Short plan title shown on the card (for propose).",
                },
                "detailsMd": {
                    "type": "string",
                    "description": "Optional Markdown body for the plan card (for propose).",
                },
                "steps": {
                    "type": "array",
                    "description": "Array of {id, content, activeForm?, note?} (for propose).",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "integer"},
                            "content": {"type": "string"},
                            "activeForm": {"type": "string"},
                            "note": {"type": "string"},
                        },
                        "required": ["content"],
                    },
                },
                "id": {"type": "integer", "description": "Step id (for update_step)."},
                "status": {
                    "type": "string",
                    "enum": [
                        "pending",
                        "in_progress",
                        "completed",
                        "blocked",
                        "skipped",
                    ],
                    "description": "New step status (for update_step).",
                },
                "note": {
                    "type": "string",
                    "description": "Free-form annotation on a step (for update_step).",
                },
                "summary": {
                    "type": "string",
                    "description": "Completion summary (for complete) or reason (for block).",
                },
                "session_key": {
                    "type": "string",
                    "description": "Optional — defaults to the agent's current session.",
                },
            },
            "required": ["action"],
        }

    # ── session resolution ──────────────────────────────────────────────

    def _resolve_session_key(self, kwargs: dict[str, Any]) -> str:
        explicit = kwargs.get("session_key")
        if explicit:
            return str(explicit)
        if self._registry is not None:
            sess = getattr(self._registry, "_active_session_id", "")
            if sess:
                return str(sess)
        return self._default_session_key

    def _current_run_id(self) -> Optional[str]:
        if self._registry is not None:
            rid = getattr(self._registry, "_active_run_id", "")
            if rid:
                return str(rid)
        return None

    # ── execute ─────────────────────────────────────────────────────────

    async def execute(self, action: str = "", **kwargs: Any) -> str:
        if not plan_tool_enabled():
            return json.dumps({"error": "plan tool disabled (FLOWLY_PLAN_ENABLED=0)."})

        valid = {"propose", "view", "update_step", "complete", "block", "abort"}
        if action not in valid:
            return json.dumps(
                {"error": f"Unknown action: {action!r}. Valid: {sorted(valid)}"}
            )

        session_key = self._resolve_session_key(kwargs)
        try:
            if action == "propose":
                return await self._propose(session_key, kwargs)
            if action == "view":
                return self._view(session_key)
            if action == "update_step":
                return await self._update_step(session_key, kwargs)
            if action == "complete":
                return await self._complete(session_key, kwargs)
            if action == "block":
                return await self._block(session_key, kwargs)
            if action == "abort":
                return await self._abort(session_key)
            return json.dumps({"error": f"Unhandled action: {action}"})
        except Exception as e:
            logger.exception(f"[plan] {action} failed")
            return json.dumps({"error": f"plan {action} failed: {e}"})

    # ── action handlers ─────────────────────────────────────────────────

    async def _propose(self, session_key: str, kwargs: dict[str, Any]) -> str:
        goal = str(kwargs.get("goal", "")).strip()
        if not goal:
            return json.dumps({"error": "propose: goal is required."})
        raw_steps = kwargs.get("steps") or []
        if not isinstance(raw_steps, list) or not raw_steps:
            return json.dumps({"error": "propose: steps must be a non-empty array."})
        steps = self._manager.build_steps(raw_steps)
        if not steps:
            return json.dumps(
                {"error": "propose: no valid steps (each needs a non-empty content)."}
            )

        plan, decision = await self._manager.propose(
            session_key,
            goal,
            steps,
            title=str(kwargs.get("title", "")).strip(),
            details_md=(
                str(kwargs["detailsMd"]) if kwargs.get("detailsMd") else None
            ),
            run_id=self._current_run_id(),
        )

        # Once a plan is approved/executing, the forced-mode gate lifts.
        if decision.approved:
            self._manager.disarm_forced(session_key)

        base = {"planId": plan.id, "revision": plan.revision, "status": plan.status}
        if decision.decision == "approve":
            return json.dumps(
                {
                    **base,
                    "decision": "approved",
                    "note": (
                        "Plan approved. Execute the steps now: mark each "
                        "in_progress before starting and completed right after, "
                        "then call complete() at the end."
                    ),
                    "plan": plan.public_view(),
                }
            )
        if decision.decision == "revise":
            return json.dumps(
                {
                    **base,
                    "decision": "revise",
                    "feedback": decision.feedback or "",
                    "note": (
                        "The user wants changes. Call propose again with updated "
                        "steps — it continues THIS plan (same id)."
                    ),
                }
            )
        if decision.decision == "reject":
            return json.dumps(
                {
                    **base,
                    "decision": "rejected",
                    "note": (
                        "The user rejected the plan. Do NOT do the task. "
                        "Acknowledge briefly and stop."
                    ),
                }
            )
        # timeout / cron
        return json.dumps(
            {
                **base,
                "decision": "not_approved",
                "via": decision.via,
                "note": (
                    "No approval received (timed out or no approver). Do not "
                    "execute the task. Tell the user the plan is waiting for "
                    "their approval."
                ),
            }
        )

    def _view(self, session_key: str) -> str:
        plan = self._manager.get_current(session_key)
        if not plan:
            return json.dumps({"plan": None, "note": "No active plan for this session."})
        return json.dumps({"plan": plan.public_view()})

    async def _update_step(self, session_key: str, kwargs: dict[str, Any]) -> str:
        plan = self._manager.get_current(session_key)
        if not plan:
            return json.dumps({"error": "update_step: no active plan. Call propose first."})
        sid = kwargs.get("id")
        if not isinstance(sid, int):
            return json.dumps({"error": "update_step: id (int) required."})
        status = kwargs.get("status") or "in_progress"
        valid = {"pending", "in_progress", "completed", "blocked", "skipped"}
        if status not in valid:
            return json.dumps({"error": f"update_step: invalid status {status!r}."})
        if not plan.get_step(sid):
            return json.dumps(
                {
                    "error": f"update_step: no step {sid}. Valid: "
                    f"{[s.id for s in plan.steps]}"
                }
            )
        updated = await self._manager.update_step(
            plan.id, sid, status, note=kwargs.get("note")
        )
        if not updated:
            return json.dumps({"error": "update_step: failed."})
        return json.dumps(
            {"success": True, "progress": updated.progress_summary(), "status": updated.status}
        )

    async def _complete(self, session_key: str, kwargs: dict[str, Any]) -> str:
        plan = self._manager.get_current(session_key)
        if not plan:
            return json.dumps({"error": "complete: no active plan."})
        updated = await self._manager.complete(plan.id, str(kwargs.get("summary", "")))
        self._manager.disarm_forced(session_key)
        return json.dumps({"success": True, "plan": updated.public_view() if updated else None})

    async def _block(self, session_key: str, kwargs: dict[str, Any]) -> str:
        plan = self._manager.get_current(session_key)
        if not plan:
            return json.dumps({"error": "block: no active plan."})
        updated = await self._manager.mark_blocked(plan.id, str(kwargs.get("summary", "")))
        return json.dumps({"success": True, "plan": updated.public_view() if updated else None})

    async def _abort(self, session_key: str) -> str:
        plan = self._manager.get_current(session_key)
        if not plan:
            return json.dumps({"error": "abort: no active plan."})
        updated = await self._manager.cancel(plan.id)
        self._manager.disarm_forced(session_key)
        return json.dumps({"success": True, "plan": updated.public_view() if updated else None})
