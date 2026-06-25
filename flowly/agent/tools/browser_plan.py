"""browser_plan tool — explicit planning for browser-driven tasks.

Phase 1 actions (this commit):
  - create:      decompose a goal into steps with success criteria
  - view:        return the current plan state
  - update_step: change a step's status; evidence required for done
  - complete:    declare the whole plan complete with final evidence

Phase 2 (next commit) will add:
  - validator integration (Haiku check on every done flip)
  - end-turn guard hook in loop.py
  - filesystem persistence
  - auto-injection in browser_tab tool results

Phase 3:
  - revise (with audit log)
  - mark_blocked
  - expand_step (sub-plans)

Design choice: plan tool is ALWAYS available when browser_tab is
enabled. No adaptive heuristic, no per-task gate. The user's
directive: "browserla çalışacağı zaman direkt plan expose
edilsin, küçük tasklar bile olsa". Skill teaches the agent to
always create a plan first; tool itself doesn't enforce.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from typing import Any, Optional

from loguru import logger

from flowly.agent.planner.models import Plan, Step, StepStatus
from flowly.agent.planner.state import get_plan_state
from flowly.agent.tools.base import Tool


# Feature flag — set FLOWLY_BROWSER_PLAN_ENABLED=0 to disable the
# tool entirely (emergency kill switch). Default ON since the user
# wants planning surfaced for every browser task.
def _is_enabled() -> bool:
    val = os.environ.get("FLOWLY_BROWSER_PLAN_ENABLED", "1").strip().lower()
    return val not in {"0", "false", "no", "off"}


class BrowserPlanTool(Tool):
    """Explicit planning + step tracking for multi-step browser tasks.

    Usage pattern (the agent learns this from skill + tool description):

        # 1. At the START of any browser task:
        browser_plan(action="create",
                     goal="...",
                     steps=[
                       {"id": 1, "content": "...", "successCriteria": "..."},
                       ...
                     ])

        # 2. Before each step's actions, mark in_progress:
        browser_plan(action="update_step", id=2, status="in_progress")

        # 3. After completing the step's actions + verifying:
        browser_plan(action="update_step", id=2, status="done",
                     evidence="screenshot shows X")

        # 4. After last step (typically "verify final state"):
        browser_plan(action="complete",
                     final_evidence="screenshot shows goal achieved")
    """

    # Max validator failures before we let `done` through anyway.
    # The agent has tried 3 times to provide convincing evidence;
    # rather than block forever, we accept and surface the warning
    # so the operator can see the validator's complaints in the
    # audit log. Skyvern uses 3-strike too.
    VALIDATOR_RETRY_CAP = 3

    def __init__(
        self,
        default_session_id: str = "default",
        provider: Any = None,
        registry: Any = None,
    ):
        # session_id is normally read from the registry's active
        # session (set by AgentLoop._process_message). When that's
        # not wired we fall back to default_session_id — fine for
        # smoke tests and single-session local development.
        self._default_session_id = default_session_id
        # Provider drives the validator (separate Haiku call). If
        # None, validator is disabled (degrade gracefully — Phase 1
        # behavior).
        self._provider = provider
        # Registry reference lets us read `_active_session_id` so
        # the agent doesn't have to pass session_id explicitly.
        self._registry = registry

    def _resolve_session_id(self, kwargs: dict[str, Any]) -> str:
        explicit = kwargs.get("session_id")
        if explicit:
            return str(explicit)
        if self._registry is not None:
            sess = getattr(self._registry, "_active_session_id", "")
            if sess:
                return str(sess)
        return self._default_session_id

    @property
    def name(self) -> str:
        return "browser_plan"

    @property
    def description(self) -> str:
        return (
            "Explicit planning for browser-driven tasks. ALWAYS call "
            "browser_plan(action='create', ...) FIRST before any browser_tab "
            "action that involves more than one step. The plan keeps you "
            "honest: each step has a successCriteria you must verify with "
            "evidence (screenshot description, DOM observation, URL change) "
            "before marking it done. Without a plan, you tend to forget "
            "what you were doing after 20+ tool calls and report success "
            "when the page state actually doesn't match the user's request "
            "— planning + evidence solves that.\n\n"
            "ACTIONS:\n"
            "- create(goal, steps[, domains]): start a new plan. "
            "goal=user's task in 1 sentence. "
            "steps=array of {id, content, successCriteria[, activeForm, "
            "dependsOn, runIf, parallelGroup]}. "
            "id=integer 1..N. "
            "content=imperative ('Click Format menu'). "
            "successCriteria=specific, observable ('Format menu open with "
            "Conditional formatting visible' — NOT 'menu opens'). "
            "activeForm=gerund ('Clicking Format menu') — optional, "
            "auto-derived if omitted. "
            "domains=list of hostnames the plan will touch (used by Phase 2 "
            "permission scope; harmless to omit in Phase 1). "
            "RETURNS: the plan as JSON (id + step list).\n"
            "- view(): return the current plan state. Use to refresh your "
            "memory after many tool calls.\n"
            "- update_step(id, status[, evidence]): change a step's status. "
            "status=pending|in_progress|done|blocked|skipped. "
            "evidence REQUIRED when status=done — describe HOW you verified "
            "(e.g. 'screenshot shows Conditional format rules sidebar with "
            "Custom formula is selected'). Don't make it up — actually "
            "screenshot/read_page first, then describe what you saw.\n"
            "- complete(final_evidence): declare the entire plan done. "
            "final_evidence MUST describe how you verified the user's "
            "original goal was achieved end-to-end (typically a final "
            "screenshot description). Cannot be called if any step is "
            "still pending or in_progress — finish or skip those first.\n\n"
            "WHEN TO CREATE A PLAN: any browser task that's more than one "
            "click. Even a 2-step task benefits from a plan because it "
            "forces you to write down what 'done' actually looks like.\n\n"
            "GOOD successCriteria: 'sidebar with Conditional format rules "
            "title is visible', 'rows 5-9 have red background fill', "
            "'URL changed to docs.google.com/.../editsuggestion'.\n"
            "BAD successCriteria: 'menu opens', 'looks right', 'works' — "
            "these are unverifiable; you'll mark step done when nothing "
            "actually changed.\n\n"
            "EVIDENCE: don't fabricate. If you say 'screenshot shows X', "
            "you must have taken a screenshot in the previous tool call "
            "and X must be visible in it. Lying here defeats the whole "
            "purpose — the next phase adds an automated validator that "
            "checks evidence against the screenshot."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["create", "view", "update_step", "complete"],
                },
                "goal": {
                    "type": "string",
                    "description": "User's task in one sentence (for create).",
                },
                "steps": {
                    "type": "array",
                    "description": (
                        "Array of step objects (for create). Each: "
                        "{id:int, content:str, successCriteria:str, "
                        "activeForm?:str, dependsOn?:[int], runIf?:str, "
                        "parallelGroup?:int, timeBudgetMs?:int}."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "integer"},
                            "content": {"type": "string"},
                            "successCriteria": {"type": "string"},
                            "activeForm": {"type": "string"},
                            "dependsOn": {
                                "type": "array",
                                "items": {"type": "integer"},
                            },
                            "runIf": {"type": "string"},
                            "parallelGroup": {"type": "integer"},
                            "timeBudgetMs": {"type": "integer"},
                        },
                        "required": ["id", "content", "successCriteria"],
                    },
                },
                "domains": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Hostnames the plan will touch (for create). "
                        "Reserved for Phase 2 permission scoping; safe to omit."
                    ),
                },
                "id": {
                    "type": "integer",
                    "description": "Step id to update (for update_step).",
                },
                "status": {
                    "type": "string",
                    "enum": ["pending", "in_progress", "done", "blocked", "skipped"],
                    "description": "New status (for update_step).",
                },
                "evidence": {
                    "type": "string",
                    "description": (
                        "How you verified the step (REQUIRED for status=done). "
                        "Describe what you observed: screenshot contents, DOM "
                        "fragments, URL changes, etc. Be specific."
                    ),
                },
                "final_evidence": {
                    "type": "string",
                    "description": (
                        "How you verified the WHOLE goal was achieved "
                        "(REQUIRED for complete). Typically a screenshot "
                        "description showing the final state matches the "
                        "user's request."
                    ),
                },
                "session_id": {
                    "type": "string",
                    "description": (
                        "Optional — defaults to the agent's current session. "
                        "Pass explicitly only when running multiple plans "
                        "in parallel from the same agent (rare)."
                    ),
                },
                "tab_id": {
                    "type": "integer",
                    "description": (
                        "Optional — distinct plan per tab when the agent "
                        "is driving multiple tabs concurrently."
                    ),
                },
            },
            "required": ["action"],
        }

    async def execute(self, action: str = "", **kwargs: Any) -> str:
        if not _is_enabled():
            return json.dumps({
                "error": "browser_plan disabled (FLOWLY_BROWSER_PLAN_ENABLED=0)."
            })

        # Defensive validation — same pattern as browser_tab
        valid_actions = {"create", "view", "update_step", "complete"}
        if action not in valid_actions:
            return json.dumps({
                "error": f"Unknown action: {action!r}. Valid: {sorted(valid_actions)}"
            })

        session_id = self._resolve_session_id(kwargs)
        tab_id = kwargs.get("tab_id")
        state = get_plan_state()

        try:
            if action == "create":
                return self._create(session_id, tab_id, kwargs)
            if action == "view":
                return self._view(session_id, tab_id)
            if action == "update_step":
                return await self._update_step(session_id, tab_id, kwargs)
            if action == "complete":
                return self._complete(session_id, tab_id, kwargs)
            return json.dumps({"error": f"Unhandled action: {action}"})
        except Exception as e:
            logger.exception(f"[browser_plan] {action} failed")
            return json.dumps({
                "error": f"browser_plan {action} failed: {e}",
                "error_class": type(e).__name__,
            })

    # ── Action handlers ──────────────────────────────────────────────

    def _create(self, session_id: str, tab_id, kwargs: dict[str, Any]) -> str:
        goal = str(kwargs.get("goal", "")).strip()
        if not goal:
            return json.dumps({
                "error": "create: goal is required (one-sentence task description).",
            })

        raw_steps = kwargs.get("steps") or []
        if not isinstance(raw_steps, list) or not raw_steps:
            return json.dumps({
                "error": "create: steps must be a non-empty array.",
            })

        steps: list[Step] = []
        seen_ids: set[int] = set()
        for raw in raw_steps:
            if not isinstance(raw, dict):
                return json.dumps({
                    "error": f"create: each step must be an object, got {type(raw).__name__}",
                })
            sid = raw.get("id")
            if not isinstance(sid, int) or sid < 1:
                return json.dumps({
                    "error": f"create: step id must be a positive int (got {sid!r})",
                })
            if sid in seen_ids:
                return json.dumps({
                    "error": f"create: duplicate step id {sid}",
                })
            seen_ids.add(sid)
            content = str(raw.get("content", "")).strip()
            criteria = str(raw.get("successCriteria", "")).strip()
            if not content:
                return json.dumps({
                    "error": f"create: step {sid} missing content (imperative description).",
                })
            if not criteria:
                return json.dumps({
                    "error": (
                        f"create: step {sid} missing successCriteria. Specify "
                        f"WHAT you can observe in the page to confirm this step "
                        f"actually happened (e.g. 'sidebar with X visible', "
                        f"'rows N-M have red background')."
                    ),
                })
            active_form = str(raw.get("activeForm", "")).strip()
            if not active_form:
                # Auto-derive: "Click X" → "Clicking X" — naive but
                # cheap; agent can override if it cares.
                active_form = self._imperative_to_gerund(content)

            steps.append(Step(
                id=sid,
                content=content,
                activeForm=active_form,
                successCriteria=criteria,
                dependsOn=list(raw.get("dependsOn") or []),
                runIf=raw.get("runIf"),
                parallelGroup=raw.get("parallelGroup"),
                timeBudgetMs=int(raw.get("timeBudgetMs") or 60_000),
            ))

        domains = kwargs.get("domains") or []
        if not isinstance(domains, list):
            domains = []

        # Replace any existing plan for this slot — re-creating means
        # the agent has explicitly decided the previous plan is over.
        # If the agent meant to revise (preserve history), Phase 3
        # adds a `revise` action; for now this is destructive but
        # explicit.
        plan = Plan.new(
            sessionId=session_id,
            goal=goal,
            steps=steps,
            domains=[str(d) for d in domains],
        )
        get_plan_state().set(plan, tab_id=tab_id)

        logger.info(f"[browser_plan] created {plan.id} with {len(steps)} steps for session={session_id}")

        return json.dumps({
            "success": True,
            "plan": plan.public_view(),
            "note": (
                "Plan created. As you execute each step: (1) optionally "
                "mark it in_progress before starting, (2) take the actions, "
                "(3) verify with screenshot/read_page, (4) call "
                "update_step(id, status='done', evidence='...'). When the "
                "FINAL step verifies the user's goal end-to-end, call "
                "complete(final_evidence='...')."
            ),
        })

    def _view(self, session_id: str, tab_id) -> str:
        plan = get_plan_state().get(session_id, tab_id=tab_id)
        if not plan:
            return json.dumps({
                "success": True,
                "plan": None,
                "note": (
                    "No plan exists for this session/tab yet. Call "
                    "create(...) first. If your task is genuinely a single "
                    "atomic action, you can proceed without a plan — but "
                    "anything multi-step benefits from one."
                ),
            })
        return json.dumps({
            "success": True,
            "plan": plan.public_view(),
        })

    async def _update_step(self, session_id: str, tab_id, kwargs: dict[str, Any]) -> str:
        plan = get_plan_state().get(session_id, tab_id=tab_id)
        if not plan:
            return json.dumps({
                "error": "update_step: no plan exists. Call create(...) first.",
            })

        sid = kwargs.get("id")
        if not isinstance(sid, int):
            return json.dumps({"error": "update_step: id (int) required."})
        step = plan.get_step(sid)
        if not step:
            valid_ids = [s.id for s in plan.steps]
            return json.dumps({
                "error": f"update_step: no step with id={sid}. Valid ids: {valid_ids}",
            })

        new_status: StepStatus = kwargs.get("status") or "in_progress"
        valid_statuses = {"pending", "in_progress", "done", "blocked", "skipped"}
        if new_status not in valid_statuses:
            return json.dumps({
                "error": f"update_step: invalid status {new_status!r}. Valid: {sorted(valid_statuses)}",
            })

        evidence = kwargs.get("evidence")
        if new_status == "done":
            if not evidence or not str(evidence).strip():
                return json.dumps({
                    "error": (
                        "update_step: evidence is REQUIRED when status='done'. "
                        "Describe how you verified the step (e.g. 'screenshot "
                        "shows ...', 'read_page output contains ...'). Don't "
                        "fabricate — an automated validator checks evidence "
                        "against the success criteria."
                    ),
                    "error_code": "EVIDENCE_REQUIRED",
                    "step": step.short_label(),
                    "successCriteria": step.successCriteria,
                })

        # ── Validator pass (Skyvern pattern) ──────────────────────────
        # Only on the done flip — skipped/blocked/in_progress don't
        # need evidence verification, just status bookkeeping.
        validator_result = None
        if new_status == "done":
            from flowly.agent.planner.validator import validate_step
            validator_result = await validate_step(
                step=step,
                evidence=str(evidence).strip(),
                provider=self._provider,
            )
            if validator_result.matches is False:
                # Real verdict: evidence does NOT match criteria.
                # Bump retries; if cap reached, accept anyway with
                # a loud warning so the operator can audit. Otherwise
                # reject and tell agent what would convince validator.
                step.retries += 1
                step.validatorReasoning = validator_result.reason
                if step.retries < self.VALIDATOR_RETRY_CAP:
                    plan.updatedAt = time.time()
                    get_plan_state().set(plan, tab_id=tab_id)  # persist retry count
                    return json.dumps({
                        "error": (
                            f"Validator rejected your evidence "
                            f"({step.retries}/{self.VALIDATOR_RETRY_CAP} attempts). "
                            f"Reason: {validator_result.reason}"
                            + (f" Suggested fix: {validator_result.suggested_fix}"
                               if validator_result.suggested_fix else "")
                        ),
                        "error_code": "VALIDATOR_REJECTED",
                        "step": step.short_label(),
                        "successCriteria": step.successCriteria,
                        "your_evidence": str(evidence)[:200],
                        "validator_reason": validator_result.reason,
                        "suggested_fix": validator_result.suggested_fix,
                        "retries_remaining": self.VALIDATOR_RETRY_CAP - step.retries,
                    })
                # Cap reached — accept but keep the rejection on record
                logger.warning(
                    f"[browser_plan] {plan.id} step {sid} accepted after "
                    f"{step.retries} validator rejections: {validator_result.reason}"
                )
            elif validator_result.matches is None:
                # Infra failure (timeout/no provider/bad JSON). Soft pass.
                logger.warning(
                    f"[browser_plan] validator infra fail on step {sid}: "
                    f"{validator_result.reason} — accepting evidence as-is"
                )
            # validator_result.matches is True → silent accept.
            if validator_result.matches is True:
                step.validatorReasoning = validator_result.reason

        # ── Lifecycle bookkeeping ────────────────────────────────────
        now = time.time()
        if new_status == "in_progress" and step.startedAt is None:
            step.startedAt = now
        if new_status in ("done", "blocked", "skipped") and step.completedAt is None:
            step.completedAt = now
        step.status = new_status
        if evidence:
            step.evidence = str(evidence).strip()
        plan.updatedAt = now
        get_plan_state().set(plan, tab_id=tab_id)  # persist the update

        # Soft hint when agent goes pending → done bypassing in_progress.
        warning = None
        if new_status == "done" and step.startedAt is None:
            warning = (
                "Marked done without ever being in_progress. That's allowed "
                "but unusual — usually means you skipped the action."
            )

        logger.info(f"[browser_plan] {plan.id} step {sid} -> {new_status}")
        result: dict[str, Any] = {
            "success": True,
            "step": {
                "id": step.id,
                "content": step.content,
                "successCriteria": step.successCriteria,
                "status": step.status,
                "evidence": step.evidence,
                "validatorReasoning": step.validatorReasoning,
                "retries": step.retries,
            },
            "progress": plan.progress_summary(),
        }
        if validator_result is not None:
            result["validator"] = validator_result.to_dict()
        if warning:
            result["warning"] = warning
        return json.dumps(result)

    def _complete(self, session_id: str, tab_id, kwargs: dict[str, Any]) -> str:
        plan = get_plan_state().get(session_id, tab_id=tab_id)
        if not plan:
            return json.dumps({
                "error": "complete: no plan exists. Call create(...) first.",
            })

        final_evidence = kwargs.get("final_evidence")
        if not final_evidence or not str(final_evidence).strip():
            return json.dumps({
                "error": (
                    "complete: final_evidence is REQUIRED. Describe how you "
                    "verified the user's ORIGINAL goal was achieved end-to-"
                    "end (typically a final screenshot description showing "
                    "the result matches the request)."
                ),
                "error_code": "FINAL_EVIDENCE_REQUIRED",
            })

        # Refuse if any step is still active. Skipped is fine, blocked is
        # fine (agent has acknowledged it can't), but pending/in_progress
        # are unfinished work. The agent must either finish them, mark
        # blocked with a reason, or explicitly skip.
        unfinished = [s for s in plan.steps if s.status in ("pending", "in_progress")]
        if unfinished:
            return json.dumps({
                "error": (
                    "complete: cannot mark plan complete while steps are "
                    "still pending/in_progress. Finish them, mark them "
                    "blocked with a reason, or skipped if not applicable."
                ),
                "error_code": "STEPS_INCOMPLETE",
                "unfinished_steps": [
                    {"id": s.id, "content": s.content, "status": s.status}
                    for s in unfinished
                ],
            })

        plan.status = "complete"
        plan.finalEvidence = str(final_evidence).strip()
        plan.updatedAt = time.time()

        logger.info(f"[browser_plan] {plan.id} marked complete: {plan.finalEvidence[:80]}")
        return json.dumps({
            "success": True,
            "plan": plan.public_view(),
            "note": (
                "Plan marked complete. Phase 2 will add a validator that "
                "checks final_evidence against a fresh screenshot before "
                "accepting. For now, your reputation is on the line — "
                "don't claim done if it isn't."
            ),
        })

    # ── helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _imperative_to_gerund(text: str) -> str:
        """Cheap heuristic: 'Click X' -> 'Clicking X'. Used when activeForm
        is omitted. The agent can always pass an explicit one if it
        cares about the exact label.
        """
        if not text:
            return ""
        words = text.split()
        if not words:
            return text
        verb = words[0]
        # Strip trailing punctuation
        suffix = ""
        while verb and not verb[-1].isalpha():
            suffix = verb[-1] + suffix
            verb = verb[:-1]
        if not verb:
            return text
        # Naive English gerund formation
        lower = verb.lower()
        if lower.endswith("e") and not lower.endswith("ee"):
            gerund = verb[:-1] + "ing"
        elif lower.endswith("ie"):
            gerund = verb[:-2] + "ying"
        elif (
            len(verb) >= 3
            and verb[-1].lower() not in "aeiou"
            and verb[-2].lower() in "aeiou"
            and verb[-3].lower() not in "aeiou"
            and lower not in {"open", "edit", "send"}  # known false positives
        ):
            # double-consonant rule for short CVC verbs (run -> running)
            gerund = verb + verb[-1] + "ing"
        else:
            gerund = verb + "ing"
        # Capitalise like the input
        if verb[0].isupper():
            gerund = gerund[0].upper() + gerund[1:]
        return " ".join([gerund + suffix, *words[1:]]).strip()
