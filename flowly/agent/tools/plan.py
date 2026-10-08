"""plan tool — the agent's surface for general plan mode.

The agent calls ``plan(action="propose", ...)`` to decompose a task into
steps. In YOLO, an ordinary agent-authored tracking plan starts immediately;
an explicitly requested review (including standing/one-shot plan mode) blocks
until the user decides. Once executable, the agent reports finished steps
with ``update`` (batched, sent alongside its next real tool call); the manager
starts the next step and completes the plan itself, so tracking costs no extra
model round trips.

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


_STEP_STATUSES = ("completed", "skipped", "blocked", "in_progress", "pending")


def _progress(plan: Any) -> dict[str, Any]:
    """Terse tool result: what the model needs to continue, nothing more."""
    done = sum(1 for s in plan.steps if s.status in ("completed", "skipped"))
    out: dict[str, Any] = {"ok": True, "status": plan.status,
                           "progress": f"{done}/{len(plan.steps)}"}
    current = next((s for s in plan.steps if s.status == "in_progress"), None)
    if current is not None:
        out["current"] = {"id": current.id, "content": current.content}
    return out


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
            "Track a multi-step task as a live checklist shown above the user's "
            "input on every device (not in the chat).\n\n"
            "WHEN: only for work with 3+ distinct steps or that takes several "
            "minutes, or when the user asks for a plan / runs /plan. Never for a "
            "question, a single action, or anything you can finish in one or two "
            "tool calls (one flowlet, one reminder, one small edit) — just do it.\n\n"
            "COST: a plan call sent on its own costs a full model round trip. Send "
            "plan updates in the SAME response as the real tool call you make next, "
            "never as a standalone step. The server keeps the books: a started plan "
            "puts step 1 in progress, finishing a step starts the next pending one, "
            "and the plan completes itself once every step is completed or skipped. "
            "So report only finished steps — several at once is fine — and if the "
            "work took fewer moves than planned, mark them all completed in one update.\n\n"
            "ACTIONS:\n"
            "- propose(goal, steps[, title, detailsMd, requiresApproval]): steps = "
            "[{id:1..N, content:'imperative, user-visible outcome', activeForm?}], "
            "3-7 steps, not micro-actions. In YOLO an ordinary plan starts at once; "
            "otherwise the call waits for the user. approved → do the work; revise → "
            "propose again with the feedback (same plan); rejected or not_approved → "
            "do not do the task. Pass requiresApproval=true when the user wants to "
            "review first or wants a plan without execution. Explicit /plan or "
            "standing Plan mode is always review-gated.\n"
            "- update(steps=[{id, status, note?}]): status = completed | skipped | "
            "blocked | in_progress | pending.\n"
            "- complete([summary]): optional — only to attach a summary or to close "
            "the plan early.\n"
            "- block(summary): the plan can't proceed without the user.\n"
            "- abort(): discard the plan.\n"
            "- view(): the current plan (rarely needed; results already carry progress).\n\n"
            "While a plan awaits review, side-effecting tools (commands, file writes, "
            "messages, external services) stay blocked; reading and searching still work."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["propose", "update", "complete", "block", "abort", "view"],
                },
                "goal": {
                    "type": "string",
                    "description": "The task in one sentence (propose).",
                },
                "title": {
                    "type": "string",
                    "description": "Short plan title shown on the card (propose).",
                },
                "detailsMd": {
                    "type": "string",
                    "description": "Optional Markdown body for the plan card (propose).",
                },
                "requiresApproval": {
                    "type": "boolean",
                    "description": (
                        "propose: true when the user explicitly wants to "
                        "review/approve the plan, or wants a plan without execution."
                    ),
                },
                "steps": {
                    "type": "array",
                    "description": (
                        "propose: [{id, content, activeForm?, note?}]. "
                        "update: [{id, status, note?}] — any number of steps at once."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "integer"},
                            "content": {"type": "string"},
                            "activeForm": {"type": "string"},
                            "status": {"type": "string", "enum": list(_STEP_STATUSES)},
                            "note": {"type": "string"},
                        },
                    },
                },
                "summary": {
                    "type": "string",
                    "description": "Completion summary (complete) or reason (block).",
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

    def _runs_unattended(self) -> bool:
        """Read the live YOLO stance from the registered exec tool.

        The exec tool owns the canonical, hot-reloaded policy store. Missing or
        failing policy lookup must fail closed to the normal review path.
        """
        try:
            exec_tool = (
                self._registry.get("exec") if self._registry is not None else None
            )
            check = getattr(exec_tool, "runs_unattended", None)
            return bool(check()) if callable(check) else False
        except Exception as exc:
            logger.debug(f"[plan] unattended policy lookup failed; requiring review: {exc}")
            return False

    # ── execute ─────────────────────────────────────────────────────────

    async def execute(self, action: str = "", **kwargs: Any) -> str:
        if not plan_tool_enabled():
            return json.dumps({"error": "plan tool disabled (FLOWLY_PLAN_ENABLED=0)."})

        valid = {"propose", "view", "update", "update_step", "complete", "block", "abort"}
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
            if action == "update":
                return await self._update(session_key, kwargs.get("steps"))
            if action == "update_step":
                # Pre-batch spelling; still accepted so resumed sessions and
                # older prompts keep working.
                return await self._update(
                    session_key,
                    [{"id": kwargs.get("id"), "status": kwargs.get("status"),
                      "note": kwargs.get("note")}],
                )
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

        requested_review = kwargs.get("requiresApproval")
        requires_review = (
            requested_review not in (None, False)
            or self._manager.is_forced_pending(session_key)
            or self._manager.is_sticky(session_key)
        )
        mode = "auto" if not requires_review and self._runs_unattended() else "forced"

        plan, decision = await self._manager.propose(
            session_key,
            goal,
            steps,
            title=str(kwargs.get("title", "")).strip(),
            details_md=(
                str(kwargs["detailsMd"]) if kwargs.get("detailsMd") else None
            ),
            run_id=self._current_run_id(),
            mode=mode,
        )

        # Once a plan is approved/executing, the forced-mode gate lifts.
        if decision.approved:
            self._manager.disarm_forced(session_key)

        base = {
            "planId": plan.id,
            "revision": plan.revision,
            "status": plan.status,
            "via": decision.via,
        }
        if decision.decision == "approve":
            started = (
                "Plan auto-started under YOLO."
                if decision.via == "policy"
                else "Plan approved."
            )
            return json.dumps(
                {
                    **base,
                    "decision": "approved",
                    "note": (
                        started
                        + " Step 1 is in progress. Do the work now; report finished "
                        "steps with update in the same response as your next real "
                        "tool call. The plan completes itself when every step is done."
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
            if not self._manager.is_sticky(session_key):
                self._manager.disarm_forced(session_key)
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
        if not self._manager.is_sticky(session_key):
            self._manager.disarm_forced(session_key)
        return json.dumps(
            {
                **base,
                "decision": "not_approved",
                "via": decision.via,
                "note": (
                    "No approval received (timed out or no approver). The task "
                    "was not executed."
                ),
            }
        )

    def _view(self, session_key: str) -> str:
        plan = self._manager.get_current(session_key)
        if not plan:
            return json.dumps({"plan": None, "note": "No active plan for this session."})
        return json.dumps({"plan": plan.public_view()})

    async def _update(self, session_key: str, raw: Any) -> str:
        plan = self._manager.get_current(session_key)
        if not plan:
            return json.dumps({"error": "update: no active plan. Call propose first."})
        if not isinstance(raw, list) or not raw:
            return json.dumps({"error": "update: steps must be a non-empty array of {id, status}."})
        updates: list[dict[str, Any]] = []
        for item in raw:
            if not isinstance(item, dict) or not isinstance(item.get("id"), int):
                return json.dumps({"error": "update: every step needs an integer id."})
            status = item.get("status") or "completed"
            if status not in _STEP_STATUSES:
                return json.dumps({"error": f"update: invalid status {status!r}."})
            if not plan.get_step(item["id"]):
                return json.dumps({
                    "error": f"update: no step {item['id']}. Valid: {[s.id for s in plan.steps]}"
                })
            updates.append({"id": item["id"], "status": status, "note": item.get("note")})
        updated = await self._manager.update_steps(plan.id, updates)
        if not updated:
            return json.dumps({"error": "update: failed."})
        return json.dumps(_progress(updated))

    async def _complete(self, session_key: str, kwargs: dict[str, Any]) -> str:
        plan = self._manager.get_current(session_key)
        if not plan:
            # The plan may already have completed itself on its last step.
            latest = next(iter(self._manager.list_for_session(session_key)), None)
            if latest is not None and latest.status == "completed":
                if kwargs.get("summary"):
                    await self._manager.complete(latest.id, str(kwargs["summary"]))
                return json.dumps({"ok": True, "status": "completed"})
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
