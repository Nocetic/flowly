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
from typing import Any, Awaitable, Callable

from loguru import logger

from flowly.compaction.estimator import estimate_messages_tokens, estimate_tokens
from flowly.compaction.service import CompactionService
from flowly.compaction.types import (
    SUMMARY_METADATA_KEY,
    CompactionError,
    CompactionResult,
    build_summary_content,
    is_summary_message,
)
from flowly.providers.base import LLMErrorInfo, LLMResponse

# What a transient request summary is asked to keep, unless the caller says.
REQUEST_SUMMARY_INSTRUCTIONS = (
    "Create a compact continuity reference. Preserve established "
    "facts, decisions, constraints, tool outcomes, unresolved "
    "issues, and the current user's exact intent. Do not turn "
    "historical requests into current instructions."
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
    # The failure was a summary call that did not produce a usable summary
    # (provider error, timeout, a summary that would not shrink anything).
    # Another attempt may succeed; every other failure is structural.
    retryable: bool = False

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


def request_too_large_response(reason: str) -> LLMResponse:
    """A provider-shaped refusal for a request that cannot be made to fit.

    Fails closed with machine-routable metadata (classified INPUT_TOO_LARGE)
    and user-safe prose instead of sending a payload the provider would
    reject.
    """
    logger.warning("Provider request blocked before dispatch: {}", reason)
    return LLMResponse(
        content="Error calling LLM: request input is too large after compaction",
        finish_reason="error",
        error_info=LLMErrorInfo(
            status_code=413,
            code="input_too_large",
            type="request_too_large",
        ),
    )


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
    def _partition(
        cls,
        messages: list[dict[str, Any]],
        pinned: int,
        pin_at: int | None = None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]],
               list[dict[str, Any]], list[dict[str, Any]]]:
        """``(fixed, summaries, before, pins, after)`` in list order.

        ``fixed`` are the leading system instructions; ``summaries`` the
        compaction summaries that directly follow them (an earlier fit's);
        ``pins`` authoritative input that is kept verbatim (a subagent's
        task); ``before`` and ``after`` the compactable messages on either
        side of the pins. With ``pinned`` the pins are the next ``pinned``
        messages after the summaries (``before`` is empty); with ``pin_at``
        the pin is the one message at that index of ``messages``, wherever
        it stands — a subagent's later task follows its earlier turns.
        Only ``summaries``, ``before`` and ``after`` are compactable.
        """
        fixed, tail = cls._split_fixed_prefix(messages)
        pin = None if pin_at is None else pin_at - len(fixed)
        index = 0
        while (
            (pinned > 0 or pin is not None)
            and index < len(tail)
            and (pin is None or index < pin)
            and is_summary_message(tail[index])
        ):
            index += 1
        summaries = tail[:index]
        if pin is not None:
            if not index <= pin < len(tail):
                return fixed, summaries, [], [], tail[index:]
            return fixed, summaries, tail[index:pin], [tail[pin]], tail[pin + 1:]
        pins = tail[index:index + max(0, pinned)]
        return fixed, summaries, [], pins, tail[index + len(pins):]

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
        *,
        pinned: int = 0,
        pin_at: int | None = None,
    ) -> RequestBudget:
        fixed, summaries, before, pins, after = self._partition(messages, pinned, pin_at)
        return RequestBudget(
            input_limit=self.compaction.compaction_threshold_for(model),
            messages_tokens=estimate_messages_tokens(messages),
            tool_schema_tokens=self._tool_tokens(tools),
            fixed_prefix_tokens=estimate_messages_tokens([*fixed, *pins]),
            compactable_tokens=estimate_messages_tokens([*summaries, *before, *after]),
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
        pinned: int = 0,
        pin_at: int | None = None,
        custom_instructions: str | None = None,
        on_summary_start: Callable[[], Awaitable[None]] | None = None,
    ) -> RequestFit:
        """Return a bounded provider working copy or a fail-closed result.

        ``force_reduction`` is used only after a structured 413/context
        overflow.  It targets a substantially smaller history even if the
        local estimate claimed the rejected request fit.

        ``pinned`` messages after the leading system instructions (and any
        earlier summary) are kept verbatim and placed after a new summary;
        ``pin_at`` instead keeps the one message at that index verbatim where
        it stands: a summary takes its place before it, and what the summary
        keeps stays on the side of it where it was.
        ``custom_instructions`` replaces what the summary is asked to keep.
        The main agent passes neither. ``on_summary_start`` is awaited just
        before a summary call (not for a cut alone); a failure in it is
        ignored.
        """
        original = list(messages)
        before = self.budget(original, tools, model, pinned=pinned, pin_at=pin_at)
        if not force_reduction and before.estimated_input_tokens <= before.input_limit:
            return RequestFit(original, before, before)

        fixed, summaries, ahead, pins, behind = self._partition(original, pinned, pin_at)
        rest = [*ahead, *behind]
        compactable = [*summaries, *rest]
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
        # alone caused the overflow. Summaries and pins hold no tool results,
        # so the list keeps its order (a cut keeps every message in place).
        micro_rest = self.compaction.microcompact(rest)
        micro_ahead, micro_behind = micro_rest[:len(ahead)], micro_rest[len(ahead):]
        micro = [*summaries, *micro_rest]
        candidate = [*fixed, *summaries, *micro_ahead, *pins, *micro_behind]
        micro_budget = self.budget(candidate, tools, model, pinned=pinned, pin_at=pin_at)
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
        if on_summary_start is not None:
            try:
                await on_summary_start()
            except Exception:  # noqa: BLE001 — a notice never fails the request
                logger.debug("on_summary_start failed (non-fatal)", exc_info=True)
        try:
            result = await self.compaction.compact(
                micro,
                custom_instructions=custom_instructions or REQUEST_SUMMARY_INSTRUCTIONS,
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
                retryable=True,
            )

        summary = {
            "role": "system",
            "content": build_summary_content(result.summary),
            SUMMARY_METADATA_KEY: True,
        }
        # What the summary kept stays on its side of a pin placed with
        # ``pin_at`` (kept messages are the very objects it was given).
        ahead_ids = {id(message) for message in micro_ahead}
        kept_ahead = [m for m in result.kept_messages if id(m) in ahead_ids]
        kept_behind = [m for m in result.kept_messages if id(m) not in ahead_ids]
        fitted = [*fixed, summary, *kept_ahead, *pins, *kept_behind]
        after = self.budget(fitted, tools, model, pinned=pinned,
                            pin_at=None if pin_at is None or not pins
                            else len(fixed) + 1 + len(kept_ahead))
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
