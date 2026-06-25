"""Audit logger — records every tool call and LLM call to a JSONL file.

Each line is a self-contained JSON object.  Files are rotated daily:
  ~/.flowly/audit/YYYY-MM-DD.jsonl

The logger is intentionally simple: best-effort, non-blocking, never raises.
"""

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from loguru import logger as _logger
from flowly.profile import get_flowly_home


class AuditLogger:
    """Write tool-call and LLM-call audit records to daily JSONL files."""

    def __init__(self, audit_dir: Path | None = None):
        if audit_dir is None:
            audit_dir = get_flowly_home() / "audit"
        self._dir = audit_dir

    def _file(self) -> Path:
        date = datetime.now().strftime("%Y-%m-%d")
        return self._dir / f"{date}.jsonl"

    def _write(self, record: dict[str, Any]) -> None:
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            path = self._file()
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
            # Restrict to owner read/write
            try:
                from flowly.utils.file_security import secure_file
                secure_file(path)  # POSIX chmod; real owner-only ACL on Windows
            except OSError:
                pass
        except Exception as e:
            _logger.debug(f"[Audit] Write failed (non-fatal): {e}")

    def log_tool_call(
        self,
        *,
        session_key: str,
        tool_name: str,
        args: dict[str, Any],
        result: str,
        duration_ms: int,
        success: bool,
    ) -> None:
        """Record a tool execution."""
        # Redact large results; keep first 300 chars for context
        result_snippet = result[:300] + ("…" if len(result) > 300 else "")
        # Redact sensitive arg keys
        safe_args = {
            k: ("***" if k.lower() in {"password", "token", "secret", "key", "api_key"} else v)
            for k, v in args.items()
        }
        self._write({
            "type": "tool_call",
            "ts": datetime.now(tz=timezone.utc).isoformat(),
            "session": session_key,
            "tool": tool_name,
            "args": safe_args,
            "result_snippet": result_snippet,
            "duration_ms": duration_ms,
            "success": success,
        })

    def log_llm_call(
        self,
        *,
        session_key: str,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        duration_ms: int,
        tool_choice: str = "auto",
        iteration: int = 0,
        finish_reason: str | None = None,
        families: list[str] | None = None,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
        streamed: bool | None = None,
        purpose: str | None = None,
    ) -> None:
        """Record an LLM API call.

        Args:
            session_key: ``{channel}:{chat_id}`` so audit lines can be
                grouped by conversation.
            model: Full model id as sent to the provider
                (e.g. ``"openai/gpt-5.5"``).
            prompt_tokens / completion_tokens: From the provider response
                ``usage`` dict. Both default to 0 when the provider
                doesn't report them.
            duration_ms: Wall-clock time from request start to final
                chunk (streaming) or response decode (non-streaming).
            tool_choice: ``"auto"`` / ``"required"`` / ``"none"`` — the
                value sent to the provider for this turn.
            iteration: Per-turn iteration counter inside
                ``_run_llm_tool_loop``. Useful for spotting tool-loop
                regressions in the wild.
            finish_reason: ``"stop"`` / ``"tool_calls"`` / ``"length"``
                / ``"error"`` / ... Helps tell genuine completions
                from truncations and tool-only turns when reading audit
                logs back.
            families: The model-family tags inferred for ``model``
                (e.g. ``["openai"]``). Optional — when omitted, post-hoc
                reconstruction from ``model`` is possible.
            cache_read_tokens / cache_write_tokens: Anthropic prompt-
                cache counters when available. 0 for providers that
                don't report them.
            streamed: True if the call used ``chat_stream`` rather than
                ``chat``. Lets us distinguish streaming-path metrics
                in aggregations.
            purpose: Non-OpenAI field carried through to Flowly's
                proxy — typically ``"coaching"`` for the coaching
                pipeline. ``None`` for ordinary chat.
        """
        record: dict[str, Any] = {
            "type": "llm_call",
            "ts": datetime.now(tz=timezone.utc).isoformat(),
            "session": session_key,
            "model": model,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
            "duration_ms": duration_ms,
            "tool_choice": tool_choice,
            "iteration": iteration,
        }
        # Optional fields — only include when set so JSONL stays compact
        # and old log scrapers that don't know about them keep working.
        if finish_reason is not None:
            record["finish_reason"] = finish_reason
        if families:
            record["families"] = sorted(families)
        if cache_read_tokens:
            record["cache_read_tokens"] = cache_read_tokens
        if cache_write_tokens:
            record["cache_write_tokens"] = cache_write_tokens
        if streamed is not None:
            record["streamed"] = streamed
        if purpose:
            record["purpose"] = purpose
        self._write(record)

    def log_overflow_recovery(
        self,
        *,
        session_key: str,
        tokens_before: int,
        tokens_after: int,
        messages_dropped: int,
    ) -> None:
        """Record a context overflow recovery event."""
        self._write({
            "type": "overflow_recovery",
            "ts": datetime.now(tz=timezone.utc).isoformat(),
            "session": session_key,
            "tokens_before": tokens_before,
            "tokens_after": tokens_after,
            "messages_dropped": messages_dropped,
        })

    def log_key_rotation(
        self,
        *,
        provider: str,
        reason: str,
        key_index_from: int,
        key_index_to: int,
    ) -> None:
        """Record an API key rotation event."""
        self._write({
            "type": "key_rotation",
            "ts": datetime.now(tz=timezone.utc).isoformat(),
            "provider": provider,
            "reason": reason,
            "from_index": key_index_from,
            "to_index": key_index_to,
        })


# Module-level singleton — lazily initialized
_instance: AuditLogger | None = None


def get_audit_logger(audit_dir: Path | None = None) -> AuditLogger:
    """Get (or create) the module-level AuditLogger singleton."""
    global _instance
    if _instance is None:
        _instance = AuditLogger(audit_dir)
    return _instance
