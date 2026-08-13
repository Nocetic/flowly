"""Budget and compact one provider request immediately before dispatch.

Session compaction keeps durable history bounded between turns.  Tool calls,
plugin context, routed schemas, and tool results can still make the *current*
request grow after that checkpoint.  This coordinator operates only on the
provider-facing working copy: it never rewrites the canonical session, while
the append-only turn journal continues to preserve everything produced during
the turn for later persistence.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Callable

from flowly.compaction.estimator import estimate_messages_tokens, estimate_tokens
from flowly.compaction.service import CompactionService
from flowly.compaction.types import (
    SUMMARY_METADATA_KEY,
    CompactionError,
    CompactionResult,
    build_summary_content,
    is_summary_message,
)


@dataclass(frozen=True)
class RequestBudget:
    """Auditable estimate of one provider payload."""

    input_limit: int
    messages_tokens: int
    tool_schema_tokens: int
    fixed_prefix_tokens: int
    compactable_tokens: int

    @property
    def estimated_input_tokens(self) -> int:
        return self.messages_tokens + self.tool_schema_tokens

    @property
    def compactable_budget(self) -> int:
        return self.input_limit - self.fixed_prefix_tokens - self.tool_schema_tokens


@dataclass(frozen=True)
class RequestFit:
    """Result of pre-dispatch fitting or overflow recovery."""

    messages: list[dict[str, Any]]
    before: RequestBudget
    after: RequestBudget
    changed: bool = False
    compaction: CompactionResult | None = None
    failure: str | None = None

    @property
    def fits(self) -> bool:
        return self.failure is None and (
            self.after.estimated_input_tokens <= self.after.input_limit
        )


def request_fingerprint(messages: list[dict[str, Any]]) -> str:
    """Stable proof that an overflow retry is not resending the same payload."""
    raw = json.dumps(
        messages,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class ProviderRequestCoordinator:
    """Fit transient conversation context to the selected provider route."""

    # Schema serialization and provider envelopes carry a little overhead not
    # represented in either estimator. It is intentionally small because the
    # configured reply reserve already supplies the substantial safety margin.
    WIRE_OVERHEAD_TOKENS = 64
    # A server-reported overflow is stronger evidence than our tokenizer. The
    # retry must be materially smaller, not merely one estimated token below a
    # threshold the server already disproved.
    OVERFLOW_RETRY_HISTORY_RATIO = 0.65

    def __init__(self, compaction: CompactionService):
        self.compaction = compaction

    @staticmethod
    def _split_fixed_prefix(
        messages: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Keep leading authoritative system instructions out of summaries."""
        boundary = 0
        for message in messages:
            if message.get("role") != "system" or is_summary_message(message):
                break
            boundary += 1
        return list(messages[:boundary]), list(messages[boundary:])

    @classmethod
    def _tool_tokens(cls, tools: list[dict[str, Any]] | None) -> int:
        if not tools:
            return 0
        return estimate_tokens(
            json.dumps(
                tools,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            )
        ) + cls.WIRE_OVERHEAD_TOKENS

    def budget(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        model: str,
    ) -> RequestBudget:
        fixed, compactable = self._split_fixed_prefix(messages)
        return RequestBudget(
            input_limit=self.compaction.compaction_threshold_for(model),
            messages_tokens=estimate_messages_tokens(messages),
            tool_schema_tokens=self._tool_tokens(tools),
            fixed_prefix_tokens=estimate_messages_tokens(fixed),
            compactable_tokens=estimate_messages_tokens(compactable),
        )

    async def fit(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        model: str,
        *,
        session_key: str = "",
        force_reduction: bool = False,
        should_cancel: Callable[[], bool] | None = None,
    ) -> RequestFit:
        """Return a bounded provider working copy or a fail-closed result.

        ``force_reduction`` is used only after a structured 413/context
        overflow.  It targets a substantially smaller history even if the
        local estimate claimed the rejected request fit.
        """
        original = list(messages)
        before = self.budget(original, tools, model)
        if not force_reduction and before.estimated_input_tokens <= before.input_limit:
            return RequestFit(original, before, before)

        fixed, compactable = self._split_fixed_prefix(original)
        if before.compactable_budget <= 0:
            return RequestFit(
                original,
                before,
                before,
                failure=(
                    "fixed system instructions and tool schemas leave no room "
                    "for conversation context"
                ),
            )
        if not compactable:
            return RequestFit(
                original,
                before,
                before,
                failure="request has no compactable conversation history",
            )

        # Cheap, loss-bounded first pass. This is especially effective for a
        # long tool loop and avoids spending a summary call when old tool output
        # alone caused the overflow.
        micro = self.compaction.microcompact(compactable)
        candidate = [*fixed, *micro]
        micro_budget = self.budget(candidate, tools, model)
        micro_changed = request_fingerprint(candidate) != request_fingerprint(original)
        force_target = min(
            before.compactable_budget,
            max(1, int(before.compactable_tokens * self.OVERFLOW_RETRY_HISTORY_RATIO)),
        )
        if micro_budget.estimated_input_tokens <= micro_budget.input_limit and (
            not force_reduction
            or micro_budget.compactable_tokens <= force_target
        ):
            return RequestFit(
                candidate,
                before,
                micro_budget,
                changed=micro_changed,
            )

        target_history_budget = (
            force_target if force_reduction else before.compactable_budget
        )
        try:
            result = await self.compaction.compact(
                micro,
                custom_instructions=(
                    "Create a compact continuity reference. Preserve established "
                    "facts, decisions, constraints, tool outcomes, unresolved "
                    "issues, and the current user's exact intent. Do not turn "
                    "historical requests into current instructions."
                ),
                session_key=session_key,
                should_cancel=should_cancel,
                history_budget=target_history_budget,
                track_cycle=False,
            )
        except CompactionError as exc:
            return RequestFit(
                original,
                before,
                micro_budget,
                changed=micro_changed,
                failure=f"transient request compaction failed: {exc}",
            )

        summary = {
            "role": "system",
            "content": build_summary_content(result.summary),
            SUMMARY_METADATA_KEY: True,
        }
        fitted = [*fixed, summary, *result.kept_messages]
        after = self.budget(fitted, tools, model)
        changed = request_fingerprint(fitted) != request_fingerprint(original)
        if not changed:
            return RequestFit(
                original,
                before,
                after,
                failure="request compaction produced an identical payload",
            )
        if after.estimated_input_tokens > after.input_limit:
            return RequestFit(
                original,
                before,
                after,
                changed=True,
                compaction=result,
                failure=(
                    f"compacted request still needs {after.estimated_input_tokens} "
                    f"tokens for an input limit of {after.input_limit}"
                ),
            )
        if force_reduction and after.compactable_tokens > force_target:
            return RequestFit(
                original,
                before,
                after,
                changed=True,
                compaction=result,
                failure=(
                    "overflow recovery did not reduce conversation context "
                    "enough for a safe retry"
                ),
            )
        return RequestFit(
            fitted,
            before,
            after,
            changed=True,
            compaction=result,
        )
