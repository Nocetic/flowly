"""Agent loop: the core processing engine."""

import asyncio
import copy
import json
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Awaitable

from loguru import logger

from flowly.bus.events import InboundMessage, OutboundMessage
from flowly.bus.queue import MessageBus
from flowly.providers.base import LLMProvider
from flowly.agent.context import ContextBuilder
from flowly.agent.tools.registry import ToolRegistry
from flowly.agent.tools.filesystem import ReadFileTool, WriteFileTool, EditFileTool, ListDirTool, MemoryAppendTool
from flowly.agent.tools.web import WebSearchTool, WebFetchTool, WebExtractTool
from flowly.agent.tools.message import MessageTool
from flowly.agent.tools.screenshot import ScreenshotTool
from flowly.agent.tools.spawn import SpawnTool
from flowly.agent.tools.cron import CronTool
from flowly.agent.tools.trello import TrelloTool
from flowly.agent.tools.docker import DockerTool
from flowly.agent.tools.system import SystemTool
from flowly.agent.tools.voice import VoiceCallTool
from flowly.agent.tools.process import ProcessTool
from flowly.exec.process_registry import get_registry as _get_process_registry
from flowly.exec.process_checkpoint import ProcessCheckpoint
from flowly.agent.subagent import SubagentManager
from flowly.session.manager import SessionManager
from flowly.cron.service import CronService
from flowly.compaction.service import CompactionService
from flowly.compaction.types import CompactionConfig, MemoryFlushConfig
from flowly.compaction.estimator import estimate_messages_tokens, estimate_tokens
from flowly.exec.types import ExecConfig
from flowly.config.schema import TrelloConfig, VoiceBridgeConfig, XConfig, MemorySearchConfig
from flowly.audit.logger import get_audit_logger
from flowly.providers.key_rotator import is_context_overflow
from flowly.agent.prompt_blocks import detect_model_families
from flowly.agent.reply_media import extract_reply_media
from flowly.agent.tool_result_spill import build_spill_pointer, spill_tool_result


# ---------------------------------------------------------------------------
# Tool result sanitization — prevent token bloat
# ---------------------------------------------------------------------------

_BASE64_IMAGE_RE = re.compile(r'data:image/[^;]+;base64,[A-Za-z0-9+/=]{100,}')

# Per-tool max chars (keeps most important info, truncates verbose output)
_TOOL_MAX_CHARS: dict[str, int] = {
    "browser_tab": 5000,
    "exec": 8000,
    "read_file": 8000,
    "screenshot": 200,
    "computer": 3000,
    "web_search": 4000,
    "web_fetch": 6000,
    "process": 6000,
}
_DEFAULT_MAX_CHARS = 8000

# Channels that are NOT genuine user activity: the agent's own background runs.
# Memory learning (idle + turn dreamer triggers) must ignore these so a periodic
# heartbeat/cron tick never looks like a conversation worth remembering.
_NON_USER_CHANNELS = ("system", "heartbeat", "cron")


def _is_user_activity_channel(channel: str) -> bool:
    """True when ``channel`` is real user conversation (not a background run)."""
    return (channel or "") not in _NON_USER_CHANNELS


# ── Built-in agent keyword routing ──────────────────────────────────────────

_WRITING_KW = frozenset({
    "rapor", "report", "essay", "makale", "article", "doküman", "document",
    "yaz", "write", "hazırla", "prepare", "oluştur", "çalışma kağıdı",
    "study guide", "özet", "summary", "mektup", "letter", "döküman",
    "composition", "draft", "taslak",
})
_RESEARCH_KW = frozenset({
    "araştır", "research", "analiz", "analysis", "karşılaştır", "compare",
    "incele", "investigate", "kaynak", "source", "bul ve özetle",
})
_CODE_KW = frozenset({
    "code review", "refactor", "debug", "test yaz", "write test",
    "kod incele", "hata bul", "fix bug", "kod review",
})


def _detect_builtin_agent_type(task_text: str) -> str | None:
    """Detect if a spawn task should be routed to a built-in specialist agent.

    Returns agent type ('writer', 'researcher', 'coder') or None.
    Uses keyword scoring to pick the best-matching specialist.
    """
    t = task_text.lower()
    scores = {
        "writer": sum(1 for kw in _WRITING_KW if kw in t),
        "researcher": sum(1 for kw in _RESEARCH_KW if kw in t),
        "coder": sum(1 for kw in _CODE_KW if kw in t),
    }
    best = max(scores, key=scores.get)  # type: ignore[arg-type]
    return best if scores[best] > 0 else None


def _maybe_extract_image_for_vision(raw_result: str, tool_name: str) -> str | None:
    """If the raw tool result asks the loop to render an image content block,
    return the image data URL. Otherwise return None.

    Browser_tab's screenshot action returns
    ``{"_render_as_image": true, "image_data_url": "data:image/jpeg;base64,..."}``
    when it wants the agent to actually SEE the image. The loop converts
    that into an ``image_url`` content block on the tool_result message
    so vision-capable models (Claude 3.5+, GPT-4o, etc.) get real visual
    input — not a base64 string they treat as opaque text.

    Without this, the agent calls screenshot, the result is sanitized to
    "[image data stripped]", and the agent stays blind on canvas-rendered
    apps (Sheets, Figma, Miro). That was the #1 reason browser_tab
    looped forever on those targets.
    """
    if tool_name != "browser_tab":
        return None
    try:
        parsed = json.loads(raw_result)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None
    if not parsed.get("_render_as_image"):
        return None
    url = parsed.get("image_data_url")
    if not isinstance(url, str) or not url.startswith("data:image/"):
        return None
    return url


# ── Leaked tool-call markup ───────────────────────────────────────────────
# Some models (e.g. deepseek-v4) sometimes emit a tool call as INLINE TEXT in
# their native markup instead of using the structured tool-call API. The loop
# then sees no structured ``tool_calls`` and would save that markup as the
# assistant's final answer, leaking raw tool-call syntax into the chat. These
# helpers detect + strip that markup from the TEXT content only — they never
# touch ``response.tool_calls`` (real, structured calls), so tool execution and
# the tool-card UI are completely unaffected.

# Fullwidth vertical bar (U+FF5C) — used in deepseek/DSML markers like
# ``<｜｜DSML｜｜tool_calls>`` and ``<｜tool▁calls▁begin｜>``.
_PIPE = "[｜|]"
_DSML_BLOCK_RE = re.compile(
    rf"<\s*{_PIPE}+\s*DSML\s*{_PIPE}+\s*tool_calls\s*>.*?</\s*{_PIPE}+\s*DSML\s*{_PIPE}+\s*tool_calls\s*>",
    re.DOTALL,
)
_DSML_TAG_RE = re.compile(rf"</?\s*{_PIPE}+\s*DSML\s*{_PIPE}+[^>]*>", re.DOTALL)
_DEEPSEEK_MARK_RE = re.compile(rf"<\s*{_PIPE}\s*tool[▁_ ]*calls?[▁_ ]*(?:begin|end)\s*{_PIPE}\s*>")
_XML_TOOLCALLS_RE = re.compile(r"<\s*(tool_calls|function_calls)\s*>.*?</\s*\1\s*>", re.DOTALL | re.IGNORECASE)
_XML_INVOKE_RE = re.compile(r"<\s*invoke\b[^>]*>.*?</\s*invoke\s*>", re.DOTALL | re.IGNORECASE)
_CODE_FENCE_RE = re.compile(r"(```.*?```|~~~.*?~~~)", re.DOTALL)


def _contains_leaked_tool_markup(text: str | None) -> bool:
    """True when *text* carries inline tool-call markup outside any code fence."""
    if not text:
        return False
    probe = _CODE_FENCE_RE.sub("", text)  # ignore fenced examples
    return bool(
        _DSML_TAG_RE.search(probe)
        or _DEEPSEEK_MARK_RE.search(probe)
        or _XML_TOOLCALLS_RE.search(probe)
        or _XML_INVOKE_RE.search(probe)
    )


def _strip_leaked_tool_markup(text: str | None) -> str:
    """Remove inline tool-call markup from *text*, preserving fenced code blocks.

    Targeted: only the known leaked-markup shapes are removed, so legitimate
    prose is untouched. Returns the cleaned text (may be empty if the whole
    message was a leaked tool call — the caller then forces a corrective retry).
    """
    if not text:
        return text or ""
    parts = _CODE_FENCE_RE.split(text)
    cleaned: list[str] = []
    for i, seg in enumerate(parts):
        if i % 2 == 1:  # code fence — keep verbatim
            cleaned.append(seg)
            continue
        seg = _DSML_BLOCK_RE.sub("", seg)
        seg = _XML_TOOLCALLS_RE.sub("", seg)
        seg = _XML_INVOKE_RE.sub("", seg)
        seg = _DEEPSEEK_MARK_RE.sub("", seg)
        seg = _DSML_TAG_RE.sub("", seg)  # any stray open/close DSML tags
        cleaned.append(seg)
    out = "".join(cleaned)
    return re.sub(r"\n{3,}", "\n\n", out).strip()


def _sanitize_tool_result(result: str, tool_name: str) -> str:
    """Strip base64 images, truncate, and tag web content for prompt injection defense."""
    # 1. Strip all base64 image data (screenshots, captured images).
    #    Note: when this is a browser_tab screenshot meant for vision,
    #    the loop ALSO calls _maybe_extract_image_for_vision against the
    #    raw (pre-sanitized) result, so the image still reaches the LLM
    #    via a separate image content block. Stripping it from the text
    #    here just keeps the string content terse — the picture isn't lost.
    if "data:image/" in result and ";base64," in result:
        result = _BASE64_IMAGE_RE.sub("[image data stripped — see attached image]", result)

    # 2. Truncate to per-tool limit. The full output is spilled to a temp
    #    file first so nothing is lost — the appended pointer lets the model
    #    read the rest back via read_file(offset, limit). read_file results
    #    are exempt: their source file already exists on disk, so the
    #    truncation note just points back at offset/limit.
    max_chars = _TOOL_MAX_CHARS.get(tool_name, _DEFAULT_MAX_CHARS)
    if len(result) > max_chars:
        total_chars = len(result)
        if tool_name == "read_file":
            result = result[:max_chars] + (
                f"\n[... truncated from {total_chars} chars — re-call read_file "
                f"with offset/limit to read a smaller section.]"
            )
        else:
            spill_path = spill_tool_result(result, tool_name)
            if spill_path is not None:
                total_lines = result.count("\n") + 1
                result = result[:max_chars] + build_spill_pointer(
                    spill_path, total_chars, total_lines
                )
            else:
                result = result[:max_chars] + f"\n[... truncated from {total_chars} chars]"

    # 3. Scan and wrap external content for prompt injection defense
    if tool_name in ("web_fetch", "web_extract", "web_search", "browser_tab",
                     "obsidian_search", "obsidian_read", "obsidian_list"):
        from flowly.agent.tools.content_guard import wrap_external_content
        result = wrap_external_content(result, source=tool_name)

    return result


def _sanitize_codex_projected_result(
    assistant_msg: dict[str, Any], tool_msg: dict[str, Any],
) -> dict[str, Any]:
    """Sanitize a Codex-projected tool result before it enters the transcript.

    Codex's inner item stream (exec / apply_patch outputs) is spliced into
    the live transcript as-is by ``_drain_codex_projected_pairs`` — it never
    passes through the per-tool execution path, so without this it would
    bypass ``_sanitize_tool_result`` entirely: no size cap, no spill. A long
    codex turn is mostly MADE of these messages (the codex_session envelope
    itself is a compact summary), so the bypass defeats spill where it
    matters most.

    The inner tool name is recovered from the paired assistant message and
    prefixed with ``codex_`` so spill filenames identify their origin
    (e.g. ``codex_exec-…txt``).
    """
    content = tool_msg.get("content")
    if not isinstance(content, str):
        return tool_msg

    inner_name = "codex_session"
    try:
        calls = assistant_msg.get("tool_calls") or []
        first = calls[0] if calls else None
        fn = None
        if isinstance(first, dict):
            fn = (first.get("function") or {}).get("name")
        if isinstance(fn, str) and fn:
            inner_name = fn if fn.startswith("codex_") else f"codex_{fn}"
    except Exception:
        pass

    sanitized = _sanitize_tool_result(content, inner_name)
    if sanitized is content:
        return tool_msg
    return {**tool_msg, "content": sanitized}


def _relabel_codex_projected_pair(
    assistant_msg: dict[str, Any], tool_msg: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Rename projected Codex tool calls ``exec`` → ``codex_exec`` etc.

    Projected pairs used to enter the transcript under Codex's raw inner
    tool names — indistinguishable from the agent's OWN exec calls. Asked
    "what tools did codex use?", the model saw 70+ plain ``exec`` entries it
    couldn't attribute and answered "codex's tool usage isn't visible to
    me". The ``codex_`` prefix makes attribution explicit for the model, the
    desktop/iOS tool views, and matches the spill filename convention.

    Copies are shallow and defensive: malformed shapes are returned as-is
    (the pair is still spliced — attribution is best-effort, never a reason
    to drop Codex's work from the transcript).
    """
    try:
        calls = assistant_msg.get("tool_calls")
        if isinstance(calls, list) and calls:
            new_calls = []
            for call in calls:
                fn = (call.get("function") or {}) if isinstance(call, dict) else {}
                name = fn.get("name")
                if isinstance(name, str) and name and not name.startswith("codex_"):
                    new_calls.append(
                        {**call, "function": {**fn, "name": f"codex_{name}"}}
                    )
                else:
                    new_calls.append(call)
            assistant_msg = {**assistant_msg, "tool_calls": new_calls}

        name = tool_msg.get("name")
        if isinstance(name, str) and name and not name.startswith("codex_"):
            tool_msg = {**tool_msg, "name": f"codex_{name}"}
    except Exception:
        pass
    return assistant_msg, tool_msg


# Marker for internal corrective nudges injected mid-turn (retry the tool,
# stop drifting, this-is-an-action-request). These ``role:user`` messages
# steer the model during the turn but must never be persisted — otherwise
# they surface in the transcript as if the user typed them. Underscore-
# prefixed so the provider layer strips it before the API call.
_EPHEMERAL_NUDGE = "_ephemeral_nudge"


def _drop_ephemeral_nudges(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Filter internal corrective nudges out of a turn's messages before
    persistence. The nudges did their job steering the model live; the real
    tool calls, results, and final reply remain and are what the next turn
    (and the user) should see."""
    if not any(m.get(_EPHEMERAL_NUDGE) for m in messages):
        return messages
    return [m for m in messages if not m.get(_EPHEMERAL_NUDGE)]


def _strip_old_tool_results(
    messages: list[dict[str, Any]],
    keep_last: int = 3,
    max_old_chars: int = 200,
    keep_last_images: int = 1,
) -> list[dict[str, Any]]:
    """Truncate old tool results, keeping only the last N in full.

    Two separate retention windows:
      - keep_last: how many recent text-only tool results to keep in
        full. Older ones get truncated to max_old_chars.
      - keep_last_images: how many recent image-bearing tool results
        (browser_tab screenshots) to keep visually. Older ones drop
        the image content block entirely; the text part stays. One
        screenshot is ~10-15K tokens, so keeping more than 1-2 in
        context blows past the model's window in a few turns.

    Returns a NEW list to avoid mutating session-shared message dicts (Fix #11).
    """
    tool_indices = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
    if not tool_indices:
        return messages

    # Identify which tool messages carry an image block. For these we
    # keep only the most recent N visually; older ones lose the image.
    image_indices = [
        i for i in tool_indices
        if isinstance(messages[i].get("content"), list)
        and any(
            isinstance(p, dict) and p.get("type") == "image_url"
            for p in messages[i].get("content", [])
        )
    ]
    image_to_strip = set(image_indices[:-keep_last_images]) if len(image_indices) > keep_last_images else set()

    # Text-truncate window (existing behavior).
    to_truncate = set(tool_indices[:-keep_last]) if len(tool_indices) > keep_last else set()

    if not to_truncate and not image_to_strip:
        return messages

    result = list(messages)
    for i in to_truncate | image_to_strip:
        msg = result[i]
        content = msg.get("content", "")

        if i in image_to_strip and isinstance(content, list):
            # Drop image blocks; keep text. If a text block is also old
            # enough to truncate, do that to its text in the same pass.
            new_content_parts: list[dict[str, Any]] = []
            for p in content:
                if isinstance(p, dict) and p.get("type") == "image_url":
                    continue  # drop the image
                if isinstance(p, dict) and p.get("type") == "text":
                    txt = p.get("text", "")
                    if i in to_truncate and len(txt) > max_old_chars:
                        txt = txt[:max_old_chars] + "\n[earlier result truncated, image dropped]"
                    new_content_parts.append({"type": "text", "text": txt})
                else:
                    new_content_parts.append(p)
            # If only one text block left, collapse to plain string for
            # providers that prefer string content on tool messages.
            if len(new_content_parts) == 1 and new_content_parts[0].get("type") == "text":
                result[i] = {**msg, "content": new_content_parts[0]["text"]}
            else:
                result[i] = {**msg, "content": new_content_parts}
        elif i in to_truncate and isinstance(content, str) and len(content) > max_old_chars:
            result[i] = {**msg, "content": content[:max_old_chars] + "\n[earlier result truncated]"}

    return result


# Field names whose values are likely user-private URLs (CDN links to
# uploaded media, signed S3 keys, etc.). When we log tool dispatches we
# substitute placeholders so a server-log dump or Sentry breadcrumb
# doesn't leak the unguessable share-by-URL link straight to whoever
# can read the logs. The actual tool still receives the real value.
_REDACT_LOG_FIELDS = frozenset({
    "video_url", "image_url", "audio_url", "file_url",
    "cdnUrl", "cdn_url",
    "s3Key", "s3_key",
})


def _redact_log_args(args: Any) -> Any:
    """Recursively redact sensitive URL / key fields for log output.

    Mutating the agent's actual call arguments would break tool execution;
    we return a sanitized copy used only by the ``logger.info`` call site.
    Strings under matching field names get replaced with ``<redacted>``;
    nested dicts and lists are walked. Everything else passes through.
    """
    if isinstance(args, dict):
        out: dict[str, Any] = {}
        for key, value in args.items():
            if key in _REDACT_LOG_FIELDS and isinstance(value, str) and value:
                out[key] = "<redacted>"
            else:
                out[key] = _redact_log_args(value)
        return out
    if isinstance(args, list):
        return [_redact_log_args(item) for item in args]
    return args


class AgentLoop:
    """
    The agent loop is the core processing engine.

    It:
    1. Receives messages from the bus
    2. Builds context with history, memory, skills
    3. Calls the LLM
    4. Executes tool calls
    5. Sends responses back
    """
    
    def __init__(
        self,
        bus: MessageBus,
        provider: LLMProvider,
        workspace: Path,
        model: str | None = None,
        action_temperature: float = 0.1,
        action_tool_retries: int = 2,
        max_iterations: int = 100,
        soft_warn_at_iteration: int = 50,
        brave_api_key: str | None = None,
        cron_service: CronService | None = None,
        context_messages: int = 100,
        compaction_config: CompactionConfig | None = None,
        exec_config: ExecConfig | None = None,
        trello_config: TrelloConfig | None = None,
        voice_config: VoiceBridgeConfig | None = None,
        x_config: XConfig | None = None,
        persona: str = "default",
        memory_search_config: MemorySearchConfig | None = None,
        state_dir: Path | None = None,
        main_config: Any | None = None,
    ):
        self.bus = bus
        self.provider = provider
        self.workspace = workspace
        self.model = model or provider.get_default_model()
        from flowly.compaction.estimator import set_active_model
        set_active_model(self.model)
        self.action_temperature = action_temperature
        self.action_tool_retries = max(0, action_tool_retries)
        self.max_iterations = max_iterations
        # Inject a one-shot self-evaluation nudge at this iteration. Helps
        # the model decide between "keep going" and "stop and ask the user"
        # without us hard-stopping it. 0 disables.
        self.soft_warn_at_iteration = max(0, soft_warn_at_iteration)
        self.brave_api_key = brave_api_key
        self.cron_service = cron_service
        self.context_messages = context_messages

        self.context = ContextBuilder(workspace, persona=persona)
        self.sessions = SessionManager(workspace)
        from flowly.agent.hooks import HookRegistry
        self.hooks = HookRegistry()
        self.tools = ToolRegistry(hooks=self.hooks)
        # Gateway reference for turn-level lifecycle broadcasts. Wired by
        # set_gateway_server() once the gateway is constructed.
        self._gateway_server = None

        # Live tool activity callback for streaming UIs (TUI / desktop).
        # Signature: (event_name, payload_dict) -> Awaitable[None] | None.
        # event_name is "tool.start" or "tool.complete". Wired by gateway.
        self.tool_callback: Callable[[str, dict], Any] | None = None

        # ─── Codex app-server runtime (opt-in) ─────────────────────────
        # Warm Codex subprocess sessions, keyed by Flowly session_key.
        # Populated lazily by the codex_session tool on first use; the
        # subprocess stays alive across Flowly turns so multi-turn Codex
        # threads don't pay the ~1s respawn cost. Closed when the tool
        # retires a session (wedge / OAuth refresh / action='new') or on
        # loop shutdown.
        self._codex_sessions: dict[str, Any] = {}
        # The session_key the codex_session tool consults when the main
        # agent invokes it mid-turn. Set in _process_message_inner before
        # tool dispatch; cleared after.
        self._codex_active_session_key: str = ""
        # Per-turn streaming callback the codex_session tool surfaces
        # Codex stream deltas through. Same lifecycle as the key above.
        self._codex_stream_callback: Any = None

        # Exec config (must be set before SubagentManager)
        self.exec_config = exec_config or ExecConfig()

        # State dir (must be set before SubagentManager)
        self._state_dir = state_dir or (workspace / ".flowly_state")

        from flowly.agent.subagent_registry import SubagentRegistry
        self._subagent_registry = SubagentRegistry()
        self.subagents = SubagentManager(
            provider=provider,
            workspace=workspace,
            bus=bus,
            model=self.model,
            brave_api_key=brave_api_key,
            exec_config=self.exec_config,
            registry=self._subagent_registry,
            state_dir=self._state_dir,
        )

        # Compaction service
        self.compaction = CompactionService(
            provider=provider,
            model=self.model,
            config=compaction_config,
        )

        # Trello config
        self.trello_config = trello_config

        # X config
        self.x_config = x_config

        # Voice config
        self.voice_config = voice_config
        self._live_call_default_allow_tools = {"voice_call", "message", "screenshot", "system"}
        configured_allow = []
        if self.voice_config and self.voice_config.live_call and self.voice_config.live_call.allow_tools:
            configured_allow = [tool.strip() for tool in self.voice_config.live_call.allow_tools if tool]
        self._live_call_allow_tools = set(configured_allow) or set(self._live_call_default_allow_tools)
        self._live_call_strict_tool_sandbox = bool(
            self.voice_config and self.voice_config.live_call.strict_tool_sandbox
        ) if self.voice_config else True

        # Memory search config
        self._memory_search_config = memory_search_config or MemorySearchConfig()
        self._main_config = main_config
        self._memory_manager: Any | None = None  # lazy-initialized

        # Session search index (FTS5)
        self._session_indexer: Any | None = None
        try:
            from flowly.session.indexer import SessionIndexer
            self._session_indexer = SessionIndexer()
            self.sessions._indexer = self._session_indexer
            # Rebuild index from existing sessions on first run
            self._session_indexer.rebuild_from_sessions_dir(self.sessions.sessions_dir)
        except Exception as e:
            logger.warning("Session indexer init failed (search disabled): {}", e)

        self._running = False
        self._on_compaction: Callable | None = None  # set by CLI after creation
        # Fired (session_key, title) when a session is auto-titled. The CLI
        # wires this to the web channel so the relay can encrypt + persist the
        # title onto the conversation doc — the relay owns title encryption, so
        # this push is the only way a good title reaches an encrypted relay
        # conversation. Gateway sessions ignore it (they read sessions.list).
        self._on_session_titled: Callable | None = None  # set by CLI after creation

        # ─── Activity tracker ──────────────────────────────────────────
        # Heartbeat for long-running work. Refreshed at every blocking
        # boundary (API stream chunk, tool call start/end, backoff sleep).
        # Cron's inactivity poller reads `get_activity_summary()` every
        # 5s and kills the run only if `seconds_since_activity` exceeds
        # the configured limit — so a legitimately busy 20-minute
        # research job never gets guillotined by a wall-clock timer.
        # Refreshes the parent session's idle-timeout clock.
        import time as _time
        self._last_activity_ts: float = _time.time()
        self._last_activity_desc: str = "initialized"
        self._current_tool: str | None = None
        self._api_call_count: int = 0
        # Cooperative interrupt flag — the cron poller sets this via
        # `agent.interrupt(reason)`; tool and stream loops check it at
        # their next blocking boundary and exit cleanly.
        self._interrupt_requested: bool = False
        self._interrupt_reason: str | None = None

        # Session keys we've fired ``on_session_start`` for. First time
        # a session_key is seen by ``_process_message`` we fire start;
        # ``on_session_end`` fires after every turn.
        self._started_sessions: set[str] = set()
        # Strong refs to in-flight auto-title tasks. asyncio only holds a weak
        # reference to a bare create_task() result, so without this the title
        # coroutine can be garbage-collected mid-flight and silently never
        # finish — exactly the "no title on the server" failure mode.
        self._title_tasks: set[asyncio.Task] = set()

        # ─── Per-run cooperative abort ─────────────────────────────────
        # The desktop / iOS Stop button drives this. The web channel's
        # ``chat.abort`` RPC used to call ``task.cancel()`` on the task
        # that pushed the inbound to the bus — but that task is done
        # in microseconds (it only awaits ``bus.publish_inbound``), so
        # the cancel was a no-op. The actual LLM call runs inside
        # ``agent.run()``'s long-lived task, which can't be cancelled
        # per-message without a refactor.
        #
        # Instead we track aborted run_ids in this set; the streaming
        # loop checks it between every chunk and breaks out, preserving
        # the partial accumulated text so the user still sees what the
        # bot had said up to the abort point. ``MAX_ABORTED_RUNS`` is
        # a defensive cap with LRU eviction — the set never holds more
        # than a session's worth of recent abort markers, but a leak
        # would otherwise grow unbounded on a long-lived gateway.
        self._aborted_runs: set[str] = set()
        self._aborted_runs_order: list[str] = []
        self._MAX_ABORTED_RUNS = 64

        # Self-improvement nudge intervals (0 = disabled). Skill review is
        # intentionally not triggered by self-review; skill creation remains
        # a main-agent/user-requested action.
        self._memory_nudge_interval = 10
        if self._main_config and hasattr(self._main_config, "agents"):
            defs = self._main_config.agents.defaults
            self._memory_nudge_interval = getattr(defs, "memory_nudge_interval", 10)
            # Frozen-memory prefix-cache opt (default OFF). Independent of the
            # governance feature — MEMORY.md is injected regardless.
            md = getattr(defs, "memory_dreaming", None)
            if md is not None:
                self.context.set_freeze_injected_memory(
                    bool(getattr(md, "freeze_injected_memory", False))
                )

        self._memory_gov = None
        self._consolidate_tool = None
        self._auto_consolidate = False
        self._consolidate_lock = None
        # Cross-session dreamer (constructed lazily in _start_dreamer_timers,
        # which needs the running loop for the extractor's sync→async bridge).
        self._dreamer = None
        self._dreamer_lock = None
        self._dreamer_enabled = False
        self._dreamer_idle_minutes = 0
        self._dreamer_daily_time = ""
        self._dreamer_turn_interval = 0
        self._dreamer_max_messages = 500
        self._dreamer_auto_floor = 0.80
        self._dreamer_review_floor = 0.55
        self._dreamer_turns = 0
        # Last genuine USER turn (not heartbeat/cron) — drives the idle trigger.
        # Kept separate from _last_activity_ts so a background heartbeat at the
        # same cadence as idle_minutes can't keep resetting the idle clock. 0.0
        # until the first user message, so it never fires on a quiet bot.
        self._dreamer_last_user_ts = 0.0
        self._maybe_enable_memory_governance()

        self._skill_gov = None
        self._skill_improve_tool = None
        self._skill_lock = None
        self._mine_every_minutes = 0
        self._curate_every_minutes = 0
        self._maybe_enable_skill_improvement()

        self._register_default_tools()

    def _maybe_enable_memory_governance(self) -> None:
        """Wire memory_append/knowledge_graph writes into the governance layer.

        Off by default. When ``agents.defaults.memory_dreaming.enabled`` is true,
        a post_tool_call hook records each memory_append / knowledge_graph add as
        a governed item and regenerates MEMORY.md's generated block. This is the
        live, chat-visible path on top of the offline dreamer/CLI surfaces.
        """
        enabled = False
        if self._main_config and hasattr(self._main_config, "agents"):
            md = getattr(self._main_config.agents.defaults, "memory_dreaming", None)
            enabled = bool(getattr(md, "enabled", False)) if md else False
        if not enabled:
            return
        try:
            from flowly.agent.memory import MemoryStore
            from flowly.memory.governance import GovernanceStore
            from flowly.memory.coordinator import MemoryGovernance
            from flowly.memory.kg_mirror import SqliteKGMirror

            kg_path = self._state_dir / "knowledge_graph.sqlite3"

            def _kg_summary() -> str:
                try:
                    if kg_path.exists():
                        from flowly.memory.knowledge_graph import KnowledgeGraph
                        return KnowledgeGraph(str(kg_path)).summary(max_entities=20)
                except Exception:
                    pass
                return ""

            self._memory_gov = MemoryGovernance(
                GovernanceStore(self._state_dir / "memory_governance.sqlite3"),
                memory_store=MemoryStore(self.workspace),
                kg_mirror=SqliteKGMirror(str(kg_path)),
                kg_summary_fn=_kg_summary,
                kg_path=str(kg_path),
            )
            self.hooks.register("post_tool_call", self._governance_post_tool)
            # Also route subagent (background self-review) memory/KG writes into
            # governance — they use a separate tool registry that bypasses the
            # main hooks above.
            try:
                self.subagents.governance_post_tool = self._governance_post_tool
            except Exception:
                pass
            # Consolidation runs through the agent's own (authenticated) provider
            # — a standalone CLI LLM call hits the Flowly proxy without the
            # agent's streaming setup and times out / returns empty.
            from flowly.agent.tools.memory_consolidate import MemoryConsolidateTool
            self._consolidate_tool = MemoryConsolidateTool(
                facade=self._memory_gov, provider=self.provider,
                model=self.model, kg_summary_fn=_kg_summary,
            )
            self.tools.register(self._consolidate_tool)
            from flowly.agent.tools.memory_import import MemoryImportTool
            self.tools.register(MemoryImportTool(
                facade=self._memory_gov, provider=self.provider,
                model=self.model, workspace=self.workspace,
            ))
            # Trust feedback: recall surfaces item ids, feedback nudges confidence.
            from flowly.agent.tools.memory_recall import MemoryRecallTool
            from flowly.agent.tools.memory_feedback import MemoryFeedbackTool
            self.tools.register(MemoryRecallTool(facade=self._memory_gov))
            self.tools.register(MemoryFeedbackTool(facade=self._memory_gov))
            # Autonomous consolidation config (both turn-based + background timer).
            self._auto_consolidate = bool(getattr(md, "auto_consolidate", True))
            self._consolidate_turn_interval = int(getattr(md, "consolidate_turn_interval", 50))
            self._consolidate_every_minutes = int(getattr(md, "consolidate_every_minutes", 30))
            self._consolidate_turns = 0
            self._consolidate_lock = None  # created lazily in the running loop
            # Cross-session dreamer config — the previously-dead idle/daily/turn
            # fields become live triggers (wired in _start_dreamer_timers).
            self._dreamer_enabled = True
            self._dreamer_idle_minutes = int(getattr(md, "idle_minutes", 30))
            self._dreamer_daily_time = str(getattr(md, "daily_time", "03:30") or "")
            self._dreamer_turn_interval = int(getattr(md, "turn_interval", 10))
            self._dreamer_max_messages = int(getattr(md, "max_messages_per_run", 500))
            self._dreamer_auto_floor = float(getattr(md, "auto_floor", 0.80))
            self._dreamer_review_floor = float(getattr(md, "review_floor", 0.55))
            logger.info("[memory-gov] live governance enabled (post_tool_call hook + memory_consolidate tool)")
        except Exception as exc:
            logger.warning(f"[memory-gov] failed to enable: {exc}")
            self._memory_gov = None

    def _governance_post_tool(self, ctx: Any) -> None:
        """post_tool_call hook: mirror memory writes into the governance layer."""
        if self._memory_gov is None or not getattr(ctx, "success", True):
            return
        name = getattr(ctx, "tool_name", "")
        params = getattr(ctx, "params", {}) or {}
        session = getattr(ctx, "session_id", "") or ""
        # Autonomous/background runs (heartbeat/cron/subagent/system) save on the
        # agent's own inference, not a user statement — those writes go to review
        # instead of silently becoming active memory. Real user-channel writes
        # stay trusted (auto-active), as before.
        from flowly.memory.dreamer import is_automation_session
        auto_activate = not is_automation_session(session)
        try:
            if name == "memory_append":
                self._memory_gov.ingest_append(
                    params.get("content", ""), source_session=session,
                    auto_activate=auto_activate,
                )
            elif name == "knowledge_graph" and params.get("action") == "add":
                import re
                m = re.search(r"id:\s*(t_[^)\s]+)", getattr(ctx, "result", "") or "")
                if not m:
                    return  # add failed or no triple id → nothing to record
                self._memory_gov.ingest_kg_fact(
                    params.get("subject", ""), params.get("predicate", ""),
                    params.get("object", ""), m.group(1), source_session=session,
                    auto_activate=auto_activate,
                )
        except Exception as exc:
            logger.warning(f"[memory-gov] post_tool sync failed: {exc}")

    def _start_memory_maintenance_timer(self) -> None:
        """Launch the background consolidation timer (called once from run())."""
        if self._memory_gov is None or not self._auto_consolidate:
            return
        if self._consolidate_lock is None:
            self._consolidate_lock = asyncio.Lock()
        if self._consolidate_every_minutes > 0:
            asyncio.create_task(self._consolidation_timer())
            logger.info(
                f"[memory-gov] auto-consolidate timer every {self._consolidate_every_minutes}m"
            )

    async def _consolidation_timer(self) -> None:
        interval = max(60, self._consolidate_every_minutes * 60)
        while self._running:
            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                return
            await self._maybe_consolidate("timer")

    async def _maybe_consolidate(self, trigger: str) -> None:
        """Run a background consolidation pass if there's anything new and no
        other pass is in flight. Fire-and-forget; never blocks a user turn."""
        if self._memory_gov is None or self._consolidate_tool is None:
            return
        if not self._auto_consolidate:
            return
        if not self._memory_gov.is_dirty():
            return
        if self._consolidate_lock is None:
            self._consolidate_lock = asyncio.Lock()
        if self._consolidate_lock.locked():
            return  # a pass is already running
        async with self._consolidate_lock:
            # Re-check dirtiness inside the lock (another pass may have cleared it).
            if not self._memory_gov.is_dirty():
                return
            try:
                result = await self._consolidate_tool.execute(dry_run=False)
                self._memory_gov.clear_dirty()
                logger.info(f"[memory-gov] auto-consolidate ({trigger}): {result}")
            except Exception as exc:
                logger.warning(f"[memory-gov] auto-consolidate ({trigger}) failed: {exc}")

    # ── Cross-session dreamer (offline memory discovery) ──────────────

    def _start_dreamer_timers(self) -> None:
        """Construct the dreamer (extractor bound to the running loop) and start
        its idle + daily triggers. Called once from run(); the turn trigger lives
        in _maybe_spawn_review and the manual trigger in _maybe_run_dreamer."""
        if not self._dreamer_enabled or self._memory_gov is None:
            return
        try:
            loop = asyncio.get_running_loop()
            from flowly.memory.dreamer import (
                MemoryDreamerService,
                SessionIndexDeltaSource,
                read_user_profile,
            )
            from flowly.memory.extractor import SubagentExtractor
            from flowly.memory.kg_mirror import SqliteKGMirror

            kg_path = str(self._state_dir / "knowledge_graph.sqlite3")
            si_path = str(self._state_dir / "session_index.sqlite")
            _workspace = self.workspace
            self._dreamer = MemoryDreamerService(
                self._memory_gov.gov,
                SessionIndexDeltaSource(si_path),
                SubagentExtractor(provider=self.provider, model=self.model, loop=loop),
                auto_floor=self._dreamer_auto_floor,
                review_floor=self._dreamer_review_floor,
                calibrate=True,
                kg_mirror=SqliteKGMirror(kg_path),
                on_committed=self._memory_gov.refresh,
                profile_fn=lambda: read_user_profile(_workspace),
            )
            self._dreamer_lock = asyncio.Lock()
            if self._dreamer_idle_minutes > 0:
                asyncio.create_task(self._dreamer_idle_timer())
            if self._dreamer_daily_time:
                asyncio.create_task(self._dreamer_daily_timer())
            logger.info(
                f"[dreamer] wired — idle={self._dreamer_idle_minutes}m "
                f"daily={self._dreamer_daily_time or 'off'} turn={self._dreamer_turn_interval}"
            )
        except Exception as exc:
            logger.warning(f"[dreamer] failed to start: {exc}")
            self._dreamer = None

    async def _maybe_run_dreamer(self, trigger: str) -> None:
        """Run one cross-session dreaming pass if not already running.

        Cheap when there is no new session delta (the dreamer short-circuits on
        the watermark), and run in a worker thread so its SQLite writes and the
        bridged extractor LLM call never block a user turn. Fire-and-forget;
        the dreamer also holds its own advisory lock + watermark for crash safety.
        """
        if self._dreamer is None or self._dreamer_lock is None:
            return
        if self._dreamer_lock.locked():
            logger.debug(f"[dreamer] {trigger} trigger skipped — a pass is already running")
            return
        async with self._dreamer_lock:
            logger.info(
                f"[dreamer] ▶ {trigger} trigger — scanning recent conversations for new memories…"
            )
            try:
                res = await asyncio.to_thread(
                    self._dreamer.run, max_messages=self._dreamer_max_messages
                )
            except Exception as exc:
                logger.warning(f"[dreamer] ✗ {trigger} run failed: {exc}")
                return
            # On a processed delta the engine already logs the detailed
            # "[dreamer] processed=… super=…" line; here we make sure a watcher
            # always sees an outcome — including the cheap cases the engine stays
            # quiet on (already running, or nothing new past the watermark).
            if not res.ran:
                logger.info(f"[dreamer] ⏭ {trigger}: skipped ({res.reason})")
            elif res.candidates == 0:
                logger.info(
                    f"[dreamer] ✓ {trigger}: nothing new to learn "
                    f"(scanned {res.processed_messages} messages, watermark={res.watermark})"
                )
            else:
                logger.info(
                    f"[dreamer] ✓ {trigger}: learned from {res.processed_messages} "
                    f"messages — {res.activated} added, {res.needs_review} for review, "
                    f"{res.superseded} updated"
                )

    async def _dreamer_idle_timer(self) -> None:
        """Fire one dreaming pass after idle_minutes of inactivity — once per
        active→idle transition, not repeatedly while idle."""
        idle_s = max(60, self._dreamer_idle_minutes * 60)
        check = min(idle_s, 120)
        fired_for = 0.0
        while self._running:
            try:
                await asyncio.sleep(check)
            except asyncio.CancelledError:
                return
            la = getattr(self, "_dreamer_last_user_ts", 0.0)
            if la <= fired_for:
                continue  # no new user activity since the last idle fire
            if (time.time() - la) >= idle_s:
                fired_for = la
                await self._maybe_run_dreamer("idle")

    async def _dreamer_daily_timer(self) -> None:
        """Fire one dreaming pass each day at daily_time (HH:MM local)."""
        while self._running:
            delay = self._seconds_until_daily(self._dreamer_daily_time)
            if delay is None:
                return  # unparseable time → disable the daily trigger
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                return
            if not self._running:
                return
            await self._maybe_run_dreamer("daily")

    @staticmethod
    def _seconds_until_daily(hhmm: str, now=None):
        """Seconds from ``now`` to the next local ``HH:MM``; None if unparseable."""
        import datetime as _dt

        try:
            hh, mm = str(hhmm).strip().split(":")
            hour, minute = int(hh), int(mm)
            if not (0 <= hour < 24 and 0 <= minute < 60):
                return None
        except (ValueError, AttributeError):
            return None
        now = now or _dt.datetime.now()
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target <= now:
            target += _dt.timedelta(days=1)
        return max(1.0, (target - now).total_seconds())

    # ── Skill self-improvement (auto-apply, snapshot-guarded) ─────────

    def _maybe_enable_skill_improvement(self) -> None:
        """Wire the trajectory miner + curator (auto-apply under snapshot). Off by
        default; enabled via agents.defaults.skill_improvement.enabled."""
        cfg = None
        if self._main_config and hasattr(self._main_config, "agents"):
            cfg = getattr(self._main_config.agents.defaults, "skill_improvement", None)
        if cfg is None or not getattr(cfg, "enabled", False):
            return
        try:
            from flowly.config.loader import get_data_dir
            from flowly.profile import get_flowly_home
            from flowly.agent.skill_usage import SkillUsageStore
            from flowly.agent.skill_lifecycle import SkillLifecycle
            from flowly.agent.tools.skill_manage import SkillManageTool
            from flowly.skills.op_log import SkillOpLog
            from flowly.skills.snapshot import SkillSnapshots
            from flowly.skills.governance import SkillGovernance
            from flowly.memory.dreamer import SessionIndexDeltaSource
            from flowly.agent.tools.skill_improve import SkillImproveTool

            skills_dir = get_flowly_home() / "skills"
            usage = SkillUsageStore(skills_dir)
            self._skill_gov = SkillGovernance(
                SkillOpLog(get_data_dir() / "skill_governance.sqlite3"),
                usage, SkillManageTool(),
                SkillSnapshots(skills_dir=skills_dir, keep=int(getattr(cfg, "snapshot_keep", 10))),
                lifecycle=SkillLifecycle(usage, stale_after_days=int(getattr(cfg, "stale_after_days", 60))),
            )
            self._skill_improve_tool = SkillImproveTool(
                facade=self._skill_gov, provider=self.provider, model=self.model,
                delta_source=SessionIndexDeltaSource(str(get_data_dir() / "session_index.sqlite")),
                skills_loader=self.context.skills, usage=usage,
                min_evidence_sessions=int(getattr(cfg, "min_evidence_sessions", 2)),
                min_repeat_count=int(getattr(cfg, "min_repeat_count", 3)),
                max_messages=int(getattr(cfg, "max_messages_per_run", 1000)),
            )
            self.tools.register(self._skill_improve_tool)
            self._mine_every_minutes = int(getattr(cfg, "mine_every_minutes", 0)) if getattr(cfg, "mine_enabled", True) else 0
            self._curate_every_minutes = int(getattr(cfg, "curate_every_minutes", 0)) if getattr(cfg, "curate_enabled", True) else 0
            self.hooks.register("post_tool_call", self._skill_telemetry_post_tool)
            logger.info("[skill-gov] skill self-improvement enabled")
        except Exception as exc:
            logger.warning(f"[skill-gov] failed to enable: {exc}")
            self._skill_gov = None

    def _skill_telemetry_post_tool(self, ctx: Any) -> None:
        """Bump skill usage on skill_view; mark curate-dirty on skill_manage writes."""
        if self._skill_gov is None or not getattr(ctx, "success", True):
            return
        name = getattr(ctx, "tool_name", "")
        params = getattr(ctx, "params", {}) or {}
        try:
            if name == "skill_view":
                sn = params.get("name")
                if sn:
                    self._skill_gov.usage.bump_use(sn)
            elif name == "skill_manage" and params.get("action") in ("create", "edit", "patch", "archive"):
                self._skill_gov.mark_dirty()
        except Exception as exc:
            logger.debug(f"[skill-gov] telemetry failed: {exc}")

    def _start_skill_maintenance_timer(self) -> None:
        if self._skill_gov is None:
            return
        if self._skill_lock is None:
            self._skill_lock = asyncio.Lock()
        if self._mine_every_minutes > 0:
            asyncio.create_task(self._skill_timer("mine", self._mine_every_minutes))
        if self._curate_every_minutes > 0:
            asyncio.create_task(self._skill_timer("curate", self._curate_every_minutes))
        if self._mine_every_minutes or self._curate_every_minutes:
            logger.info(
                f"[skill-gov] timers mine={self._mine_every_minutes}m curate={self._curate_every_minutes}m"
            )

    async def _skill_timer(self, mode: str, minutes: int) -> None:
        interval = max(60, minutes * 60)
        while self._running:
            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                return
            await self._maybe_skill_pass(mode)

    async def _maybe_skill_pass(self, mode: str) -> None:
        """Background mine/curate pass; curate gated on dirty. Fire-and-forget."""
        if self._skill_gov is None or self._skill_improve_tool is None:
            return
        if mode == "curate" and not self._skill_gov.is_dirty():
            return
        if self._skill_lock is None:
            self._skill_lock = asyncio.Lock()
        if self._skill_lock.locked():
            return
        async with self._skill_lock:
            try:
                result = await self._skill_improve_tool.execute(mode=mode, dry_run=False)
                logger.info(f"[skill-gov] auto {mode}: {result}")
            except Exception as exc:
                logger.warning(f"[skill-gov] auto {mode} failed: {exc}")

    # System sessions that should never get a chat title.
    _NO_TITLE_PREFIXES = ("heartbeat:", "cron:", "system:", "memory:", "review:")

    def _set_provisional_title(self, session: Any, user_content: str) -> None:
        """Seed a placeholder title from the user's opening message.

        A brand-new chat otherwise shows a raw session-key suffix until the real
        auto-title is generated — which only happens AFTER the turn's stream
        completes. Seeding the first message here (pre-turn, persisted by the
        early save) gives clients something meaningful immediately; the flag
        ``title_provisional`` lets the post-turn auto-title replace it. No-op once
        any title exists, on non-opening turns, or for system/non-chat sessions.
        """
        try:
            if session.metadata.get("title"):
                return
            text = (user_content or "").strip()
            if not text:
                return
            key = getattr(session, "key", "") or ""
            if any(key.startswith(p) for p in self._NO_TITLE_PREFIXES):
                return
            # Opening turn only. The early save hasn't appended this message to
            # ``session.messages`` yet, so any existing user message means the
            # session isn't new — leave its (real or provisional) title alone.
            if any(m.get("role") == "user" for m in session.messages):
                return
            session.metadata["title"] = text.split("\n", 1)[0][:50]
            session.metadata["title_provisional"] = True
        except Exception:
            logger.debug("[title] provisional set failed", exc_info=True)

    def _sync_session_cwd_metadata(self, session_key: str, metadata: dict[str, Any]) -> None:
        """Two-way sync between the in-memory cwd pin and persisted metadata.

        Runs once per turn at session load. Covers two directions:

        * **Restore**: metadata recorded a cwd from a previous turn but the
          in-memory pin is empty (bot restart, channel that doesn't transport
          cwd — CLI/Telegram/autonomous run). Restore the pin from metadata
          so exec/codex tools resolve to the same dir the user picked earlier.

        * **Persist**: the in-memory pin is non-empty (a frontend just
          shipped cwd on this turn's chat.send, or we just restored from
          metadata). Copy the current pin into metadata so the canonical
          end-of-turn save snapshots it. Subsequent turns from any channel
          inherit the same dir.

        Stale entries (the recorded directory was deleted on disk) drop the
        metadata field and degrade to the workspace fallback rather than
        crash. This is best-effort — a failure here must never break the turn.
        """
        try:
            from flowly.runtime_cwd import get_session_cwd, set_session_cwd

            current_pin = get_session_cwd(session_key)
            if current_pin is None:
                stored = metadata.get("cwd")
                if isinstance(stored, str) and stored.strip():
                    try:
                        current_pin = set_session_cwd(session_key, stored)
                        logger.info(
                            "[Loop] restored session cwd pin from metadata: "
                            "key=%s cwd=%s",
                            session_key, stored,
                        )
                    except ValueError:
                        logger.warning(
                            "[Loop] stored session cwd no longer exists, dropping: "
                            "key=%s cwd=%s",
                            session_key, stored,
                        )
                        metadata.pop("cwd", None)

            if current_pin is not None:
                metadata["cwd"] = str(current_pin)
        except Exception:  # pragma: no cover — defensive
            logger.debug("[Loop] session cwd metadata sync failed", exc_info=True)

    def _maybe_autotitle_session(self, session: Any, user_content: str, final_content: str | None) -> None:
        """Fire-and-forget a session-title generation after the first exchange.

        Only fires when: no title yet, this is the first user turn, it's a
        chat-shaped (not system) session, and we have both halves of the
        exchange. Auto-titling so CLI/desktop/iOS show the same
        descriptive name instead of a random session-key suffix.
        """
        try:
            if not final_content or not user_content:
                return
            # A PROVISIONAL title (the user's first message, seeded pre-turn) must
            # NOT block real auto-titling — only a settled real title does.
            if session.metadata.get("title") and not session.metadata.get("title_provisional"):
                return
            key = getattr(session, "key", "") or ""
            if any(key.startswith(p) for p in self._NO_TITLE_PREFIXES):
                return
            user_turns = sum(1 for m in session.messages if m.get("role") == "user")
            if user_turns > 1:
                return  # only the opening exchange
            key = getattr(session, "key", "")
            logger.info(f"[title] scheduling auto-title for {key!r}")
            task = asyncio.create_task(
                self._autotitle_session(session, user_content, final_content)
            )
            # Hold a strong ref until the task settles (anti-GC), and surface
            # any failure at INFO so a server operator can see when titling
            # breaks instead of it dying silently in the background.
            self._title_tasks.add(task)

            def _on_title_done(t: asyncio.Task, _key: str = key) -> None:
                self._title_tasks.discard(t)
                exc = t.exception() if not t.cancelled() else None
                if exc is not None:
                    logger.warning(f"[title] task for {_key!r} failed: {exc!r}")

            task.add_done_callback(_on_title_done)
        except Exception:
            logger.debug("[title] schedule failed", exc_info=True)
            # titling is best-effort; never disturb the turn

    async def _autotitle_session(self, session: Any, user_content: str, final_content: str) -> None:
        from flowly.session.title import generate_title

        key = getattr(session, "key", "")
        title = await generate_title(self.provider, self.model, user_content, final_content)
        if not title:
            logger.info(f"[title] generator returned no title for {key!r} (model={self.model!r})")
            return
        # Re-check under no lock (single event loop): another turn may have set
        # a REAL title, or the user may have renamed it. A provisional title
        # (first-message placeholder) is meant to be replaced, so don't bail on it.
        if session.metadata.get("title") and not session.metadata.get("title_provisional"):
            return
        session.metadata["title"] = title
        session.metadata.pop("title_provisional", None)  # settled now
        try:
            self.sessions.save(session)
        except Exception as e:
            logger.warning(f"[title] save failed for {key!r}: {e}")
            return
        logger.info(f"[title] auto-titled {key!r}: {title!r}")

        # Push to the channel layer (relay → encrypt + persist on the
        # conversation doc). Best-effort; gateway sessions have no relay
        # mapping so the callback no-ops for them.
        if self._on_session_titled is not None:
            try:
                await self._on_session_titled(key, title)
            except Exception as e:
                logger.debug(f"[title] notify callback failed for {key!r}: {e}")

    def _should_save_trajectories(self) -> bool:
        """Check if trajectory saving is enabled in config."""
        if self._main_config and hasattr(self._main_config, "agents"):
            return bool(self._main_config.agents.defaults.save_trajectories)
        return False

    # ── Self-improvement: background review ──────────────────────────

    _MEMORY_REVIEW_PROMPT = (
        "Review the conversation, EXISTING MEMORY, and EXISTING KNOWLEDGE GRAPH below.\n\n"
        "## 1. Knowledge Graph (knowledge_graph tool) — STRUCTURED FACTS\n"
        "Extract entity facts into knowledge_graph. STRICT RULES:\n"
        "- FULL NAMES always — use the full form the user provided, not a shortened one\n"
        "- ALWAYS set subject_type (person/company/project/event)\n"
        "- ONE entity per field — never comma-separated\n"
        "- Query KG first to check if fact already exists before adding\n"
        "- Common predicates: email, phone, role, works_at, works_with, lives_in, loves, uses\n"
        "- SKIP if already in existing knowledge graph\n\n"
        "## 2. Memory (memory_append) — FREE-FORM PREFERENCES ONLY\n"
        "Save ONLY genuinely new user preferences, corrections, or environment details.\n"
        "NEVER save:\n"
        "- Anything already in existing memory (even rephrased)\n"
        "- Task outputs, analysis results, file listings\n"
        "- News, articles, external content\n"
        "- Structured facts (names, emails, companies → use knowledge_graph instead)\n"
        "- Temporary or session-specific information\n"
        "Keep entries to 1 line max.\n\n"
        "If nothing genuinely new to save in memory or KG, reply 'Nothing to save.'\n"
        "When in doubt, DON'T save. Less is more."
    )

    def _maybe_spawn_review(
        self,
        session: Any,
        executed_tools: list[str],
        msg: Any,
    ) -> None:
        """Check the memory-review nudge counter and spawn review if needed.

        Self-review is deliberately limited to memory/KG maintenance. It does
        not create or patch skills; that remains a main-agent path initiated by
        the user or by the active task context.
        """
        # Turn-based autonomous consolidation: every N turns, if memory has new
        # writes, kick off a background cleanup pass (independent of the
        # memory-review cadence above).
        if (
            self._memory_gov is not None and self._auto_consolidate
            and self._consolidate_turn_interval > 0
        ):
            self._consolidate_turns += 1
            if self._consolidate_turns >= self._consolidate_turn_interval:
                self._consolidate_turns = 0
                asyncio.create_task(self._maybe_consolidate("turn"))

        # Cross-session dreamer: coarse turn-based trigger. Only real user
        # conversation should drive it — a background heartbeat/cron/subagent
        # turn must not tick the counter (the idle trigger already gates on the
        # same channels, loop `_dreamer_last_user_ts`). Without this a 30-min
        # heartbeat alone would fire dreaming passes with no user activity.
        if (
            self._dreamer is not None and self._dreamer_turn_interval > 0
            and _is_user_activity_channel(getattr(msg, "channel", ""))
        ):
            self._dreamer_turns += 1
            if self._dreamer_turns >= self._dreamer_turn_interval:
                self._dreamer_turns = 0
                asyncio.create_task(self._maybe_run_dreamer("turn"))

        meta = session.metadata

        # Increment the persisted counter once per user turn.
        turns = meta.get("turns_since_review", 0) + 1
        meta["turns_since_review"] = turns

        should_memory = (
            self._memory_nudge_interval > 0
            and turns >= self._memory_nudge_interval
        )

        # Always persist updated counters.
        self.sessions.save(session)

        if not should_memory:
            return

        # Reset the memory counter after triggering.
        meta["turns_since_review"] = 0
        self.sessions.save(session)

        prompt = self._MEMORY_REVIEW_PROMPT

        # Summarize recent history for the review agent
        recent = session.messages[-20:]  # last 20 messages
        history_lines = []
        for m in recent:
            role = m.get("role", "?")
            content = m.get("content", "")
            if content:
                history_lines.append(f"[{role}]: {content[:500]}")
        history_summary = "\n".join(history_lines)

        # Include existing memory so review agent can avoid duplicates
        # Use full content (up to 8000 chars) — truncating causes duplicate writes
        existing_memory = ""
        try:
            memory_file = self.workspace / "memory" / "MEMORY.md"
            if memory_file.exists():
                raw = memory_file.read_text(encoding="utf-8")
                if len(raw) > 8000:
                    # Keep first and last sections (most likely to have profile + recent)
                    existing_memory = raw[:4000] + "\n\n[...middle truncated...]\n\n" + raw[-4000:]
                else:
                    existing_memory = raw
        except Exception:
            pass

        memory_section = ""
        if existing_memory:
            memory_section = f"\n\n## Existing Memory (DO NOT DUPLICATE — if a fact is here, SKIP it)\n\n{existing_memory}"

        # Include knowledge graph summary for dedup
        kg_section = ""
        try:
            from flowly.memory.knowledge_graph import KnowledgeGraph
            kg_path = self._state_dir / "knowledge_graph.sqlite3"
            if kg_path.exists():
                kg = KnowledgeGraph(str(kg_path))
                kg_summary = kg.summary(max_entities=15)
                if kg_summary:
                    kg_section = f"\n\n## Existing Knowledge Graph (query before adding — SKIP if already here)\n\n{kg_summary}"
        except Exception:
            pass

        full_task = f"## Conversation to review\n\n{history_summary}{memory_section}{kg_section}\n\n---\n\n{prompt}"

        # Fire-and-forget background review via subagent.
        # silent=True: this is internal bookkeeping (memory/KG
        # writes) whose output is not for the user. Announcing it would
        # push a system message to the parent session and wake the
        # parent LLM for a paraphrase turn — wasted tokens and the
        # "review done" message often confused the model into
        # re-explaining its previous answer.
        asyncio.create_task(
            self.subagents.spawn(
                task=full_task,
                label="self-review",
                origin_channel=msg.channel,
                origin_chat_id=msg.chat_id,
                timeout_seconds=120,
                cleanup="delete",
                silent=True,
            )
        )
        logger.info("Background memory review spawned")

    def sync_xai_search_tool(self) -> bool:
        """Register or unregister ``x_search`` to match current xAI creds.

        The xAI ``x_search`` tool needs either an ``XAI_API_KEY`` or a stored
        OAuth token. This runs at startup and again after a provider
        hot-reload, so signing in to (or out of) xAI Grok makes the tool
        appear/disappear without a gateway restart. Returns whether
        ``x_search`` is registered afterwards.
        """
        if not self._main_config:
            return self.tools.has("x_search")
        try:
            xai_api_key = (
                getattr(getattr(self._main_config.providers, "xai", None), "api_key", "")
                or os.getenv("XAI_API_KEY", "")
            )
            from flowly.auth.xai_oauth import load_token_payload
            available = bool(load_token_payload() is not None or xai_api_key)
        except Exception:
            available = False
        if available and not self.tools.has("x_search"):
            from flowly.agent.tools.x_search import XSearchTool
            self.tools.register(XSearchTool(config=self._main_config))
        elif not available and self.tools.has("x_search"):
            self.tools.unregister("x_search")
        return self.tools.has("x_search")

    def _register_default_tools(self) -> None:
        """Register the default set of tools."""
        # File tools (sandboxed to workspace + ~/.flowly)
        self.tools.register(ReadFileTool(workspace=self.workspace))
        self.tools.register(WriteFileTool(workspace=self.workspace))
        self.tools.register(EditFileTool(workspace=self.workspace))
        self.tools.register(ListDirTool(workspace=self.workspace))
        self.tools.register(MemoryAppendTool(workspace=self.workspace))

        # Skill tools
        from flowly.agent.tools.skill_manage import SkillManageTool
        self.tools.register(SkillManageTool(workspace=self.workspace))
        from flowly.agent.tools.skill_view import SkillViewTool
        self.tools.register(SkillViewTool(workspace=self.workspace))

        # Shell tool (secure) — with centralized approval manager
        from flowly.agent.tools.shell import SecureExecTool
        from flowly.exec.approval_manager import get_approval_manager
        approval_mgr = get_approval_manager()

        async def _exec_approval_callback(pending: Any) -> Any:
            """Route approval request through the centralized manager."""
            return await approval_mgr.request_and_wait(pending)

        self.tools.register(SecureExecTool(
            config=self.exec_config,
            working_dir=str(self.workspace),
            approval_callback=_exec_approval_callback,
            main_config=self._main_config,
        ))

        # Google Workspace tools (Gmail API — only act when user asks)
        if self._main_config and hasattr(self._main_config, 'channels'):
            email_cfg = getattr(self._main_config.channels, 'email', None)
            if email_cfg and email_cfg.enabled:
                from flowly.agent.tools.email import EmailTool
                from flowly.agent.tools.google_calendar import GoogleCalendarTool
                from flowly.agent.tools.google_drive import GoogleDriveTool
                from flowly.agent.tools.google_contacts import GoogleContactsTool
                from flowly.agent.tools.google_tasks import GoogleTasksTool
                self.tools.register(EmailTool())
                self.tools.register(GoogleCalendarTool())
                self.tools.register(GoogleDriveTool())
                self.tools.register(GoogleContactsTool())
                self.tools.register(GoogleTasksTool())
        
        # Web tools — direct Brave key OR centralized proxy via web app
        web_proxy_url = None
        web_server_id = None
        web_auth_token = None
        if self._main_config and hasattr(self._main_config, 'channels'):
            web_cfg = getattr(self._main_config.channels, 'web', None)
            if web_cfg and web_cfg.server_id and web_cfg.auth_token:
                web_proxy_url = self._main_config.tools.web.search.proxy_url
                web_server_id = web_cfg.server_id
                web_auth_token = web_cfg.auth_token
        self.tools.register(WebSearchTool(
            api_key=self.brave_api_key,
            proxy_url=web_proxy_url,
            server_id=web_server_id,
            auth_token=web_auth_token,
        ))
        self.tools.register(WebFetchTool())
        self.tools.register(WebExtractTool())

        # Share web search proxy config with SubagentManager
        self.subagents._web_search_proxy_url = web_proxy_url
        self.subagents._web_search_server_id = web_server_id
        self.subagents._web_search_auth_token = web_auth_token
        
        # Message tool
        message_tool = MessageTool(send_callback=self.bus.publish_outbound)
        self.tools.register(message_tool)

        # Screenshot tool
        self.tools.register(ScreenshotTool())

        # Clarify tool — agent-initiated disambiguation. Pauses the turn
        # on an asyncio.Future until a surface (desktop/TUI/channel) sends
        # back an answer; see flowly.clarify.
        from flowly.agent.tools.clarify import ClarifyTool
        self.tools.register(ClarifyTool())

        # Video analysis tool — routes attached videos through the
        # OpenRouter ``video_url`` content block to a video-capable
        # model (Gemini). Provider is the same one the main agent
        # uses; the tool overrides ``model`` per-call so the request
        # lands on a video-understanding SKU regardless of the
        # primary chat model.
        from flowly.agent.tools.video_analyze import VideoAnalyzeTool
        self.tools.register(VideoAnalyzeTool(provider=self.provider))

        # Spawn tool (for subagents)
        spawn_tool = SpawnTool(manager=self.subagents)
        self.tools.register(spawn_tool)

        # Built-in agent tool (internal specialist agents)
        # P2.8 — the `builtin_agent` tool is now registry-backed: its
        # enum and description are derived from AssistantRegistry at
        # schema time, so dropping a new ~/.flowly/assistants/{name}.md
        # exposes a new specialist without a code change. Tool name and
        # parameter shape are unchanged vs the pre-P2.8 API so LLM
        # trajectories keep working.
        from flowly.agent.assistants import AssistantRegistry
        from flowly.agent.tools.builtin_agent import BuiltinAgentTool
        from flowly.profile import get_flowly_home
        self._assistant_registry = AssistantRegistry(
            get_flowly_home() / "assistants"
        )
        builtin_agent_tool = BuiltinAgentTool(
            manager=self.subagents, registry=self._assistant_registry,
        )
        self.tools.register(builtin_agent_tool)

        # Sessions list tool (with cancel capability)
        from flowly.agent.tools.sessions_list import SessionsListTool
        self.tools.register(SessionsListTool(
            registry=self._subagent_registry,
            manager=self.subagents,
        ))

        # Session search tool (FTS5 over past conversations)
        if self._session_indexer:
            from flowly.agent.tools.session_search import SessionSearchTool
            self.tools.register(SessionSearchTool(indexer=self._session_indexer))

        # Cron tool (for scheduling)
        cron_tool = CronTool(cron_service=self.cron_service)
        self.tools.register(cron_tool)

        # Trello tool (if configured)
        if self.trello_config and self.trello_config.api_key and self.trello_config.token:
            self.tools.register(TrelloTool(
                api_key=self.trello_config.api_key,
                token=self.trello_config.token,
            ))

        # X (Twitter) tool (if configured)
        if self.x_config and (self.x_config.bearer_token or self.x_config.api_key):
            from flowly.agent.tools.x import XTool
            self.tools.register(XTool(
                bearer_token=self.x_config.bearer_token,
                api_key=self.x_config.api_key,
                api_secret=self.x_config.api_secret,
                access_token=self.x_config.access_token,
                access_token_secret=self.x_config.access_token_secret,
            ))

        # Grok-backed X Search (xAI OAuth subscription first, API key fallback).
        # Extracted to a method so a provider hot-reload right after
        # `xai login` can register it without a gateway restart.
        self.sync_xai_search_tool()

        # Linear tool (if configured)
        if self._main_config and hasattr(self._main_config, 'integrations'):
            linear_cfg = getattr(self._main_config.integrations, 'linear', None)
            if linear_cfg and linear_cfg.api_key:
                from flowly.agent.tools.linear import LinearTool
                self.tools.register(LinearTool(api_key=linear_cfg.api_key))

        # GitHub tool (if a token is configured).
        if self._main_config and hasattr(self._main_config, 'integrations'):
            gh_cfg = getattr(self._main_config.integrations, 'github', None)
            if gh_cfg and gh_cfg.token:
                from flowly.agent.tools.github import GitHubTool
                self.tools.register(GitHubTool(
                    token=gh_cfg.token,
                    default_repo=getattr(gh_cfg, 'default_repo', ''),
                ))

        # Sentry tool (if both token and org are configured).
        if self._main_config and hasattr(self._main_config, 'integrations'):
            sentry_cfg = getattr(self._main_config.integrations, 'sentry', None)
            if sentry_cfg and sentry_cfg.token and sentry_cfg.org:
                from flowly.agent.tools.sentry import SentryTool
                self.tools.register(SentryTool(
                    token=sentry_cfg.token,
                    org=sentry_cfg.org,
                    default_project=getattr(sentry_cfg, 'default_project', ''),
                ))

        # Image generation (FAL-backed, opt-in) — dual-gated on enabled + key.
        if self._main_config and hasattr(self._main_config, 'tools'):
            img_cfg = getattr(self._main_config.tools, 'image_generation', None)
            if img_cfg and img_cfg.enabled and img_cfg.api_key:
                from flowly.agent.tools.image_generate import ImageGenerateTool
                self.tools.register(ImageGenerateTool(api_key=img_cfg.api_key, model=img_cfg.model))

        # Home Assistant tools (if configured) — gated on both url AND
        # token so a half-finished setup doesn't expose tools that will
        # only ever 401. Four tools register together as a unit.
        if self._main_config and hasattr(self._main_config, 'integrations'):
            ha_cfg = getattr(self._main_config.integrations, 'home_assistant', None)
            if ha_cfg and ha_cfg.url and ha_cfg.token:
                from flowly.agent.tools.homeassistant import (
                    HAListEntitiesTool,
                    HAGetStateTool,
                    HAListServicesTool,
                    HACallServiceTool,
                )
                self.tools.register(HAListEntitiesTool(url=ha_cfg.url, token=ha_cfg.token))
                self.tools.register(HAGetStateTool(url=ha_cfg.url, token=ha_cfg.token))
                self.tools.register(HAListServicesTool(url=ha_cfg.url, token=ha_cfg.token))
                self.tools.register(HACallServiceTool(url=ha_cfg.url, token=ha_cfg.token))

        # Obsidian tools (if configured) — gated on enabled + a resolvable
        # vault. Vault path resolution is deferred to first use, so a path
        # that disappears later degrades to a clean per-call error rather
        # than blocking registration.
        if self._main_config and hasattr(self._main_config, 'integrations'):
            obs_cfg = getattr(self._main_config.integrations, 'obsidian', None)
            if obs_cfg and getattr(obs_cfg, 'enabled', False):
                try:
                    from flowly.obsidian.tools import build_obsidian_tools
                    # facade present → review-gated obsidian_ingest is included.
                    _facade = getattr(self, "_memory_gov", None)
                    for _otool in build_obsidian_tools(
                        obs_cfg, self._state_dir, facade=_facade
                    ):
                        self.tools.register(_otool)
                    # On-demand context injection: only when explicitly enabled.
                    if getattr(obs_cfg, "auto_inject", "off") == "on_demand":
                        from flowly.obsidian.inject import build_obsidian_injector
                        self.hooks.register(
                            "pre_llm_call",
                            build_obsidian_injector(obs_cfg, self._state_dir),
                        )
                except Exception as exc:  # noqa: BLE001 — never block boot on Obsidian
                    logger.warning(f"[obsidian] tool registration skipped: {exc}")

        # Docker tool (always available, will error if Docker not installed)
        self.tools.register(DockerTool())

        # System monitoring tool
        self.tools.register(SystemTool())

        # Background process tool — long-running subprocess management.
        # The registry singleton is bound to this loop's bus so completion
        # events route back as synthetic system messages on the right
        # channel/chat. Using the singleton (rather than per-loop instance)
        # so multiple AgentLoop creations during a session reuse the same
        # in-flight process tracking. Checkpoint to ~/.flowly/processes.json
        # lets surviving (start_new_session=True) subprocesses be re-probed
        # after a Flowly restart — they come back as "detached" sessions
        # without buffered history but pollable/killable by PID.
        _process_registry = _get_process_registry()
        _process_registry.bind_bus(self.bus)
        from flowly.profile import get_flowly_home as _get_flowly_home_proc
        _process_registry.bind_checkpoint(
            ProcessCheckpoint(_get_flowly_home_proc() / "processes.json")
        )
        self.tools.register(ProcessTool(registry=_process_registry))

        # Artifact tool (enabled by default)
        artifact_enabled = True
        if self._main_config and hasattr(self._main_config, "tools"):
            artifact_enabled = self._main_config.tools.artifact.enabled
        if artifact_enabled:
            from flowly.artifacts.store import get_store
            from flowly.agent.tools.artifact import ArtifactTool
            self._artifact_store = get_store(self._state_dir)
            self.tools.register(ArtifactTool(store=self._artifact_store))
        else:
            self._artifact_store = None

        # Flowlet tool — agent-generated dynamic screens (enabled by default).
        try:
            from flowly.agent.tools.flowlet import FlowletTool
            from flowly.flowlets.store import get_store as get_flowlet_store
            self._flowlet_store = get_flowlet_store(self._state_dir)
            self.tools.register(FlowletTool(store=self._flowlet_store))
        except Exception as exc:  # noqa: BLE001 — never block agent startup
            logger.debug("Flowlet tool unavailable: {}", exc)
            self._flowlet_store = None

        # Browser tab tool (if enabled — requires Chrome extension)
        browser_tab_enabled = False
        if self._main_config and hasattr(self._main_config, "tools"):
            browser_tab_enabled = self._main_config.tools.browser_tab.enabled
        if browser_tab_enabled:
            from flowly.agent.tools.browser_tab import BrowserTabTool
            # Provider is wired now so the `find` action can transparently
            # upgrade to LLM-powered semantic matching (Haiku 4.5 inner
            # call). Gateway is set later via set_gateway_server().
            # Registry lets the tool inject plan context (auto-fed
            # external memory — Manus pattern) into every result tail.
            self.tools.register(BrowserTabTool(
                gateway_server=None,
                provider=self.provider,
                registry=self.tools,
            ))

            # Planning subsystem — exposed alongside browser_tab whenever
            # browser_tab itself is enabled. Per user directive: plan tool
            # is always available for browser tasks, no adaptive gate.
            # Phase 1: data model + 4 actions (create/view/update_step/
            # complete). Phase 2 will add validator + end-turn guard.
            # Emergency disable: FLOWLY_BROWSER_PLAN_ENABLED=0.
            try:
                from flowly.agent.tools.browser_plan import BrowserPlanTool
                # Provider drives the validator (Skyvern pattern):
                # separate Haiku LLM call confirms evidence matches
                # successCriteria before update_step(done) is accepted.
                # Registry lets the tool pick up _active_session_id so
                # the agent doesn't have to thread session_id through
                # every call.
                self.tools.register(BrowserPlanTool(
                    provider=self.provider,
                    registry=self.tools,
                ))
            except Exception:
                logger.exception("[loop] BrowserPlanTool registration failed (browser_tab unaffected)")

        # Computer use tool (if enabled — explicit opt-in)
        computer_enabled = False
        if self._main_config and hasattr(self._main_config, "tools"):
            computer_enabled = self._main_config.tools.computer.enabled
        if computer_enabled:
            from flowly.agent.tools.computer import ComputerTool
            screenshot_tool = self.tools.get("screenshot")
            self.tools.register(ComputerTool(
                config=self._main_config.tools.computer,
                screenshot_tool=screenshot_tool,
            ))

        # Voice call tool (if configured)
        # Note: The voice plugin is set later via set_voice_plugin() after the plugin is created
        if self.voice_config and self.voice_config.enabled:
            self._voice_tool = VoiceCallTool()
            self.tools.register(self._voice_tool)
        else:
            self._voice_tool = None

        # Codex app-server session tool — opt-in delegator for
        # coding-heavy turns. Hands the turn off to a `codex
        # app-server` subprocess via JSON-RPC; Codex runs its
        # sandboxed shell / apply_patch tools in its own loop and
        # streams the items back to the user's chat. See flowly/codex/
        # for the transport / session / projector / approval stack.
        self._register_codex_session_tool()

        # Memory search tools (if enabled)
        if self._memory_search_config.enabled:
            self._memory_manager = self._build_memory_manager()
            if self._memory_manager is not None:
                from flowly.agent.tools.memory_search import MemorySearchTool, MemoryGetTool
                self.tools.register(MemorySearchTool(self._memory_manager))
                self.tools.register(MemoryGetTool(self._memory_manager))

        # Knowledge graph tool (always available, DB created lazily on first write)
        from flowly.agent.tools.knowledge_graph import KnowledgeGraphTool
        self.tools.register(KnowledgeGraphTool(state_dir=self._state_dir))

        # Board — cross-channel task board. Lives at a profile-aware shared
        # location (``get_flowly_home()/board.db``) so it's the same board
        # across every session/channel and respects FLOWLY_PROFILE isolation.
        try:
            from flowly.profile import get_flowly_home as _get_flowly_home_board
            from flowly.board.store import BoardStore
            from flowly.board.orchestrator import BoardOrchestrator
            from flowly.agent.tools.board import build_board_tools
            from flowly.bus.events import InboundMessage as _BoardInbound

            self._board_store = BoardStore(_get_flowly_home_board() / "board.db")

            async def _board_spawn(task, *, label=None, origin_channel="",
                                   origin_chat_id="", model=None):
                # Run a full agent turn on the card text and return its result.
                # wait=True executes inline and returns the child's final text;
                # silent=True suppresses the per-child parent announce (the
                # orchestrator sends ONE consolidated notify instead).
                result = await self.subagents.spawn(
                    task=task,
                    label=label,
                    origin_channel=origin_channel or "board",
                    origin_chat_id=origin_chat_id or "board",
                    model=model,
                    wait=True,
                    silent=True,
                )
                # Link the finished run to the card (label == card.id) so the
                # task-detail/audit view can surface its tool_trace + timing.
                # Best-effort: a missing card or registry never affects the run.
                try:
                    if label:
                        rec = self.subagents.registry.latest_by_label(label)
                        if rec is not None:
                            self._board_store.set_run_id(label, rec.run_id)
                except Exception:
                    pass
                return result

            async def _board_notify(channel: str, chat_id: str, text: str) -> None:
                # Wake the agent (relay turn) to deliver the finished result:
                # the result re-enters as a system message, the agent replies
                # to the user naturally and in context (persona, conversation).
                # Its reply then reaches the right surface — Telegram/etc via
                # their adapter, TUI/desktop via the gateway's local push (see
                # _process_system_message). Same pattern as subagent announces.
                if not channel or channel == "board":
                    return
                session_key = f"{channel}:{chat_id}"
                await self.bus.publish_inbound(_BoardInbound(
                    channel="system",
                    sender_id="board",
                    chat_id=session_key,
                    content=text,
                ))

            async def _board_on_finished(card, outcome: str) -> None:
                # APNs/FCM wake when a board card hits a terminal state, so a
                # UI-run task's result reaches the phone even when the app is
                # closed. No-op when no device registered push (e.g. relay-only
                # users without the anonymous push path). Banner-only (no chat).
                from flowly.push.board_push import notify_board_finished

                await notify_board_finished(card, outcome)

            self._board_orchestrator = BoardOrchestrator(
                self._board_store, _board_spawn,
                notify=_board_notify, on_finished=_board_on_finished, model=self.model,
            )
            # Crash recovery: reset cards left 'in_progress' by a prior run
            # whose worker is gone (in-process tasks don't survive a restart).
            try:
                self._board_store.reset_orphaned(live_run_ids=set())
            except Exception as exc:  # pragma: no cover
                logger.warning(f"[board] crash recovery skipped: {exc}")

            for _board_tool in build_board_tools(self._board_store, self._board_orchestrator):
                self.tools.register(_board_tool)
        except Exception as exc:  # pragma: no cover - never block boot on board
            self._board_store = None
            self._board_orchestrator = None
            logger.warning(f"[board] tools unavailable: {exc}")

        # MCP servers — discovered in a BACKGROUND thread so a slow or
        # unreachable server (e.g. one awaiting an OAuth login, or a dead
        # endpoint) can never delay gateway startup. Tools register into
        # self.tools as each server connects; they appear on the first turn
        # that starts after the connection completes (registry reads are
        # snapshot-safe, see ToolRegistry.get_definitions). Per-server
        # failures stay isolated inside discover_mcp_tools. Tools register
        # after built-ins so name collisions (mcp_linear_*, mcp_trello_*)
        # preserve the native tool.
        try:
            mcp_servers = getattr(self._main_config, "mcp_servers", None)
            if mcp_servers:
                import threading

                def _discover_mcp_background() -> None:
                    try:
                        from flowly.mcp import discover_mcp_tools
                        discover_mcp_tools(
                            servers=mcp_servers,
                            tool_registry=self.tools,
                        )
                    except Exception as exc:  # pragma: no cover — defensive
                        logger.warning("MCP discovery failed: {}", exc)

                threading.Thread(
                    target=_discover_mcp_background,
                    name="flowly-mcp-discovery",
                    daemon=True,
                ).start()
        except Exception as exc:  # pragma: no cover — defensive
            logger.warning("MCP discovery could not start: {}", exc)

        # Wire the finished registry to ContextBuilder so per-tool
        # guidance blocks (trello, docker, voice_call, computer,
        # browser_tab, etc.) can gate on registry membership instead of
        # always firing. Without this the conditional blocks we added
        # in context.py can't see what's actually registered and fall
        # back to "not registered" → stay silent, which is wrong.
        self.context.set_tool_registry(self.tools)

        # Plugin discovery — runs after default tools are registered so
        # plugins can override built-ins by registering with the same
        # name (last-write-wins in ToolRegistry) and so plugin-provided
        # hooks see the same registry the agent uses at runtime.
        # Failures here only disable the offending plugin; the agent
        # still boots.
        try:
            from flowly.plugins import get_plugin_manager
            self._plugin_manager = get_plugin_manager(
                tool_registry=self.tools,
                hook_registry=self.hooks,
            )
            self._plugin_manager.discover_and_load()
        except Exception as exc:  # pragma: no cover — defensive
            logger.warning("plugin discovery failed: {}", exc)
            self._plugin_manager = None

    def _register_codex_session_tool(self) -> None:
        """Register the opt-in ``codex_session`` tool when configured.

        Reads ``tools.codex_session`` from config; a no-op when the
        section is absent or ``enabled=False`` so Flowly carries zero
        dependency on the Codex CLI by default. Wires the tool to:

          * a session-metadata accessor (so the tool can persist the
            Codex thread id + reasoning-continuity blobs across turns),
          * a per-turn stream resolver (Codex deltas render inline),
          * the warm-session registry (subprocess reuse across turns),
          * Flowly's central ApprovalManager (Codex exec / apply_patch
            approval prompts arrive through the same channels as the
            exec tool's).
        """
        codex_cfg = None
        if self._main_config and hasattr(self._main_config, "tools"):
            codex_cfg = getattr(self._main_config.tools, "codex_session", None)
        if codex_cfg is None or not getattr(codex_cfg, "enabled", False):
            return

        from flowly.agent.tools.codex_session import CodexSessionTool
        from flowly.codex.session import CodexSessionConfig

        # Resolve a deterministic cwd for the (warm, long-lived) Codex
        # subprocess. Previously an empty config meant "inherit the Flowly
        # process cwd", which under a service install is the install
        # directory — not the workspace or project. Pin it explicitly.
        from flowly.runtime_cwd import resolve_runtime_cwd

        _codex_cwd = resolve_runtime_cwd(
            explicit=codex_cfg.cwd or None,
            config=self._main_config,
            workspace=self.workspace,
        )

        runtime_cfg = CodexSessionConfig(
            codex_bin=codex_cfg.codex_bin or "codex",
            codex_home=codex_cfg.codex_home or None,
            cwd=str(_codex_cwd),
            turn_timeout_s=float(codex_cfg.turn_timeout_s),
            post_tool_quiet_timeout_s=float(codex_cfg.post_tool_quiet_timeout_s),
            approval_policy=codex_cfg.approval_policy,
            sandbox=codex_cfg.sandbox,
            client_name="flowly",
        )

        # Best-effort migration: expose Flowly's curated tools to the
        # Codex subprocess via an MCP callback registered in
        # ~/.codex/config.toml. Non-fatal — a migration failure must
        # not block the (otherwise fully functional) codex_session tool.
        try:
            from flowly.codex.tool_migration import (
                _approval_to_codex,
                _sandbox_to_permission,
                migrate_flowly_tools_to_codex,
            )
            expose = getattr(codex_cfg, "expose_flowly_tools", True)
            # Boot path: write the sandbox + approval policy to
            # ~/.codex/config.toml — thread/start can't carry them, so this is
            # the ONLY place they take effect. When tools are exposed, also
            # register the flowly-tools callback + the user's MCP servers
            # (plugin discovery is OFF here — it spawns a codex subprocess; the
            # `flowly codex enable` CLI does the full discovery). Otherwise the
            # write is policy-only, so the sandbox/approval settings still apply
            # even when the runtime is kept fully isolated.
            migrate_flowly_tools_to_codex(
                codex_home=codex_cfg.codex_home or None,
                config=self._main_config,
                default_permissions=_sandbox_to_permission(codex_cfg.sandbox),
                approval_policy=_approval_to_codex(codex_cfg.approval_policy),
                discover_plugins=False,
                include_callback=expose,
            )
        except Exception:
            logger.debug("codex config migration skipped", exc_info=True)

        def _codex_session_accessor(sk: str) -> dict[str, Any]:
            return self.sessions.get_or_create(sk).metadata

        def _codex_stream_resolver(sk: str):
            if sk == self._codex_active_session_key:
                return self._codex_stream_callback
            return None

        from flowly.codex.approval_bridge import build_codex_approval_callback
        from flowly.exec.approval_manager import get_approval_manager

        approval_cb = build_codex_approval_callback(
            approval_manager=get_approval_manager(),
            session_key_getter=lambda: self._codex_active_session_key,
        )

        self.tools.register(CodexSessionTool(
            config=runtime_cfg,
            session_accessor=_codex_session_accessor,
            stream_resolver=_codex_stream_resolver,
            session_store_get=lambda sk: self._codex_sessions.get(sk),
            session_store_set=lambda sk, sess: (
                self._codex_sessions.__setitem__(sk, sess)
                if sess is not None
                else self._codex_sessions.pop(sk, None)
            ),
            active_session_key_getter=lambda: self._codex_active_session_key,
            approval_callback=approval_cb,
        ))
        logger.info(f"codex_session tool registered (sandbox={runtime_cfg.sandbox})")

    def sync_codex_session_tool(self) -> bool:
        """Register/unregister ``codex_session`` to match current config.

        Mirrors :meth:`sync_xai_search_tool`: lets the ``/codex`` slash
        command toggle the runtime on/off (or re-apply a sandbox change)
        live, without a gateway restart. Always rebuilds the tool from
        the current ``tools.codex_session`` config so a settings change
        takes effect on the next turn. Returns whether the tool is
        registered afterwards.
        """
        if self.tools.has("codex_session"):
            self.tools.unregister("codex_session")
        # _register_codex_session_tool is a no-op when disabled, and
        # builds a fresh tool (new CodexSessionConfig) when enabled.
        self._register_codex_session_tool()
        return self.tools.has("codex_session")

    async def _close_warm_codex_sessions(self, *, exclude_key: str | None = None) -> None:
        """Close + drop any warm Codex subprocesses for this loop.

        Called when the runtime is disabled or a setting that the warm
        session captured at spawn time (sandbox, cwd, approval) changes, so
        the next turn respawns Codex with the new config instead of riding
        a stale subprocess.

        ``exclude_key`` — when set, that session is DROPPED from the pool but
        NOT closed. A live policy reload can fire while that Flowly session is
        mid-turn, and ``CodexSessionTool.execute`` holds its own local
        ``CodexSession`` reference for the duration of ``run_turn`` — so
        closing the subprocess would break the in-flight turn. Dropping it
        (without close) lets that turn finish on the existing subprocess while
        the NEXT turn respawns with the new config.
        """
        sessions = getattr(self, "_codex_sessions", None)
        if not sessions:
            return
        for sk, sess in list(sessions.items()):
            if sk != exclude_key and sess is not None:
                try:
                    await sess.close()
                except Exception:
                    logger.debug("codex warm-session close failed", exc_info=True)
            sessions.pop(sk, None)

    async def reload_codex_session_config(self) -> dict[str, Any]:
        """Apply a live ``tools.codex_session`` change without a gateway restart.

        Re-reads config, drops the warm Codex subprocesses (so the next turn
        respawns with the new sandbox/approval — which
        :meth:`sync_codex_session_tool` rewrites into ``~/.codex/config.toml``
        via the migration), and re-registers the tool. A codex turn in flight
        for the currently-active Flowly session is not interrupted: that
        session is dropped from the pool but its subprocess is left running for
        the current turn (see :meth:`_close_warm_codex_sessions`).

        The gateway wires this to ``feature_rpc.set_codex_reload_callback`` so a
        ``codex.policy.set`` RPC applies live. Returns a status dict for the
        caller; raising propagates so the caller falls back to a restart.
        """
        from flowly.config.loader import load_config
        self._main_config = load_config()
        await self._close_warm_codex_sessions(
            exclude_key=self._codex_active_session_key or None,
        )
        registered = self.sync_codex_session_tool()
        ccfg = getattr(self._main_config.tools, "codex_session", None)
        return {
            "ok": True,
            "enabled": bool(getattr(ccfg, "enabled", False)),
            "registered": registered,
            "sandbox": getattr(ccfg, "sandbox", None),
            "approvalPolicy": getattr(ccfg, "approval_policy", None),
        }

    def _persist_codex_config(self) -> None:
        """Write the in-memory config back to disk after a /codex change."""
        try:
            from flowly.config.loader import save_config
            save_config(self._main_config)
        except Exception:
            logger.exception("codex config persist failed")

    def _format_codex_status(self, ccfg: Any, cli_ok: bool) -> str:
        return (
            "**Codex runtime**\n"
            f"• Status: {'🟢 on' if ccfg.enabled else '⚪ off'}\n"
            f"• Sandbox: `{ccfg.sandbox}`\n"
            f"• Working dir: `{ccfg.cwd or '(gateway launch dir)'}`\n"
            f"• Approval policy: `{ccfg.approval_policy}`\n"
            f"• Flowly tool bridge: {'on' if ccfg.expose_flowly_tools else 'off'}\n"
            f"• Codex CLI: {'✅ installed' if cli_ok else '❌ missing — run `npm i -g @openai/codex` then `codex login`'}\n"
            "\nUse: `/codex on` · `/codex off` · "
            "`/codex sandbox <read-only|workspace-write|full-access>` · "
            "`/codex cwd <path>` · `/codex tools on|off`"
        )

    async def _handle_codex_command(self, args: str) -> str:
        """Handle the ``/codex`` slash command. Returns Markdown text.

        Sub-commands:
          * (none) / ``status`` — show current state + codex CLI health
          * ``on`` / ``enable``  — enable codex_session (live), migrate the
            Flowly tool callback into ~/.codex/config.toml
          * ``off`` / ``disable`` — disable + retire warm sessions
          * ``sandbox <level>``  — read-only | workspace-write | full-access
          * ``cwd <path>``       — set codex's working directory (persistent)
          * ``tools on|off``     — toggle the Flowly tool callback
        """
        import shutil

        cfg = getattr(self, "_main_config", None)
        if cfg is None or not hasattr(cfg, "tools"):
            return "❌ Codex runtime unavailable — no config loaded."
        ccfg = cfg.tools.codex_session
        raw = (args or "").strip()  # case-preserved (paths are case-sensitive)
        sub = raw.lower()
        codex_bin = ccfg.codex_bin or "codex"

        def _cli_ok() -> bool:
            return shutil.which(codex_bin) is not None

        if sub in ("", "status"):
            return self._format_codex_status(ccfg, _cli_ok())

        if sub in ("on", "enable"):
            if not _cli_ok():
                return (
                    "❌ Can't enable — Codex CLI not found. Install with "
                    "`npm i -g @openai/codex`, then `codex login`."
                )
            ccfg.enabled = True
            self._persist_codex_config()
            self.sync_codex_session_tool()
            return "✅ Codex runtime **enabled**.\n\n" + self._format_codex_status(ccfg, True)

        if sub in ("off", "disable"):
            ccfg.enabled = False
            self._persist_codex_config()
            await self._close_warm_codex_sessions()
            self.sync_codex_session_tool()
            return "✅ Codex runtime **disabled**. (The agent will use Flowly's own tools.)"

        if sub.startswith("sandbox"):
            rest = sub.split(None, 1)
            level = rest[1].strip() if len(rest) > 1 else ""
            valid = ("read-only", "workspace-write", "full-access")
            if level not in valid:
                return f"Usage: `/codex sandbox <{' | '.join(valid)}>` (current: `{ccfg.sandbox}`)"
            ccfg.sandbox = level
            self._persist_codex_config()
            await self._close_warm_codex_sessions()  # retire old-sandbox sessions
            self.sync_codex_session_tool()
            return f"✅ Codex sandbox set to **{level}** (applies on the next Codex turn)."

        if sub in ("tools on", "tools off", "expose on", "expose off"):
            ccfg.expose_flowly_tools = sub.endswith("on")
            self._persist_codex_config()
            self.sync_codex_session_tool()
            state = "on" if ccfg.expose_flowly_tools else "off"
            return f"✅ Flowly tool bridge **{state}** for Codex turns."

        if sub == "cwd" or sub.startswith("cwd "):
            import os
            # Path from the case-preserved args (paths are case-sensitive).
            path_arg = raw[len("cwd"):].strip()
            if not path_arg:
                cur = ccfg.cwd or "(gateway launch dir)"
                return (
                    f"Codex working directory: `{cur}`\n"
                    "Set with `/codex cwd <path>` — e.g. `/codex cwd ~/flowlyai`. "
                    "With a read/write sandbox this scopes where codex works; "
                    "full-access can still reach absolute paths anywhere."
                )
            expanded = os.path.abspath(os.path.expanduser(path_arg))
            ccfg.cwd = expanded
            self._persist_codex_config()
            # A warm session captured the old cwd at spawn — retire it so the
            # next turn respawns codex in the new directory.
            await self._close_warm_codex_sessions()
            self.sync_codex_session_tool()
            warn = "" if os.path.isdir(expanded) else " ⚠️ (directory doesn't exist yet)"
            return (
                f"✅ Codex working directory set to **{expanded}**{warn} "
                "(applies on the next codex turn)."
            )

        return (
            "Unknown `/codex` sub-command. Use one of:\n"
            "`/codex` (status) · `/codex on` · `/codex off` · "
            "`/codex sandbox <level>` · `/codex cwd <path>` · `/codex tools on|off`"
        )

    def _drain_codex_projected_pairs(self) -> list[dict[str, Any]]:
        """Drain Codex's projected message queue for the active session.

        The codex_session tool pushes the full projected item stream
        (assistant tool_calls, tool results, reasoning, final text) onto
        ``session.metadata['codex_pending_messages']``. We return only
        the self-consistent ``[assistant-with-tool_calls, tool-result]``
        pairs — those are safe to splice into the live LLM transcript.
        Standalone assistant/user/reasoning messages are dropped (the
        final text already rides in the tool-result envelope, and a
        trailing assistant message would risk two consecutive assistant
        turns for strict providers). The queue is always cleared so it
        can't grow unbounded across turns.
        """
        sk = self._codex_active_session_key
        if not sk:
            return []
        try:
            metadata = self.sessions.get_or_create(sk).metadata
        except Exception:
            return []
        queue = metadata.get("codex_pending_messages")
        if not isinstance(queue, list) or not queue:
            if isinstance(queue, list):
                metadata["codex_pending_messages"] = []
            return []

        pairs: list[dict[str, Any]] = []
        i = 0
        n = len(queue)
        while i < n:
            msg = queue[i]
            is_tool_call = (
                isinstance(msg, dict)
                and msg.get("role") == "assistant"
                and msg.get("tool_calls")
            )
            nxt = queue[i + 1] if i + 1 < n else None
            is_result = isinstance(nxt, dict) and nxt.get("role") == "tool"
            if is_tool_call and is_result:
                # Attribution: rename inner tool calls to codex_<name> so the
                # model / UI can tell Codex's work from the agent's own calls.
                relabeled_call, relabeled_result = _relabel_codex_projected_pair(msg, nxt)
                pairs.append(relabeled_call)
                # Same choke point as directly-executed tools — codex inner
                # exec output can be huge and would otherwise enter the
                # transcript with no size cap and no spill.
                pairs.append(
                    _sanitize_codex_projected_result(relabeled_call, relabeled_result)
                )
                i += 2
            else:
                i += 1
        # Clear the queue regardless of how many pairs we kept.
        metadata["codex_pending_messages"] = []
        return pairs

    def _build_memory_manager(self) -> Any | None:
        """Create MemoryIndexManager from config. Returns None on import error."""
        try:
            from flowly.memory.manager import get_manager
            ms = self._memory_search_config

            # Resolve api_key from main config if not overridden.
            api_key = ms.api_key
            if not api_key and self._main_config:
                # Only seed the embedding key from the active provider when it is
                # actually an OpenAI key — embeddings work with OpenAI only. A
                # non-OpenAI active key (xAI/Grok, a Flowly proxy `flw_…` key,
                # etc.) would otherwise be mis-detected as a Gemini key by the
                # "auto" resolver and fail silently (401 → keyword-only) while
                # claiming vector search. Leaving it empty lets the resolver fall
                # back to an explicitly-configured openai/gemini key, or honestly
                # report keyword-only search.
                active = self._main_config.get_api_key() or ""
                if active.startswith("sk-"):
                    api_key = active

            state_dir = self._state_dir if self._state_dir else (self.workspace / ".flowly_state")

            return get_manager(
                workspace=self.workspace,
                state_dir=state_dir,
                config=self._main_config,
                provider=ms.provider,
                model=ms.model,
                api_key=api_key,
                api_base=ms.api_base,
                chunk_tokens=ms.chunk_tokens,
                overlap_tokens=ms.overlap_tokens,
                max_results=ms.max_results,
                min_score=ms.min_score,
                vector_weight=ms.vector_weight,
                text_weight=ms.text_weight,
            )
        except Exception as e:
            logger.warning(f"[Memory] Failed to init memory manager: {e}")
            return None

    def set_gateway_server(self, gateway_server) -> None:
        """Set the gateway server reference for browser_tab tool.

        Also retained on the loop itself so we can broadcast turn-level
        lifecycle events (active/idle) to extensions for real-time
        presence indicators that don't need the renderer to keep
        guessing from tool-call timing alone.
        """
        self._gateway_server = gateway_server
        browser_tab = self.tools.get("browser_tab")
        if browser_tab:
            browser_tab._gateway = gateway_server
            logger.info("Gateway server connected to browser_tab tool")

    async def _notify_agent_state(self, state: str) -> None:
        """Tell connected extensions the agent's turn-level state.

        States:
          - "active": LLM is reasoning or chaining tool calls. Extensions
                      should keep their per-tab presence cue visible.
          - "idle":   The turn produced its final response and no further
                      tool calls are coming. Extensions can fade indicators.

        Best-effort: silently no-ops if there's no gateway, no extension
        clients, or the gateway hasn't exposed broadcast yet. The agent
        loop itself never depends on a delivery; this is presentation.
        """
        gw = getattr(self, "_gateway_server", None)
        if not gw:
            return
        broadcast = getattr(gw, "broadcast_agent_state", None)
        if not broadcast:
            return
        try:
            await broadcast(state)
        except Exception as e:
            logger.debug(f"agent_state broadcast failed: {e}")

    def set_voice_plugin(self, voice_plugin) -> None:
        """Set the voice plugin for voice call tool integration.

        This must be called after the VoicePlugin is created to enable
        integrated voice call handling with full tool access.
        """
        if self._voice_tool:
            self._voice_tool.set_voice_plugin(voice_plugin)
            logger.info("Voice plugin connected to agent")

    async def run(self) -> None:
        """Run the agent loop, processing messages from the bus."""
        self._running = True
        logger.info("Agent loop started")
        self.subagents.resume_pending()
        self._start_memory_maintenance_timer()
        self._start_dreamer_timers()
        self._start_skill_maintenance_timer()
        # Strong refs to in-flight concurrent chat turns (web/relay). Without
        # holding them, ``asyncio.create_task`` results could be GC'd mid-run;
        # the done callback discards each when it settles. See
        # ``_spawn_concurrent_turn``.
        self._concurrent_turns: set[asyncio.Task[Any]] = set()

        while self._running:
            try:
                # Wait for next message
                first_msg = await asyncio.wait_for(
                    self.bus.consume_inbound(),
                    timeout=1.0
                )
                batch, dropped = self._coalesce_inbound_batch(first_msg)
                if dropped:
                    logger.warning(f"Inbound coalescing dropped {dropped} stale message(s)")

                for msg in batch:
                    await self._dispatch_inbound(msg)
            except asyncio.TimeoutError:
                continue

    async def _dispatch_inbound(self, msg: "InboundMessage") -> None:
        """Route one coalesced inbound message to processing.

        Relay/web chats are independent per browser session, so processing
        them inline here serialized unrelated conversations: chat B's reply
        could not start until chat A's *entire* turn (LLM + tools + stream)
        finished. Spawn each web turn as its own task so they run
        concurrently — exactly like the direct gateway, which already calls
        the same ``_process_message`` via ``process_direct`` in parallel
        tasks. Every other channel (Telegram, Discord, …) and system messages
        (subagent announces re-enter as channel="system") stay strictly
        sequential, preserving their existing ordering guarantees.
        """
        if msg.channel == "web":
            self._spawn_concurrent_turn(msg)
        else:
            await self._process_turn(msg)

    def _spawn_concurrent_turn(self, msg: "InboundMessage") -> None:
        """Process a per-session chat (web/relay) in its own task so unrelated
        conversations don't serialize behind one another."""
        task = asyncio.create_task(self._process_turn(msg))
        self._concurrent_turns.add(task)
        task.add_done_callback(self._concurrent_turns.discard)

    async def _process_turn(self, msg: "InboundMessage") -> None:
        """Process a single inbound message and publish its response.

        Wrapped in active→idle agent_state notifications so connected
        extensions can paint a per-tab presence cue for the entire turn
        (LLM thinking + tool chain + result), not just for individual tool
        round-trips. See ``_notify_agent_state`` docstring for the protocol.
        """
        await self._notify_agent_state("active")
        try:
            response = await self._process_message(msg)
            if response:
                await self.bus.publish_outbound(response)
        except Exception as e:
            logger.error(f"Error processing message: {e}")
            # Send error response
            await self.bus.publish_outbound(OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content="Sorry, I encountered an internal error. Please try again."
            ))
        finally:
            await self._notify_agent_state("idle")
            # Settle the in-flight partial now that the turn is FULLY processed.
            # The WebChannel can't do this — its chat.send task only publishes to
            # the bus and returns instantly, so finishing there dropped the entry
            # before the run started (chat.inflight then returned null for the
            # whole tool phase). Finishing here, at true run completion, keeps the
            # partial live for the entire turn so a relay client re-entering
            # mid-stream can restore the bubble + tool panel. Keyed by the same
            # session_key/run_id the WebChannel's begin() used.
            if msg.channel == "web":
                run_id = msg.metadata.get("run_id")
                if run_id:
                    try:
                        from flowly.agent import inflight
                        inflight.finish(msg.session_key, run_id)
                    except Exception:
                        logger.debug("[loop] inflight.finish failed", exc_info=True)

    def mark_aborted(self, run_id: str) -> None:
        """Mark a run_id as aborted so the streaming loop bails out.

        Called by ``WebChannel.chat.abort`` (and any future channel
        with a Stop affordance). The streaming loop checks
        ``is_run_aborted(run_id)`` between chunks; on a positive hit
        it breaks out cooperatively, keeping the partial accumulated
        text so the user sees what the bot had managed to say.

        Idempotent. The set is bounded — see ``_MAX_ABORTED_RUNS``
        for the LRU policy.
        """
        if not run_id:
            return
        if run_id in self._aborted_runs:
            return
        self._aborted_runs.add(run_id)
        self._aborted_runs_order.append(run_id)
        while len(self._aborted_runs_order) > self._MAX_ABORTED_RUNS:
            evict = self._aborted_runs_order.pop(0)
            self._aborted_runs.discard(evict)
        logger.info(f"[agent] mark_aborted run_id={run_id}")

    def is_run_aborted(self, run_id: str) -> bool:
        """Test whether a run_id has been marked aborted."""
        return bool(run_id) and run_id in self._aborted_runs

    def stop(self) -> None:
        """Stop the agent loop.

        Cascades to subagents so the gateway shutdown doesn't leave
        background tasks running (which would end up in the registry
        as ``ended_at=None`` and surface on the next boot as spurious
        "Process restarted before task completed" errors). Each
        subagent task catches CancelledError and records an
        "interrupted" outcome gracefully — see ``_run_subagent``.
        """
        self._running = False
        try:
            cancelled = self.subagents.cancel_all()
            if cancelled:
                logger.info(f"Agent loop: cancelled {cancelled} subagent(s) on stop()")
        except Exception:
            logger.exception("AgentLoop.stop: subagent cancel_all failed")

        # Tear down warm Codex subprocesses so a gateway shutdown doesn't
        # leak `codex app-server` processes. Fire-and-forget on the
        # running loop because stop() is synchronous; the per-session
        # close timeout bounds the longest delay. On a hard shutdown with
        # no running loop, the OS reaps the children when the parent exits.
        if self._codex_sessions:
            try:
                loop = asyncio.get_event_loop()
                if loop and not loop.is_closed():
                    for _sk, sess in list(self._codex_sessions.items()):
                        if sess is not None:
                            asyncio.ensure_future(sess.close())
                self._codex_sessions.clear()
            except Exception:
                logger.exception("AgentLoop.stop: codex session teardown failed")
        logger.info("Agent loop stopping")

    def set_cron_service(self, cron_service: CronService) -> None:
        """Set the cron service for the cron tool."""
        self.cron_service = cron_service
        cron_tool = self.tools.get("cron")
        if isinstance(cron_tool, CronTool):
            cron_tool.set_cron_service(cron_service)

    def _extract_action_intent_text(self, content: str) -> str:
        """
        Extract the user utterance from voice-wrapped prompts for intent detection.

        Voice prompts include additional instructions that contain action words
        (e.g. "kapat"). We only want to analyze what the user actually said.
        """
        voice_patterns = (
            r'User said:\s*"(.*?)"',
        )
        for pattern in voice_patterns:
            match = re.search(pattern, content, flags=re.DOTALL | re.IGNORECASE)
            if match:
                return match.group(1).strip().lower()
        return content.lower()

    def _is_action_turn(self, channel: str, content: str) -> bool:
        """Detect whether this turn is an action request that should execute tools strictly."""
        lowered = content.lower()
        if "voice_call(" in lowered or "cron(" in lowered:
            return True

        intent_text = self._extract_action_intent_text(content)
        action_patterns = (
            # Call / phone
            r"\bcall\b",
            r"\bdial\b",
            r"\bphone\b",
            r"\bring\b",
            r"\barasana\b",
            r"\barar\s+m[ıi]s[ıi]n\b",
            r"\baray[ıi]p\b",
            r"\barama\b",
            r"\btelefon(?:la)?\b",
            # Retry
            r"\btry\s+again\b",
            r"\bretry\b",
            r"\btekrar\s+dene\b",
            r"\btekrar\s+b[iı]\s+dene\b",
            r"\btekrar\s+bir\s+dene\b",
            r"\btekrar\s+dener\s+m[ıi]s[ıi]n\b",
            r"\btekrar\b.*\bden\w+\b",
            r"\byeniden\s+dene\b",
            r"\bbir\s+daha\s+dene\b",
            # Reminder / notification
            r"\bremind(?:er)?\b",
            r"\bnotify\b",
            r"\balert\b",
            r"\bhat[ıi]rlat\b",
            r"\bhaber\s+ver\b",
            r"\bbildir\b",
            # Schedule / cron
            r"\bschedule\b",
            r"\bplanla\b",
            r"\bcron\s+olu[şs]tur\b",
            # Send / share
            r"\bsend\b",
            r"\bshare\b",
            r"\bg[öo]nder\b",
            r"\bpayla[şs]\b",
            # Screenshot
            r"\bscreenshot\b",
            r"\bss\b",
            r"\bekran\s+g[öo]r[üu]nt[üu]s[üu]\b",
            # Generic
            r"\brun\s+tool\b",
            r"\bexecute\b",
        )
        return any(re.search(pattern, intent_text) for pattern in action_patterns)

    def _is_retry_action_followup(self, content: str) -> bool:
        """Detect short follow-up prompts that usually mean 'retry previous action'."""
        intent_text = self._extract_action_intent_text(content)
        retry_patterns = (
            r"\btry\s+again\b",
            r"\bretry\b",
            r"\bdo\s+it\s+again\b",
            r"\bone\s+more\s+time\b",
            r"\btekrar\s+dene\b",
            r"\btekrar\s+b[iı]\s+dene\b",
            r"\btekrar\s+bir\s+dene\b",
            r"\btekrar\s+dener\s+m[ıi]s[ıi]n\b",
            r"\btekrar\b.*\bden\w+\b",
            r"\byeniden\s+dene\b",
            r"\bbir\s+daha\s+dene\b",
        )
        return any(re.search(pattern, intent_text) for pattern in retry_patterns)

    def _is_cancel_action_followup(self, content: str) -> bool:
        """Detect explicit cancellation for pending actions."""
        intent_text = self._extract_action_intent_text(content)
        cancel_patterns = (
            r"\bcancel\b",
            r"\bstop\b",
            r"\bforget\s+it\b",
            r"\bnever\s*mind\b",
            r"\babort\b",
            r"\bvazge[cç]\b",
            r"\biptal\b",
            r"\bbo[sş]ver\b",
        )
        return any(re.search(pattern, intent_text) for pattern in cancel_patterns)

    def _consume_pending_action_lock(self, session: Any, content: str) -> bool:
        """
        Consume a pending-action lock set by a previous failed action turn.

        If active, force this turn into action mode unless user explicitly cancels.
        """
        pending = session.metadata.get("pending_action_lock")
        if not isinstance(pending, dict):
            return False
        if not pending.get("active"):
            return False

        remaining = int(pending.get("remaining_turns", 0) or 0)
        if remaining <= 0:
            session.metadata.pop("pending_action_lock", None)
            return False

        if self._is_cancel_action_followup(content):
            session.metadata.pop("pending_action_lock", None)
            return False

        pending["remaining_turns"] = remaining - 1
        pending["last_consumed_at"] = datetime.now().isoformat()
        session.metadata["pending_action_lock"] = pending
        return True

    def _set_pending_action_lock(self, session: Any, request_text: str) -> None:
        """Arm pending-action lock so next follow-up is forced into action mode."""
        session.metadata["pending_action_lock"] = {
            "active": True,
            "remaining_turns": 2,
            "request": request_text[:300],
            "set_at": datetime.now().isoformat(),
        }

    def _clear_pending_action_lock(self, session: Any) -> None:
        """Clear pending-action lock after successful action execution."""
        session.metadata.pop("pending_action_lock", None)

    def _should_promote_retry_to_action(
        self,
        content: str,
        history: list[dict[str, Any]],
    ) -> bool:
        """Promote retry follow-ups to action turns when recent context indicates pending action."""
        if not self._is_retry_action_followup(content):
            return False

        if not history:
            return False

        recent_messages = history[-6:]
        recent_text = " ".join(
            str(msg.get("content", "")).lower()
            for msg in recent_messages
            if isinstance(msg, dict)
        )
        retry_context_markers = (
            "tool call could not be verified",
            "tool calls failed",
            "no action was taken",
        )
        if any(marker in recent_text for marker in retry_context_markers):
            return True

        # If recent user messages were action-like, treat retry as action.
        for msg in reversed(recent_messages):
            if not isinstance(msg, dict):
                continue
            if msg.get("role") != "user":
                continue
            text = str(msg.get("content", ""))
            if text and self._is_action_turn("", text):
                return True
        return False

    def _contains_unverified_completion_claim(self, text: str) -> bool:
        """Detect response phrases that claim completion without tool evidence."""
        lowered = (text or "").lower()
        claim_patterns = (
            r"\byapt[ıi]m\b",
            r"\bg[öo]nderdim\b",
            r"\bald[ıi]m\b",
            r"\ba[cç]t[ıi]m\b",
            r"\bkapatt[ıi]m\b",
            r"\btamamlad[ıi]m\b",
            r"\bi did\b",
            r"\bi sent\b",
            r"\bi took\b",
            r"\bi opened\b",
            r"\bi closed\b",
            r"\bdone\b",
            r"\bcompleted\b",
            r"\bfinished\b",
        )
        return any(re.search(pattern, lowered) for pattern in claim_patterns)

    # Hardcoded fallback messages that should be replaced by model-generated summaries.
    _HARDCODED_FALLBACKS = frozenset({
        "Tool calls failed, no action was taken.",
        "Tool call could not be verified, no action was taken.",
        "No safe tool could be executed for the live call.",
        "Action executed.",
        "Action completed but no response could be generated.",
        "No tool was executed, no action was taken.",
    })

    def _is_hardcoded_fallback(self, content: str) -> bool:
        """Check if final_content is a hardcoded fallback rather than model output."""
        if content in self._HARDCODED_FALLBACKS:
            return True
        if content.startswith("Actions completed (") and "tools executed" in content:
            return True
        if content.startswith("✓ Action completed"):
            return True
        if content.startswith("Action completed.\n"):
            return True
        return False

    async def _request_summary_turn(
        self, messages: list[dict], tool_results: list[dict]
    ) -> str | None:
        """Ask the model to summarize what happened in natural language.

        When the loop exits with a hardcoded fallback, this gives the model
        a chance to explain what happened to the user naturally.
        """
        if tool_results:
            summary_prompt = (
                "The tool calls above have completed. "
                "Summarize what happened to the user in a natural, concise way. "
                "If there were errors, explain what went wrong clearly."
            )
        else:
            summary_prompt = (
                "The requested action could not be completed. "
                "Explain to the user what happened and suggest alternatives. "
                "Be concise and helpful."
            )
        messages_copy = list(messages)
        messages_copy.append({"role": "user", "content": summary_prompt})

        try:
            response = await self.provider.chat(
                messages=messages_copy,
                tools=[],
                model=self.model,
                temperature=0.7,
            )
            if response.content and response.content.strip():
                return response.content.strip()
        except Exception as e:
            logger.warning(f"Summary turn failed, keeping fallback: {e}")
        return None

    def _is_strict_live_call_action_intent(self, content: str) -> bool:
        """
        Detect high-confidence action intents in an active call turn.

        This avoids forcing tools for regular chat utterances.
        """
        intent_text = self._extract_action_intent_text(content)
        strict_patterns = (
            r"\barasana\b",
            r"\barar\s+m[ıi]s[ıi]n\b",
            r"\baray[ıi]p\b",
            r"\barama\b",
            r"\btelefon(?:la)?\b",
            r"\bcall\b",
            r"\bhat[ıi]rlat\b",
            r"\bremind(?:er)?\b",
            r"\bhaber\s+ver\b",
            r"\bbildir\b",
            r"\bnotify\b",
            r"\bschedule\b",
            r"\bplanla\b",
            r"\bcron\s+olu[şs]tur\b",
            r"\bg[öo]nder\b",
            r"\bsend\b",
            r"\bekran\s+g[öo]r[üu]nt[üu]s[üu]\b",
            r"\bscreenshot\b",
            r"\bkapat\b",
            r"\bhang\s*up\b",
            r"\bend\s*call\b",
        )
        return any(re.search(pattern, intent_text) for pattern in strict_patterns)

    def _is_live_call_turn(self, content: str) -> bool:
        """
        Detect active call orchestration prompts.

        In this mode, voice output is already handled by the call pipeline,
        so the model should not use `voice_call(action="speak")`.
        """
        lowered = content.lower()
        return (
            "[active phone call]" in lowered
            or "[aktif telefon görüşmesi]" in lowered
            or "[aktif telefon gorusmesi]" in lowered
            or ("call sid:" in lowered and "user said:" in lowered)
            or ("call sid:" in lowered and "kullanıcı şunu söyledi:" in lowered)
        )

    def _apply_turn_tool_policy(
        self,
        tool_defs: list[dict[str, Any]],
        live_call_turn: bool,
        builtin_agent_dispatched: bool = False,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        """Apply per-turn tool constraints for safety and predictability."""
        # After an ASYNC (background) subagent was dispatched this turn, hide
        # ALL tools. There is nothing more for the parent to DO in this turn —
        # the work is now running in the background — so the next model step
        # should be a short text acknowledgement ("started, I'll report back"),
        # after which the loop ends naturally (no tool calls → break). This is
        # what frees the parent to go idle promptly and answer each subagent's
        # result INCREMENTALLY as it finishes, instead of marathoning on its own
        # tool calls while completions pile up undelivered.
        #
        # All parallel dispatches in the SAME batch still run: the policy is
        # applied at the START of an iteration, so the dispatch batch executes
        # first and only the NEXT iteration is tool-less. Sync delegations don't
        # set this flag (their result is returned inline, no "dispatched"
        # envelope), so a synthesise-the-result turn keeps its full toolset.
        if builtin_agent_dispatched:
            hidden = [td.get("function", {}).get("name", "") for td in tool_defs]
            return [], hidden

        if not live_call_turn:
            return tool_defs, []

        blocked_tools: list[str] = []

        filtered_defs: list[dict[str, Any]] = []
        for tool_def in tool_defs:
            fn = tool_def.get("function", {})
            tool_name = str(fn.get("name", ""))

            if self._live_call_strict_tool_sandbox and tool_name not in self._live_call_allow_tools:
                blocked_tools.append(tool_name)
                continue

            if tool_name != "voice_call":
                filtered_defs.append(tool_def)
                continue

            # During active phone conversation turns, avoid self-referential
            # speak tool calls. The returned assistant text is spoken already.
            patched = copy.deepcopy(tool_def)
            action_prop = (
                patched.get("function", {})
                .get("parameters", {})
                .get("properties", {})
                .get("action")
            )
            if isinstance(action_prop, dict):
                enum_values = action_prop.get("enum")
                if isinstance(enum_values, list):
                    action_prop["enum"] = [
                        value for value in enum_values
                        if value in {"end_call", "list_calls"}
                    ]
            filtered_defs.append(patched)

        return filtered_defs, blocked_tools

    def _is_live_call_tool_allowed(self, tool_name: str, tool_args: dict[str, Any]) -> bool:
        """Final runtime guard for live-call tool execution."""
        if not self._live_call_strict_tool_sandbox:
            return True
        if tool_name not in self._live_call_allow_tools:
            return False
        if tool_name == "voice_call":
            action = str(tool_args.get("action", "")).lower()
            return action in {"end_call", "list_calls"}
        return True

    # Phrases that signal "the user wants pixels back" — when none of these
    # appear in the user's most recent message, the media-intent guard rejects
    # `computer(action="screenshot")` and `computer(action="capture_window")`
    # so the model can't reflex into "let me verify visually" after an AX read
    # already gave it the answer. Tighter than the prompt's Rule 1 because
    # this fires at tool-execution time, not at LLM-planning time.
    _MEDIA_INTENT_PHRASES: tuple[str, ...] = (
        # English
        "screenshot",
        "screen shot",
        "picture",
        "image",
        "show me",
        "send me a pic",
        "send a pic",
        "what does it look like",
        "what it looks like",
        "snapshot",
        # Turkish — substring match means "ss at" covers "ss atar mısın",
        # "ss al" covers "ss alır / alabilir / alsana", etc. Add bare
        # stems plus a few unambiguous full phrases.
        "ekran görüntüsü",
        "ekran goruntusu",
        "ekran goruntu",
        "ekran resmi",
        "ekran fotoğrafı",
        "ekran fotografi",
        "ekran al",            # ekran alır mısın, ekran al lütfen
        "ekran çek",
        "ekran cek",
        "resim",
        "görsel",
        "gorsel",
        "görüntü",             # görüntü al, görüntü gönder
        "goruntu",
        "göster",
        "goster",
        "nasıl görünüyor",
        "nasil gorunuyor",
        # "ss" variants — substring "ss at"/"ss al"/"ss çek"/"ss gönder"
        # catches conjugated forms (atar, alır, çekersin, gönderir misin).
        "ss at",
        "ss al",
        "ss çek",
        "ss cek",
        "ss gönder",
        "ss gonder",
        "ss yolla",
        "ss paylaş",
        "ss paylas",
        # Photo / foto stems
        "foto",                # foto, fotograf, fotoğraf, foto çek, foto at
        "fotoğraf",
        "fotograf",
    )

    @classmethod
    def _user_wants_media_output(cls, messages: list[dict[str, Any]]) -> bool:
        """True when the most recent user message contains a phrase that
        explicitly asks for a visual/pixel response (screenshot, picture,
        "show me X", etc.). False for task intents like "open Spotify and
        play X" or "calculate 2+2 and tell me the answer", where the right
        response is a structured AX read, not a screen capture.

        Walks the messages list from the end. We look at the LAST user
        message (the one that kicked off this turn) — earlier user turns
        may have requested media but this turn might not.
        """
        for msg in reversed(messages):
            if msg.get("role") != "user":
                continue
            content = msg.get("content")
            if isinstance(content, list):
                # Multimodal content: concatenate the text parts.
                text_parts = [
                    p.get("text", "") for p in content
                    if isinstance(p, dict) and p.get("type") == "text"
                ]
                lowered = " ".join(text_parts).lower()
            elif isinstance(content, str):
                lowered = content.lower()
            else:
                return False
            return any(phrase in lowered for phrase in cls._MEDIA_INTENT_PHRASES)
        return False

    @classmethod
    def _is_media_tool_call_blocked(
        cls,
        tool_name: str,
        tool_args: dict[str, Any],
        messages: list[dict[str, Any]],
    ) -> bool:
        """Returns True when this tool call should be blocked by the
        media-intent guard. Fires for `computer(action="screenshot")` and
        `computer(action="capture_window")` when the user's last message
        didn't ask for a picture.

        The full-screen `screenshot` tool (top-level, not the computer
        action) is also covered — same intent rule. Other media tools
        (read_window_text, read_window_state, capture_window with explicit
        media intent) pass through.
        """
        is_media = False
        if tool_name == "screenshot":
            is_media = True
        elif tool_name == "computer":
            action = str(tool_args.get("action", "")).lower()
            if action in {"screenshot", "capture_window"}:
                is_media = True
        if not is_media:
            return False
        return not cls._user_wants_media_output(messages)

    def _coalesce_inbound_batch(self, first_msg: InboundMessage) -> tuple[list[InboundMessage], int]:
        """
        Collect bursty inbound traffic without dropping user messages.

        Queue-All policy: preserve full ordering and keep every message.
        """
        batch = [first_msg]

        while True:
            try:
                batch.append(self.bus.inbound.get_nowait())
            except asyncio.QueueEmpty:
                break

        return batch, 0

    async def _inject_pre_llm_context(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        model: str,
    ) -> list[dict[str, Any]]:
        """Fire ``pre_llm_call`` and inject any returned context into the messages.

        Plugins return strings (or ``{"context": "..."}`` dicts) describing
        domain knowledge they want the model to consider this turn.  All
        returned values are joined and wrapped in ``<plugin_context>`` tags,
        then prepended to the last user message — never the system prompt,
        so the prompt cache prefix stays identical across turns.

        Returns a new ``messages`` list with the injection applied (or the
        original list unchanged when no plugins returned anything).
        """
        from flowly.agent.hooks import LLMHookContext

        # Extract the last user message text and the system prompt for the
        # hook context. This is informational only — the canonical message
        # list is what gets mutated.
        user_message_text = ""
        for m in reversed(messages):
            if m.get("role") == "user":
                content = m.get("content", "")
                user_message_text = content if isinstance(content, str) else ""
                break

        system_text = ""
        if messages and messages[0].get("role") == "system":
            content = messages[0].get("content", "")
            system_text = content if isinstance(content, str) else ""

        ctx = LLMHookContext(
            # Pull session id from the tool registry — same bind set in
            # _process_message at turn start.
            session_id=getattr(self.tools, "_active_session_id", "") or "",
            model=model,
            messages=messages,
            tools=list(tools or []),
            system=system_text,
            user_message=user_message_text,
        )

        try:
            contexts = await self.hooks.fire_pre_llm_call(ctx)
        except Exception:
            logger.debug("pre_llm_call fire failed", exc_info=True)
            return messages

        if not contexts:
            return messages

        injected = "\n\n".join(c for c in contexts if c)
        if not injected:
            return messages

        # Prepend to the last user message inside <plugin_context> tags.
        # Walk from the end so we hit the most recent user turn even if
        # there are tool/assistant messages after it.
        new_messages = list(messages)
        for i in range(len(new_messages) - 1, -1, -1):
            if new_messages[i].get("role") != "user":
                continue
            original = new_messages[i].get("content", "")
            wrapped = (
                f"<plugin_context>\n{injected}\n</plugin_context>\n\n{original}"
                if isinstance(original, str)
                else original  # multimodal user msgs (rare) — skip injection
            )
            new_messages[i] = {**new_messages[i], "content": wrapped}
            break

        return new_messages

    async def _chat_with_stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        model: str,
        temperature: float,
        tool_choice: str,
        stream_callback: Callable[[str], Awaitable[None]],
        run_id: str = "",
    ):
        """
        Call provider.chat_stream(), fire stream_callback for each text delta,
        and return a final LLMResponse (with full content + tool_calls).

        ``run_id`` lets the loop check the cooperative-abort set
        between chunks. On an abort hit we break out keeping the
        partial ``accumulated_text`` so the user sees what the bot
        managed to say up to the Stop point — partial-preservation
        on abort. The returned LLMResponse carries
        ``finish_reason="aborted"`` so the surrounding tool-loop /
        outbound builder knows to stop iterating and flag the
        outgoing message as aborted.
        """
        from flowly.providers.base import LLMResponse
        accumulated_text = ""
        final_response = None
        chunk_count = 0
        aborted = False

        async for chunk in self.provider.chat_stream(
            messages=messages,
            tools=tools,
            model=model,
            temperature=temperature,
            tool_choice=tool_choice,
        ):
            # Cooperative abort: check before processing the chunk so
            # accumulated_text reflects exactly what was rendered to
            # the user at the moment of Stop. Checking after the
            # ``stream_callback`` send would leak one extra delta
            # past the abort line — small but distracting in the UI.
            if run_id and self.is_run_aborted(run_id):
                aborted = True
                logger.info(
                    f"[stream] aborted at chunk #{chunk_count}, "
                    f"{len(accumulated_text)} chars preserved, run_id={run_id}"
                )
                break
            if chunk.content:
                chunk_count += 1
                accumulated_text += chunk.content
                # Heartbeat for the inactivity watchdog — every stream
                # chunk counts as progress. Without this a long streaming
                # reply could look "idle" to the poller even though the
                # model is actively emitting tokens.
                self._touch_activity("receiving stream response")
                logger.debug(f"[stream] chunk #{chunk_count}: {len(chunk.content)} chars")
                try:
                    await stream_callback(chunk.content)
                except Exception as e:
                    logger.warning(f"[stream] stream_callback error: {e}")
            if chunk.finish_reason:
                final_response = chunk

        logger.info(f"[stream] done: {chunk_count} chunks, {len(accumulated_text)} total chars{' (ABORTED)' if aborted else ''}")

        if aborted:
            # Build a synthetic final response that carries the
            # partial text + aborted finish_reason. We don't preserve
            # any partial tool_calls — half-built tool_call deltas
            # are unsafe to execute.
            return LLMResponse(
                content=accumulated_text or None,
                finish_reason="aborted",
                usage={},
            )

        # Preserve usage across the final-response rebuild. The earlier
        # constructions dropped ``usage`` by omission, which meant every
        # streaming turn surfaced an empty usage dict to the accumulator
        # in ``_run_llm_tool_loop``. Explicitly carrying it through lets
        # prompt_tokens / cache_read_tokens / cache_write_tokens reach
        # the outbound metadata (and, via channel event, the UI's
        # context-window indicator). ``LLMResponse.usage`` is typed as
        # a plain dict with a default factory, so we fall back to an
        # empty dict rather than None to keep downstream ``dict.get``
        # calls safe.
        final_usage = dict(final_response.usage) if final_response and final_response.usage else {}
        if final_response is None:
            final_response = LLMResponse(
                content=accumulated_text or None,
                finish_reason="stop",
                usage=final_usage,
            )
        elif final_response.tool_calls:
            # Final chunk had tool calls — content came in earlier deltas
            final_response = LLMResponse(
                content=accumulated_text or None,
                tool_calls=final_response.tool_calls,
                finish_reason=final_response.finish_reason,
                usage=final_usage,
            )
        else:
            # Pure text — use accumulated content
            final_response = LLMResponse(
                content=accumulated_text or final_response.content,
                finish_reason=final_response.finish_reason,
                usage=final_usage,
            )

        return final_response

    async def _emit_iteration_event(
        self,
        *,
        outbound_channel: str,
        outbound_chat_id: str,
        outbound_run_id: str,
        iteration_idx: int,
        message: dict[str, Any],
        on_iteration: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> None:
        """Publish a structured per-iteration event for the channel layer.

        The web channel forwards this to the relay as a
        ``state:"iteration_step"`` WS event; the relay writes it to
        the conversation's ``tool_turns/`` subcollection LIVE (with
        ``inProgress: true``) so connected desktop / iOS clients see
        the tool-turn panel populate as the loop runs, instead of
        waiting for ``state:"final"`` to dump everything at once.

        Each iteration event mirrors the on-disk Firestore doc
        shape:
            { runId, iterationIdx, role, content, tool_calls?,
              tool_call_id?, name? }

        No-op when outbound_channel / outbound_chat_id are empty —
        callers that don't have a live UI to push to (cron, internal
        flows) pass nothing and the helper costs zero. Old bots that
        don't even know about this method continue to behave exactly
        as before; the channel layer treats the absence of the
        ``iteration_event`` metadata field as the legacy
        single-write-at-end path.
        """
        role = message.get("role") or ""
        if role not in ("assistant", "tool"):
            return
        event_payload: dict[str, Any] = {
            "runId": outbound_run_id,
            "iterationIdx": iteration_idx,
            "role": role,
            "content": message.get("content") or "",
        }
        if role == "assistant" and message.get("tool_calls"):
            event_payload["tool_calls"] = message["tool_calls"]
        if role == "tool":
            if message.get("tool_call_id"):
                event_payload["tool_call_id"] = message["tool_call_id"]
            if message.get("name"):
                event_payload["name"] = message["name"]

        # Direct-callback path (the direct gateway / any ``process_direct``
        # caller): deliver the event straight to the caller's transport — e.g.
        # the gateway turns it into a ``state:"iteration_step"`` WS event — with
        # NO bus, NO "web"-only restriction, and NO Firestore. This is what
        # makes the direct-gateway chat show live tool turns exactly like the
        # relay. Best-effort: a callback failure must never break the loop.
        if on_iteration is not None:
            try:
                await on_iteration(event_payload)
            except Exception:
                logger.debug(
                    "[loop] on_iteration callback failed (non-fatal)",
                    exc_info=True,
                )
            return

        # Legacy relay path: only the web channel forwards these (the relay
        # writes them to the conversation's tool_turns/ subcollection live).
        if not outbound_channel or not outbound_chat_id:
            return
        if outbound_channel != "web":
            return
        try:
            await self.bus.publish_outbound(OutboundMessage(
                channel=outbound_channel,
                chat_id=outbound_chat_id,
                content="",
                metadata={"iteration_event": event_payload},
            ))
        except Exception:
            # Iteration events are best-effort UI signal; a publish
            # failure must NEVER break the agent loop. The user will
            # just see a slightly less-live panel.
            logger.debug(
                "[loop] iteration_event publish failed (non-fatal)",
                exc_info=True,
            )

    async def _run_llm_tool_loop(
        self,
        messages: list[dict[str, Any]],
        action_turn: bool,
        live_call_turn: bool = False,
        turn_content: str = "",
        stream_callback: Callable[[str], Awaitable[None]] | None = None,
        session_key: str = "",
        model_override: str | None = None,
        disabled_tools: list[str] | None = None,
        outbound_channel: str = "",
        outbound_chat_id: str = "",
        outbound_run_id: str = "",
        on_iteration: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
        reply_media: list[str] | None = None,
    ) -> tuple[str, list[dict[str, Any]], list[str], dict[str, Any], list[dict[str, Any]]]:
        """
        Run iterative LLM + tool execution loop until final response.

        Returns:
            (final_content, accumulated_tool_results, executed_tool_names,
             total_usage, messages)

        The returned ``messages`` is the loop's FINAL working list — callers
        must slice THIS list (not the one they passed in) when persisting the
        turn. Mid-turn transforms (``_strip_old_tool_results`` after
        iteration 5, the microcompact guard) REBIND the local ``messages`` to
        a new list; everything appended afterwards (e.g. a codex_session
        result landing on iteration 6+) only exists on the rebound list.
        Slicing the caller's original list silently dropped those messages
        from the session — the next turn's model had no record that the tool
        ever ran. All transforms preserve message count and order, so
        ``turn_start_idx`` computed against the input list stays valid.
        """
        iteration = 0
        # Turn-local collector for media a tool produces for THIS reply (image_
        # generate, screenshot). Callers pass their own list and read it after to
        # set ``OutboundMessage.media`` — so it's concurrency-safe (per-turn, not
        # on ``self``). Default to a local list when not threaded.
        if reply_media is None:
            reply_media = []
        final_content: str | None = None
        accumulated_tool_results: list[dict[str, Any]] = []
        executed_tool_names: list[str] = []
        blocked_tools: list[str] = []
        total_usage: dict[str, int] = {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            # Cache-related counters — accumulated alongside the legacy
            # three so per-session telemetry and the web proxy's
            # Firestore writes see cache hits instead of silently zero'd
            # totals. All keys are summed per-call even when providers
            # don't report them (contribution is 0, no-op).
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
        }
        tools_were_used = False
        _audit = get_audit_logger()
        _overflow_recovered = False  # allow at most one overflow recovery per turn
        _empty_response_count = 0   # detect think-only empty responses
        _current_session_key: str = session_key
        successful_tools_were_used = False
        no_tool_retry_count = 0
        forced_tool_retry = False
        strict_live_call_action = live_call_turn and self._is_strict_live_call_action_intent(turn_content)
        enforce_action_tools = action_turn and (not live_call_turn or strict_live_call_action)

        # Counter for per-iteration WS events the channel layer
        # forwards to clients so the desktop / iOS tool-turn panel
        # populates LIVE as the loop runs (instead of waiting for
        # state:"final" to write everything at once).
        #
        # Indexing aligns with the relay's ``tool_turns/`` doc id
        # scheme (``{runId}_iter_{NNN}``): we increment by 1 per
        # message we add to the loop's ``messages`` list — one for
        # each assistant_with_tool_calls entry plus one per
        # tool_result entry. This matches how the relay would have
        # numbered them when reading the final ``toolMessages``
        # array, so live writes and the (now redundant) final
        # toolMessages writes are idempotent — same doc id, same
        # content, Firestore set+merge no-ops the second hit.
        _iteration_event_idx = 0

        # Per-turn model override (used by cron jobs with a `model` field so
        # a single job can run on a different model than the gateway default).
        # Falls back to self.model when no override is supplied.
        selected_model = model_override or self.model
        selected_temperature = self.action_temperature if action_turn else 0.7
        max_turn_iterations = self.max_iterations
        if live_call_turn and not enforce_action_tools:
            max_turn_iterations = min(max_turn_iterations, 3)

        # Built-in agent dispatch tracking (enterprise pattern: tool hiding)
        _builtin_agent_dispatched = False
        # One-shot soft warning state — see soft_warn_at_iteration. We don't
        # hard-stop on iteration count; we trust the model
        # to decide when it's done via stop_reason="end_turn". The warning
        # gives the model a chance to self-evaluate when a turn drags on
        # (e.g. stuck in a tool retry loop) and either keep going or escalate
        # to the user instead of silently spinning to the runaway cap.
        _soft_warned = False
        _soft_warn_threshold = (
            self.soft_warn_at_iteration
            if self.soft_warn_at_iteration > 0
            and self.soft_warn_at_iteration < max_turn_iterations
            else 0
        )

        # End-turn guard one-shot flag — reset at the start of each
        # turn so a fresh turn can be nudged once. See the guard
        # block near the loop tail for the full rationale.
        self._plan_nudged_this_turn = False

        # NOTE: the repetition detector that lived here was removed in
        # 2026-05-02 because it produced false positives on legitimate
        # diagnostic patterns (e.g. 3 screenshots in 10 calls is normal
        # when checking page state) and the "STOP" nudge was being
        # taken too literally by Haiku — agent would freeze instead of
        # adapting. The DOM-mutation-count signal in browser_tab
        # results (commit af50376) plus the planner's evidence
        # requirement now catch real loops without the false positives.

        while iteration < max_turn_iterations:
            iteration += 1

            # Mid-turn context guard. Tool results accumulate INSIDE a turn
            # (a long codex session easily adds tens of K tokens across
            # iterations) while compaction only ran at turn START — so a turn
            # could sail past the provider's input cap and die with a 413
            # mid-flight. Before each LLM call, if the working message list is
            # within reserve distance of the EFFECTIVE window (model window,
            # clamped to the Flowly proxy's 80K input cap), microcompact it —
            # old tool results collapse to stubs, recent ones stay verbatim.
            if iteration > 1:
                _guard_tokens = estimate_messages_tokens(messages)
                _guard_limit = (
                    self.compaction.effective_context_window
                    - self.compaction.config.reserve_tokens_floor
                )
                if _guard_tokens > _guard_limit:
                    messages = self.compaction.microcompact(messages)
                    _guard_after = estimate_messages_tokens(messages)
                    logger.info(
                        f"Mid-turn microcompact: {_guard_tokens} → {_guard_after} tokens "
                        f"(limit {_guard_limit}, iteration {iteration})"
                    )

            # Soft warning — fired once when the turn has been running long
            # enough to suggest the model should reflect. Doesn't stop the
            # loop; just nudges the model in the next LLM call.
            #
            # Tone matters: the previous version said "stop now, tell the
            # user" which agents (especially Haiku) interpreted as "give
            # up." Browser-heavy workflows (Sheets sidebars, multi-step
            # forms) legitimately need 30-60 micro-actions and were being
            # cut off mid-task. New version distinguishes:
            #  - HEALTHY (mostly successful tool calls) → "you're doing
            #    fine, here's a checkpoint, keep going"
            #  - STUCK (high error rate / repetition) → "switch strategy
            #    or escalate"
            if (
                _soft_warn_threshold
                and not _soft_warned
                and iteration >= _soft_warn_threshold
            ):
                _soft_warned = True
                # Inspect the last ~10 tool results to gauge progress.
                # Stuck signals: repeated errors, identical tool calls,
                # >50% failure rate. Healthy signals: errors are rare
                # and varied, mostly success.
                recent_errors = 0
                recent_total = 0
                for m in messages[-20:]:
                    if (m.get("role") == "tool" and isinstance(m.get("content"), str)):
                        recent_total += 1
                        c = m["content"]
                        if '"error"' in c[:300] or '"_occluded"' in c[:300]:
                            recent_errors += 1
                error_rate = recent_errors / recent_total if recent_total else 0
                healthy = recent_total >= 5 and error_rate < 0.4

                logger.warning(
                    f"Soft iteration warning at {iteration}/{max_turn_iterations} "
                    f"(threshold={_soft_warn_threshold}, recent_errors={recent_errors}/"
                    f"{recent_total}, healthy={healthy}); injecting nudge"
                )

                if healthy:
                    nudge = (
                        f"Checkpoint at {iteration} tool calls — you're making steady "
                        "progress (low error rate). KEEP GOING; this nudge is just a "
                        "checkpoint, not a stop signal. Multi-step UI workflows often "
                        f"need {iteration}+ micro-actions and that's normal. Quick "
                        "self-check: is each step bringing you closer to the goal? "
                        "If yes, continue. If you've quietly drifted (e.g. clicking "
                        "the same area without state change), switch tactic."
                    )
                else:
                    nudge = (
                        f"You've made {iteration} tool calls and recent ones show "
                        f"{recent_errors} errors out of the last {recent_total}. "
                        "Step back:\n"
                        "1. Are you repeating the same approach that already failed?\n"
                        "2. If yes, switch strategy — different tool, selector, or "
                        "approach. Do NOT keep retrying what failed.\n"
                        "3. If genuinely blocked (the page won't respond, an "
                        "auth wall, missing data), tell the user clearly what you "
                        "tried, what failed, and what you need from them.\n"
                        "Don't keep spinning — either pivot or escalate."
                    )
                messages.append({"role": "user", "content": nudge, _EPHEMERAL_NUDGE: True})

            tool_defs, policy_blocked_tools = self._apply_turn_tool_policy(
                self.tools.get_definitions(),
                live_call_turn=live_call_turn,
                builtin_agent_dispatched=_builtin_agent_dispatched,
            )
            if policy_blocked_tools:
                blocked_tools.extend(policy_blocked_tools)

            # Caller-supplied hard block list — used by cron jobs to hide
            # the `cron` and `message` tools so a scheduled run can't
            # recursively schedule more jobs or DM users directly.
            if disabled_tools:
                disabled_set = set(disabled_tools)
                before = len(tool_defs)
                tool_defs = [
                    td for td in tool_defs
                    if str(td.get("function", {}).get("name", "")) not in disabled_set
                ]
                if len(tool_defs) < before:
                    blocked_tools.extend(disabled_set)
            # Always use "auto" on the first iteration so the model can output
            # a preamble sentence ("Hemen bakıyorum.") before the tool call.
            # This is critical for voice mode — the user needs audio feedback
            # while the tool executes. On subsequent iterations (iteration > 1),
            # if the model hasn't called any tools yet, escalate to "required"
            # to force tool use and prevent infinite text-only loops.
            if iteration == 1:
                tool_choice = "auto"
            else:
                tool_choice = (
                    "required"
                    if ((enforce_action_tools or forced_tool_retry) and not successful_tools_were_used)
                    else "auto"
                )
            # Mid-turn tool result compaction — prevent context bloat
            # Fix #22: Less aggressive — start at iteration 5 to preserve multi-step browser context
            if iteration > 5:
                messages = _strip_old_tool_results(messages, keep_last=3, max_old_chars=300)
            if iteration > 8:
                messages = _strip_old_tool_results(messages, keep_last=2, max_old_chars=150)

            logger.info(
                "LLM request telemetry: "
                f"model={selected_model}, tool_choice={tool_choice}, tool_count={len(tool_defs)}, "
                f"action_turn={action_turn}, live_call_turn={live_call_turn}, "
                f"blocked_tools={sorted(set(blocked_tools))}, "
                f"iteration={iteration}/{max_turn_iterations}"
            )
            self._api_call_count += 1
            self._touch_activity(f"starting LLM call #{self._api_call_count} (iter {iteration})")

            # Stopwatch for audit log — measures the full retry chain
            # (primary + any streaming/required fallbacks) as one
            # iteration. Per-attempt timing is not worth the extra
            # JSONL rows; iteration-level wall-clock is the useful
            # production signal.
            _iter_t0 = time.monotonic()

            # Fire pre_llm_call so plugins can inject domain context into
            # the user message (e.g. "user is a lawyer with N active
            # cases — prefer case_* tools"). Returned strings are wrapped
            # in <plugin_context> tags and prepended to the last user
            # message.  System prompt is left untouched so the prompt
            # cache prefix stays stable across turns.
            messages = await self._inject_pre_llm_context(
                messages=messages,
                tools=tool_defs,
                model=selected_model,
            )

            # Cooperative interrupt check — if the cron poller set the
            # flag on the previous iteration, exit before firing another
            # LLM call. In-flight tool calls have already settled.
            if self._interrupt_requested:
                logger.info(
                    f"Agent loop interrupted: {self._interrupt_reason or 'no reason'}"
                )
                self.clear_interrupt()
                raise RuntimeError(
                    f"Interrupted: {self._interrupt_reason or 'inactivity timeout'}"
                )

            # Use streaming whenever a stream_callback is provided. We previously
            # suppressed streaming on action turns (tool_choice="required") on
            # the assumption that tool-call chunks produce no user-visible
            # text — but in practice the LLM almost always emits a short
            # narration before the tool call ("Let me draft that…"), and
            # voice mode needs that text in real time so it can speak it.
            #
            # `_chat_with_stream` already accumulates `tool_calls` from the
            # final stream chunk (see lines 1137-1143) and the OpenRouter
            # provider's `chat_stream` method properly aggregates partial
            # tool_call deltas (openrouter_provider.py:369-406), so enabling
            # streaming here doesn't break tool execution. The non-streaming
            # fallback below (line ~1257) still kicks in if the streamed
            # required-tool turn errors.
            use_stream = stream_callback is not None
            if use_stream:
                response = await self._chat_with_stream(
                    messages=messages,
                    tools=tool_defs,
                    model=selected_model,
                    temperature=selected_temperature,
                    tool_choice=tool_choice,
                    stream_callback=stream_callback,
                    run_id=outbound_run_id,
                )
            else:
                response = await self.provider.chat(
                    messages=messages,
                    tools=tool_defs,
                    model=selected_model,
                    temperature=selected_temperature,
                    tool_choice=tool_choice,
                )

            # Cooperative abort — short-circuit the tool loop. Two
            # ways to arrive here:
            # 1. Streaming: ``_chat_with_stream`` broke out mid-flight
            #    and tagged the response ``finish_reason="aborted"``
            #    with whatever ``accumulated_text`` was received.
            # 2. Non-streaming or post-stream: user hit Stop after
            #    the LLM finished but before tool execution. The
            #    response itself is fine; we just refuse to chain
            #    another iteration / execute tools.
            # In both cases the partial text becomes the final
            # content; the surrounding ``_process_message_inner``
            # tags the OutboundMessage with ``aborted: true`` so
            # the UI can render the [Aborted] marker.
            if outbound_run_id and self.is_run_aborted(outbound_run_id):
                final_content = response.content or ""
                logger.info(
                    f"[loop] aborted at iteration {iteration}, "
                    f"{len(final_content)} chars preserved, "
                    f"run_id={outbound_run_id}"
                )
                break

            # If streaming failed, fall back to blocking chat()
            if use_stream and response.content and response.content.startswith("Error calling LLM:"):
                logger.warning("Streaming failed, falling back to blocking chat()")
                response = await self.provider.chat(
                    messages=messages,
                    tools=tool_defs,
                    model=selected_model,
                    temperature=selected_temperature,
                    tool_choice=tool_choice,
                )

            if response.content and response.content.startswith("Error") and tool_choice == "required":
                logger.warning(f"tool_choice=required failed, retrying with auto: {response.content[:120]}")
                response = await self.provider.chat(
                    messages=messages,
                    tools=tool_defs,
                    model=selected_model,
                    temperature=selected_temperature,
                    tool_choice="auto",
                )

            # Audit the LLM call after the retry chain settles. We log
            # one row per iteration (not per individual attempt) — the
            # iteration counter + duration_ms together expose retry
            # storms in aggregate without inflating JSONL volume.
            # Failures are recorded too: ``finish_reason="error"`` is
            # the strongest production debug signal for "agent kept
            # hitting overflow / schema rejection".
            try:
                _usage = response.usage or {}
                _audit.log_llm_call(
                    session_key=_current_session_key,
                    model=selected_model,
                    prompt_tokens=int(_usage.get("prompt_tokens", 0) or 0),
                    completion_tokens=int(_usage.get("completion_tokens", 0) or 0),
                    duration_ms=int((time.monotonic() - _iter_t0) * 1000),
                    tool_choice=tool_choice,
                    iteration=iteration,
                    finish_reason=getattr(response, "finish_reason", None),
                    families=list(detect_model_families(selected_model)),
                    cache_read_tokens=int(_usage.get("cache_read_tokens", 0) or 0),
                    cache_write_tokens=int(_usage.get("cache_write_tokens", 0) or 0),
                    streamed=use_stream,
                )
            except Exception:
                # Audit must never break the agent loop. Failures here
                # are typically disk / permission issues; logger.debug
                # alone is loud enough for diagnostics.
                logger.debug("audit log_llm_call failed", exc_info=True)

            if response.content and response.content.startswith("Error calling LLM:"):
                lowered_error = response.content.lower()
                schema_rejected = (
                    "input_schema does not support oneof" in lowered_error
                    or "input_schema does not support allof" in lowered_error
                    or "input_schema does not support anyof" in lowered_error
                )

                # ── Overflow recovery ──────────────────────────────────────
                if not _overflow_recovered and is_context_overflow(response.content):
                    _overflow_recovered = True
                    logger.warning(
                        "Context overflow detected — trimming messages and retrying"
                    )
                    # Keep system prompt (first message) + last 20 messages
                    system_msgs = [m for m in messages if m.get("role") == "system"]
                    non_system = [m for m in messages if m.get("role") != "system"]
                    tokens_before = estimate_messages_tokens(messages)
                    keep = non_system[-20:]
                    messages = system_msgs + keep
                    tokens_after = estimate_messages_tokens(messages)
                    dropped = len(non_system) - len(keep)
                    logger.info(
                        f"Overflow recovery: dropped {dropped} messages, "
                        f"{tokens_before} → {tokens_after} tokens"
                    )
                    _audit.log_overflow_recovery(
                        session_key=_current_session_key,
                        tokens_before=tokens_before,
                        tokens_after=tokens_after,
                        messages_dropped=dropped,
                    )
                    continue  # retry the LLM call with trimmed context
                # ─────────────────────────────────────────────────────────

                if schema_rejected:
                    logger.error("Provider rejected tool schema; aborting turn without additional retries.")
                    final_content = (
                        "Tool schema was rejected by the model provider. "
                        "No action was taken."
                    )
                else:
                    logger.error(f"LLM call failed after fallback: {response.content}")
                    # Surface the provider's ACTUAL error (the part after the
                    # "Error calling LLM:" prefix) instead of an opaque
                    # "no valid response" — so a wrong-model / unsupported-input
                    # failure (e.g. "this model does not support image input")
                    # is diagnosable. Trimmed to stay readable in a chat bubble.
                    _detail = response.content.split("Error calling LLM:", 1)[-1].strip()
                    if len(_detail) > 400:
                        _detail = _detail[:400].rstrip() + "…"
                    final_content = (
                        f"The model provider returned an error:\n\n{_detail}"
                        if _detail
                        else "Could not get a valid response from the model provider. No action was taken."
                    )
                break

            logger.info(
                "LLM response telemetry: "
                f"has_tool_calls={response.has_tool_calls}, content_len={len(response.content or '')}, "
                f"action_turn={action_turn}, live_call_turn={live_call_turn}, iteration={iteration}"
            )

            # Two different accumulation semantics live in the same
            # dict so a single outbound payload satisfies both consumers:
            #
            # • **input-side** (``prompt_tokens``, ``cache_read_tokens``)
            #   → REPLACED on every iteration. They describe the
            #   *current* prompt sent on this call, which already
            #   includes every previous iteration's tool calls + tool
            #   results. Summing would triple-count for tool-heavy turns
            #   ("turn used 5500 tokens" when the LLM only saw 2200).
            #
            # • **output-side** (``completion_tokens``, ``cache_write_tokens``)
            #   → SUMMED across iterations. They represent new bytes
            #   produced (or newly cached) and adding them gives the
            #   true cost of the whole turn.
            #
            # ``total_tokens`` is recomputed from the two sides so it
            # stays internally consistent for any downstream code that
            # reads it instead of summing components.
            if response.usage:
                for k in ("prompt_tokens", "cache_read_tokens"):
                    total_usage[k] = response.usage.get(k, 0) or 0
                for k in ("completion_tokens", "cache_write_tokens"):
                    total_usage[k] = (
                        total_usage.get(k, 0) + (response.usage.get(k, 0) or 0)
                    )
                total_usage["total_tokens"] = (
                    total_usage.get("prompt_tokens", 0)
                    + total_usage.get("completion_tokens", 0)
                    + total_usage.get("cache_read_tokens", 0)
                    + total_usage.get("cache_write_tokens", 0)
                )

            if response.has_tool_calls:
                tool_call_dicts = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": json.dumps(tc.arguments),
                        },
                        # Replay Gemini thought_signature on the next turn (and
                        # persist it). Stripped for non-Gemini targets in the
                        # provider's _prepare_messages. Absent for other providers.
                        **({"extra_content": tc.extra_content} if getattr(tc, "extra_content", None) else {}),
                    }
                    for tc in response.tool_calls
                ]

                assistant_content = None
                if response.content:
                    content_lower = response.content.lower()
                    hallucination_phrases = [
                        "i did", "i sent", "i took", "i opened", "i closed",
                        "done", "completed", "finished",
                        "yaptım", "gönderdim", "aldım", "açtım", "kapattım", "tamamlandı",
                    ]
                    if not any(phrase in content_lower for phrase in hallucination_phrases):
                        assistant_content = response.content

                messages = self.context.add_assistant_message(
                    messages, assistant_content, tool_call_dicts
                )

                # Emit live UI event for the assistant_with_tool_calls
                # we just appended. The web channel forwards this to
                # the relay which writes the corresponding tool_turns/
                # doc with inProgress:true. ChatGPT-style: the panel
                # populates as the model emits each tool call,
                # without waiting for the whole turn to finish.
                await self._emit_iteration_event(
                    outbound_channel=outbound_channel,
                    outbound_chat_id=outbound_chat_id,
                    outbound_run_id=outbound_run_id,
                    iteration_idx=_iteration_event_idx,
                    message=messages[-1],
                    on_iteration=on_iteration,
                )
                _iteration_event_idx += 1

                turn_tools: list[str] = []
                terminal_action_executed = False
                turn_success_count = 0
                for tool_call in response.tool_calls:
                    turn_tools.append(tool_call.name)
                    executed_tool_names.append(tool_call.name)
                    args_str = json.dumps(_redact_log_args(tool_call.arguments))
                    logger.info(f"Executing tool: {tool_call.name}({args_str[:160]}...)")
                    # Heartbeat for the inactivity watchdog — tool
                    # launches are the most common "agent is alive"
                    # signal, especially for long research loops.
                    self._touch_activity(f"executing tool: {tool_call.name}", tool=tool_call.name)

                    if live_call_turn and not self._is_live_call_tool_allowed(
                        tool_call.name,
                        tool_call.arguments,
                    ):
                        blocked_tools.append(tool_call.name)
                        result = (
                            f"Error: Tool '{tool_call.name}' was blocked by the "
                            "live-call security policy."
                        )
                        logger.error(
                            f"Live call blocked risky tool: {tool_call.name} args={args_str[:160]}"
                        )
                        accumulated_tool_results.append({
                            "tool": tool_call.name,
                            "success": False,
                            "result": result,
                        })
                        messages = self.context.add_tool_result(
                            messages, tool_call.id, tool_call.name, result
                        )
                        await self._emit_iteration_event(
                            outbound_channel=outbound_channel,
                            outbound_chat_id=outbound_chat_id,
                            outbound_run_id=outbound_run_id,
                            iteration_idx=_iteration_event_idx,
                            message=messages[-1],
                            on_iteration=on_iteration,
                        )
                        _iteration_event_idx += 1
                        continue

                    # Media-intent guard: reject screenshot / capture_window
                    # when the user didn't explicitly ask for an image. The
                    # error message redirects the model to AX-direct reads
                    # so it doesn't just retry the same tool. See
                    # _is_media_tool_call_blocked for the intent phrases
                    # and _user_wants_media_output for the matching logic.
                    if self._is_media_tool_call_blocked(
                        tool_call.name,
                        tool_call.arguments,
                        messages,
                    ):
                        blocked_tools.append(tool_call.name)
                        action_label = (
                            tool_call.name
                            if tool_call.name != "computer"
                            else f"computer({tool_call.arguments.get('action', '?')})"
                        )
                        result = (
                            f"BLOCKED: {action_label} produces a pixel image. "
                            "The user asked for a task, not a visual. Read "
                            "structured state instead:\n"
                            "  - read_window_state(pid=...) — indexed AX "
                            "elements with role, title, value\n"
                            "  - read_window_text(pid=...) — concatenated "
                            "plaintext of the window\n"
                            "  - read_focused_text() — current focused field\n"
                            "Look for the answer in the AXValue / AXTitle "
                            "of the snapshot elements. If you genuinely "
                            "cannot answer from AX state, say so honestly — "
                            "do NOT retry with another media tool."
                        )
                        logger.info(
                            f"Media-intent guard blocked: {action_label} "
                            f"args={args_str[:160]}"
                        )
                        accumulated_tool_results.append({
                            "tool": tool_call.name,
                            "success": False,
                            "result": result,
                        })
                        messages = self.context.add_tool_result(
                            messages, tool_call.id, tool_call.name, result
                        )
                        await self._emit_iteration_event(
                            outbound_channel=outbound_channel,
                            outbound_chat_id=outbound_chat_id,
                            outbound_run_id=outbound_run_id,
                            iteration_idx=_iteration_event_idx,
                            message=messages[-1],
                            on_iteration=on_iteration,
                        )
                        _iteration_event_idx += 1
                        continue

                    _t0 = time.monotonic()
                    _tool_result = ""
                    _tool_success = False
                    call_args = dict(tool_call.arguments)
                    _effective_tool_name = tool_call.name
                    # Best-effort live event for streaming clients. Failures
                    # (no callback wired, peer disconnect, slow consumer)
                    # must not affect agent execution.
                    if self.tool_callback:
                        try:
                            _r = self.tool_callback("tool.start", {
                                "toolCallId": getattr(tool_call, "id", "") or "",
                                "name": _effective_tool_name,
                                "args": call_args,
                                "sessionKey": _current_session_key,
                            })
                            if asyncio.iscoroutine(_r):
                                await _r
                        except Exception:
                            pass
                    try:
                        # Inject the active session for approval routing and
                        # conversation-scoped persistence.
                        if (
                            tool_call.name
                            in (
                                "exec",
                                "email",
                                "google_calendar",
                                "google_drive",
                                "google_tasks",
                                "linear",
                                "github",
                                "sentry",
                                "process",
                                "clarify",
                                "artifact",
                            )
                            and "session_key" not in call_args
                        ):
                            call_args["session_key"] = _current_session_key

                        # Spawn interception: redirect spawn → builtin_agent
                        # when task matches a specialist keyword (enterprise pattern)
                        if tool_call.name == "spawn" and not _builtin_agent_dispatched:
                            _task_text = str(call_args.get("task", ""))
                            _detected_agent = _detect_builtin_agent_type(_task_text)
                            if _detected_agent:
                                logger.info(
                                    f"Spawn intercepted → builtin:{_detected_agent} "
                                    f"(keywords matched in task)"
                                )
                                call_args = {"agent": _detected_agent, "task": _task_text}
                                _effective_tool_name = "builtin_agent"

                        _tool_result = await self.tools.execute(_effective_tool_name, call_args)
                        # Reply-media envelope: a tool (image_generate, screenshot)
                        # produced file(s) for THIS turn's reply. Peel the paths onto
                        # the turn collector and replace the raw JSON with the human
                        # summary — so the media rides the assistant's own reply (one
                        # bubble, normal delivery) and the model + tool-turn UI see
                        # clean text, not an envelope.
                        _attach_paths, _attach_summary = extract_reply_media(_tool_result)
                        if _attach_summary is not None:
                            # It was a reply-media envelope: collect any existing
                            # files and ALWAYS swap the raw JSON for the human
                            # summary (even if a file vanished, the model never
                            # sees an envelope).
                            for _p in _attach_paths:
                                if _p not in reply_media:
                                    reply_media.append(_p)
                            _tool_result = _attach_summary
                        _tool_success = not _tool_result.startswith("Error")
                        accumulated_tool_results.append({
                            "tool": tool_call.name,
                            "success": _tool_success,
                            "result": _tool_result[:500] if len(_tool_result) > 500 else _tool_result,
                        })
                        result = _tool_result
                    except Exception as e:
                        result = f"Error executing {tool_call.name}: {str(e)}"
                        _tool_result = result
                        _tool_success = False
                        logger.error(result)
                        accumulated_tool_results.append({
                            "tool": tool_call.name,
                            "success": False,
                            "result": result,
                        })
                    else:
                        _tool_elapsed = time.monotonic() - _t0
                        if _tool_success:
                            turn_success_count += 1
                            logger.info(
                                f"Tool success: {tool_call.name} result={result[:180]}"
                            )
                            # Track ASYNC (background) subagent dispatch so the
                            # next iteration hides tools and the turn ends with a
                            # plain acknowledgement — letting each subagent's
                            # result be delivered and answered incrementally as it
                            # finishes, instead of the parent marathoning on its
                            # own tool calls (staying "busy") and forcing every
                            # announce to queue up until the turn finally ends.
                            # The async envelope reports status "dispatched"; the
                            # older/spawn paths used "accepted" — match both. (The
                            # previous "accepted"-only check never fired for an
                            # async builtin_agent, so the turn never ended early.)
                            if _effective_tool_name == "builtin_agent" and (
                                '"dispatched"' in result or '"accepted"' in result
                            ):
                                _builtin_agent_dispatched = True
                        else:
                            logger.warning(
                                f"Tool failed: {tool_call.name} result={result[:220]}"
                            )
                        # Heartbeat — tool just finished, refresh the
                        # inactivity clock so the next iteration's LLM
                        # round trip doesn't look idle.
                        self._touch_activity(
                            f"tool completed: {tool_call.name} ({_tool_elapsed:.1f}s)",
                        )
                    finally:
                        _duration_ms = int((time.monotonic() - _t0) * 1000)
                        _audit.log_tool_call(
                            session_key=_current_session_key,
                            tool_name=tool_call.name,
                            args=tool_call.arguments,
                            result=_tool_result,
                            duration_ms=_duration_ms,
                            success=_tool_success,
                        )
                        if self.tool_callback:
                            try:
                                _r = self.tool_callback("tool.complete", {
                                    "toolCallId": getattr(tool_call, "id", "") or "",
                                    "name": _effective_tool_name,
                                    "success": _tool_success,
                                    "durationMs": _duration_ms,
                                    "preview": (_tool_result or "")[:200],
                                    "sessionKey": _current_session_key,
                                })
                                if asyncio.iscoroutine(_r):
                                    await _r
                            except Exception:
                                pass

                    # Tool results go through sanitize+truncate only. The
                    # short-lived parked-as-artifact path was removed
                    # because the raw tool envelope (web_fetch JSON,
                    # exec stdout) produced ugly artifacts that were
                    # awkward to promote; parent already has per-tool
                    # caps (_TOOL_MAX_CHARS) and can re-call the tool
                    # with a narrower query when it needs more detail.
                    # Context persistence now exists only on the
                    # subagent boundary (Assistant.cap_to_artifact).
                    sanitized_result = _sanitize_tool_result(result, tool_call.name)

                    # If the tool flagged its result as an image-bearing
                    # screenshot, extract the data URL from the RAW
                    # (pre-sanitize) result and pass tool_result content
                    # as a list of blocks: text first, then image. The
                    # provider sends this through to the LLM's vision
                    # input. Without this the agent never sees the
                    # picture and stays blind on canvas apps.
                    image_url = _maybe_extract_image_for_vision(result, tool_call.name)
                    if image_url:
                        tool_content: str | list[dict[str, Any]] = [
                            {"type": "text", "text": sanitized_result},
                            {"type": "image_url", "image_url": {"url": image_url}},
                        ]
                    else:
                        tool_content = sanitized_result

                    messages = self.context.add_tool_result(
                        messages, tool_call.id, tool_call.name, tool_content
                    )
                    # Live tool-result event for the UI panel — same
                    # path as the two earlier early-out branches.
                    await self._emit_iteration_event(
                        outbound_channel=outbound_channel,
                        outbound_chat_id=outbound_chat_id,
                        outbound_run_id=outbound_run_id,
                        iteration_idx=_iteration_event_idx,
                        message=messages[-1],
                        on_iteration=on_iteration,
                    )
                    _iteration_event_idx += 1

                    # codex_session projects Codex's inner item stream
                    # (exec / apply_patch / mcp tool calls) onto a
                    # per-session queue. Splice the self-consistent
                    # assistant-tool_call + tool-result PAIRS into the
                    # live transcript right after the codex_session
                    # result so the user sees what Codex actually did.
                    # Standalone messages (reasoning, final text, user
                    # replay) are dropped here — the final text already
                    # rides in the codex_session result envelope, and
                    # injecting a trailing assistant message would risk
                    # two consecutive assistant turns for strict providers.
                    if tool_call.name == "codex_session":
                        codex_pairs = self._drain_codex_projected_pairs()
                        for _cm in codex_pairs:
                            messages.append(_cm)
                        if codex_pairs:
                            for _cm in codex_pairs:
                                await self._emit_iteration_event(
                                    outbound_channel=outbound_channel,
                                    outbound_chat_id=outbound_chat_id,
                                    outbound_run_id=outbound_run_id,
                                    iteration_idx=_iteration_event_idx,
                                    message=_cm,
                                    on_iteration=on_iteration,
                                )
                                _iteration_event_idx += 1

                    # In strict action turns, stop as soon as a terminal action succeeds.
                    if not result.startswith("Error"):
                        if tool_call.name == "cron":
                            cron_action = str(tool_call.arguments.get("action", "")).lower()
                            target_tool = str(tool_call.arguments.get("tool_name", "")).lower()
                            if cron_action == "add" and target_tool == "voice_call":
                                terminal_action_executed = True
                        elif enforce_action_tools and tool_call.name == "voice_call":
                            voice_action = str(tool_call.arguments.get("action", "")).lower()
                            if voice_action in {"call", "end_call", "speak"}:
                                terminal_action_executed = True

                    if terminal_action_executed:
                        logger.info(
                            "Action turn terminal tool executed; skipping remaining tool calls in this batch."
                        )
                        break

                logger.info(f"Tool execution telemetry: executed_tools={turn_tools}")
                tools_were_used = True
                if turn_success_count > 0:
                    successful_tools_were_used = True
                    forced_tool_retry = False

                if terminal_action_executed:
                    successful = [t for t in accumulated_tool_results if t.get("success")]
                    if successful:
                        last_ok = successful[-1]
                        final_content = (
                            "Action completed.\n"
                            f"{last_ok['tool']}: {last_ok['result']}"
                        )
                    else:
                        final_content = "Action executed."
                    break

                if live_call_turn and not enforce_action_tools:
                    successful = [t for t in accumulated_tool_results if t.get("success")]
                    if successful:
                        last_ok = successful[-1]
                        final_content = (
                            response.content.strip()
                            if response.content and response.content.strip()
                            else f"Action completed: {last_ok['tool']}"
                        )
                    else:
                        final_content = "No safe tool could be executed for the live call."
                    break

                if enforce_action_tools and turn_success_count == 0:
                    if no_tool_retry_count < self.action_tool_retries:
                        no_tool_retry_count += 1
                        logger.warning(
                            "Action turn tool calls all failed; retrying with corrective instruction "
                            f"({no_tool_retry_count}/{self.action_tool_retries})"
                        )
                        messages.append({
                            "role": "user",
                            "content": (
                                "The previous tool call failed. "
                                "Retry the relevant tool with correct parameters. "
                                "If it fails, give a clear error — do not call unrelated tools."
                            ),
                            _EPHEMERAL_NUDGE: True,
                        })
                        continue
                    final_content = "Tool calls failed, no action was taken."
                    break

                continue

            # Provider/model may hallucinate completion without emitting tool calls.
            # Guard: force a corrective tool-only retry before responding.
            # Gated on action_turn: a completion claim is only suspicious when the
            # USER asked for an action this turn. On Q&A / retrospective turns
            # ("toplam tool kullanımlarını söyler misin?") the model naturally says
            # "şunları yaptım/kullandım" while reporting history — firing here
            # discarded a correct streamed answer and forced an irrelevant tool
            # call (observed 2026-06-11: sessions_list forced on a usage question).
            if (
                action_turn
                and not successful_tools_were_used
                and response.content
                and self._contains_unverified_completion_claim(response.content)
                and no_tool_retry_count < self.action_tool_retries
            ):
                no_tool_retry_count += 1
                forced_tool_retry = True
                logger.warning(
                    "Completion claim without tool call; retrying with forced tool instruction "
                    f"({no_tool_retry_count}/{self.action_tool_retries})"
                )
                messages.append({
                    "role": "user",
                    "content": (
                        "The previous response claims the action was done but no tool was called. "
                        "You must call the appropriate tool now. "
                        "Do not claim completion without executing a tool."
                    ),
                    _EPHEMERAL_NUDGE: True,
                })
                continue

            if enforce_action_tools and not successful_tools_were_used:
                if no_tool_retry_count < self.action_tool_retries:
                    no_tool_retry_count += 1
                    logger.warning(
                        "Action turn returned no tool call; retrying with corrective instruction "
                        f"({no_tool_retry_count}/{self.action_tool_retries})"
                    )
                    messages.append({
                        "role": "user",
                        "content": (
                            "This is an action request. Call the appropriate tool now. "
                            "Do not claim completion without executing a tool."
                        ),
                        _EPHEMERAL_NUDGE: True,
                    })
                    continue

                final_content = "Tool call could not be verified, no action was taken."
                break

            if forced_tool_retry and not successful_tools_were_used:
                final_content = "Tool call could not be verified, no action was taken."
                break

            # Guard: think-only empty response — model produced thinking
            # but no content and no tool calls. Retry up to 2 times.
            if not response.content or not response.content.strip():
                _empty_response_count += 1
                if _empty_response_count <= 2:
                    logger.warning(
                        f"Empty response (think-only?) — retrying "
                        f"({_empty_response_count}/2)"
                    )
                    continue
                # Give up after 2 retries
                logger.error("Empty response after 2 retries — using fallback")

            # ── Plan end-turn guard (Phase 2b) ──────────────────────
            # If a plan was created in this turn AND it's still active
            # (not complete/blocked/aborted), the agent is trying to end
            # the turn with unfinished work. Nudge once: complete the
            # plan, mark remaining steps blocked, or escalate to user.
            #
            # Safety constraints (in priority order):
            #   1. ONLY fires when an active plan exists for this
            #      session — won't affect non-browser tasks at all.
            #   2. Fires AT MOST ONCE per turn (`_plan_nudged`) — even
            #      if the agent ignores the nudge and immediately tries
            #      to end again, we accept on the second attempt to
            #      avoid an infinite loop.
            #   3. Errors during the check are swallowed — guard must
            #      NEVER block end-turn on infrastructure issues.
            try:
                if not getattr(self, "_plan_nudged_this_turn", False):
                    from flowly.agent.planner.state import get_plan_state
                    sess = getattr(self.tools, "_active_session_id", "") or ""
                    plan = get_plan_state().get(sess) if sess else None
                    if plan and plan.status == "active":
                        unfinished = [
                            s for s in plan.steps
                            if s.status in ("pending", "in_progress")
                        ]
                        # Don't nudge if plan was JUST created (no
                        # progress at all) — agent may have intentionally
                        # planned then asked user a clarifying question.
                        any_progress = any(
                            s.status in ("done", "blocked", "skipped")
                            for s in plan.steps
                        )
                        if any_progress and unfinished:
                            self._plan_nudged_this_turn = True
                            logger.warning(
                                f"[loop] end-turn guard: plan {plan.id} active with "
                                f"{len(unfinished)} unfinished step(s); nudging agent"
                            )
                            unfinished_summary = ", ".join(
                                f"#{s.id} ({s.status})" for s in unfinished[:5]
                            )
                            messages.append({
                                "role": "user",
                                "content": (
                                    f"You're ending the turn but plan {plan.id} is "
                                    f"still active. Unfinished steps: {unfinished_summary}. "
                                    f"Pick ONE:\n"
                                    f"a) Finish the remaining steps and verify each, "
                                    f"then call browser_plan(action='complete', "
                                    f"final_evidence='...').\n"
                                    f"b) If a step is genuinely blocked, mark it "
                                    f"with browser_plan(action='update_step', id=N, "
                                    f"status='blocked', evidence='reason') so we "
                                    f"have a record.\n"
                                    f"c) If you need user input to proceed, fine — "
                                    f"call complete() with final_evidence describing "
                                    f"what was done so far AND tell the user what "
                                    f"you need.\n"
                                    f"Don't claim 'done' in your reply if the plan "
                                    f"isn't actually complete — that's the silent-"
                                    f"hallucination this guard exists to catch."
                                ),
                                _EPHEMERAL_NUDGE: True,
                            })
                            continue  # one more iteration
            except Exception:
                logger.exception("[loop] plan end-turn guard failed (non-fatal)")
            # ────────────────────────────────────────────────────────

            # Leaked tool-call guard: the model emitted a tool call as inline
            # text (DSML / <invoke> / <tool_calls>) instead of the structured
            # tool API, so there are no real tool_calls to execute and this
            # markup would otherwise become the final answer (it leaked into
            # chat). Touches the TEXT only — tool_calls/tool cards are untouched.
            if _contains_leaked_tool_markup(response.content):
                stripped = _strip_leaked_tool_markup(response.content)
                if not stripped and no_tool_retry_count < self.action_tool_retries:
                    no_tool_retry_count += 1
                    logger.warning(
                        "Model wrote a tool call as inline text instead of calling "
                        f"the tool; retrying ({no_tool_retry_count}/{self.action_tool_retries})"
                    )
                    messages.append({
                        "role": "user",
                        "content": (
                            "Your previous message contained a tool call written as "
                            "text. Call the tool through the tool interface — never "
                            "write tool-call markup in your message text."
                        ),
                        _EPHEMERAL_NUDGE: True,
                    })
                    continue
                final_content = stripped
                break

            final_content = response.content
            break

        if enforce_action_tools and not successful_tools_were_used:
            if not final_content or not final_content.startswith("Tool"):
                final_content = "Tool calls failed, no action was taken."

        if final_content is None:
            if accumulated_tool_results:
                summary = f"Actions completed ({len(accumulated_tool_results)} tools executed):\n"
                for tr in accumulated_tool_results[-5:]:
                    status = "✓" if tr["success"] else "✗"
                    summary += f"  {status} {tr['tool']}\n"
                final_content = summary
            else:
                final_content = "Action completed but no response could be generated."

        if not final_content or not final_content.strip():
            if enforce_action_tools and not successful_tools_were_used:
                final_content = "Tool call could not be verified, no action was taken."
            elif accumulated_tool_results:
                final_content = "✓ Action completed."
            else:
                final_content = "Action completed but no response could be generated."

        if (
            final_content
            and not executed_tool_names
            and (action_turn or self._is_retry_action_followup(turn_content))
            and self._contains_unverified_completion_claim(final_content)
        ):
            logger.warning("Suppressed unverified completion claim because no tool was executed.")
            final_content = "No tool was executed, no action was taken."

        logger.info(
            "LLM final telemetry: "
            f"final_content_length={len(final_content)}, executed_tools={executed_tool_names}, "
            f"action_turn={action_turn}, live_call_turn={live_call_turn}, "
            f"blocked_tools={sorted(set(blocked_tools))}"
        )

        if enforce_action_tools and not executed_tool_names:
            logger.error("Action turn alarm: executed_tools=0")

        # If the loop produced a hardcoded fallback, ask the model to
        # summarize what happened in natural language — always, regardless
        # of whether tool results exist. Users should never see generic
        # "no action was taken" messages.
        if final_content and self._is_hardcoded_fallback(final_content):
            logger.info("Requesting model summary turn to replace hardcoded fallback")
            summary = await self._request_summary_turn(messages, accumulated_tool_results)
            if summary:
                final_content = summary

        return final_content, accumulated_tool_results, executed_tool_names, total_usage, messages

    async def _run_memory_flush(
        self,
        session: Any,
        channel: str,
        chat_id: str,
    ) -> None:
        """
        Run a pre-compaction memory flush turn.

        This gives the agent a chance to save important information
        to disk before context gets compacted.
        """
        user_prompt, system_prompt = self.compaction.get_memory_flush_prompt()

        # Build messages with flush prompt. Pass self.model so the
        # family-aware guidance block matches the model the flush
        # turn will actually run on — memory flush is a real LLM
        # turn, not a pre-render, and silently shipping the wrong
        # family directives would defeat the whole point. Channel
        # flows through too so e.g. a flush on the WhatsApp surface
        # doesn't suddenly emit Markdown formatting.
        messages = self.context.build_messages(
            history=session.get_history(max_messages=self.context_messages),
            current_message=user_prompt,
            model=self.model,
            channel=channel,
        )

        # Add system prompt for flush context
        messages[0]["content"] += f"\n\n{system_prompt}"

        # Run a single turn with tools available
        try:
            response = await self.provider.chat(
                messages=messages,
                tools=self.tools.get_definitions(),
                model=self.model
            )

            # Execute any tool calls (agent might want to write to memory)
            if response.has_tool_calls:
                for tool_call in response.tool_calls:
                    logger.debug(f"Memory flush tool: {tool_call.name}")
                    await self.tools.execute(tool_call.name, tool_call.arguments)

            # Check if response should be silent
            content = response.content or ""
            if not self.compaction.is_silent_reply(content):
                # Agent wants to communicate something
                stripped = self.compaction.strip_silent_token(content)
                if stripped:
                    logger.info(f"Memory flush response: {stripped[:100]}...")
                    # Optionally send to user
                    await self.bus.publish_outbound(OutboundMessage(
                        channel=channel,
                        chat_id=chat_id,
                        content=f"📝 {stripped}"
                    ))

            # Save flush interaction to session
            session.add_message("user", f"[System: Memory Flush] {user_prompt}")
            session.add_message("assistant", content)
            self.sessions.save(session)

        except Exception as e:
            logger.warning(f"Memory flush failed: {e}")
    
    async def _process_message(self, msg: InboundMessage) -> OutboundMessage | None:
        """
        Process a single inbound message.
        
        Args:
            msg: The inbound message to process.
        
        Returns:
            The response message, or None if no response needed.
        """
        # Handle system messages (subagent announces)
        # The chat_id contains the original "channel:chat_id" to route back to
        if msg.channel == "system":
            return await self._process_system_message(msg)

        # pre_gateway_dispatch — let plugins drop or rewrite the inbound
        # message BEFORE any session/processing work happens. Runs once
        # per inbound from any channel (telegram/web/desktop/iOS/cli).
        from flowly.agent.hooks import (
            GatewayDispatchContext, SkipAction, RewriteAction,
        )
        gw_ctx = GatewayDispatchContext(
            session_id=msg.session_key,
            event=msg,
            gateway=msg.channel,
            session_store=self.sessions,
        )
        gw_action = await self.hooks.fire_gateway_dispatch(gw_ctx)
        if isinstance(gw_action, SkipAction):
            logger.info(
                f"pre_gateway_dispatch dropped message from "
                f"{msg.channel}:{msg.sender_id}: {gw_action.reason or 'no reason'}"
            )
            return None
        if isinstance(gw_action, RewriteAction):
            msg.content = gw_action.text

        voice_hint = " [voice_mode]" if msg.metadata.get("voice_mode") else ""
        logger.info(f"Processing message from {msg.channel}:{msg.sender_id}{voice_hint}")

        # Mark genuine user conversation activity for the dreamer's idle trigger —
        # NOT shared with _last_activity_ts, which background heartbeat/cron turns
        # also touch (a 30-min heartbeat would otherwise keep resetting a 30-min
        # idle clock, so the idle pass would never fire).
        if _is_user_activity_channel(msg.channel):
            self._dreamer_last_user_ts = time.time()

        # Mark parent session busy so subagent announces are queued, not injected mid-processing
        self.subagents.mark_busy(msg.session_key)

        # Bind the active session_id so ToolHookContext carries it into
        # plugin pre/post_tool hooks (e.g. disk-cleanup's per-session
        # tracker keys off this).
        self.tools.set_active_session(msg.session_key)

        # Bind the active session_key + stream callback for the
        # codex_session tool (if registered). Lets the tool resolve the
        # right CodexSession + metadata + stream sink without the main
        # agent having to thread the key through tool args.
        self._codex_active_session_key = msg.session_key
        self._codex_stream_callback = msg.metadata.get("stream_callback")

        # Session lifecycle hooks — on_session_start fires once per
        # session_key, on_session_end fires after every turn.
        from flowly.agent.hooks import SessionHookContext
        if msg.session_key not in self._started_sessions:
            self._started_sessions.add(msg.session_key)
            await self.hooks.fire_session_start(SessionHookContext(
                session_id=msg.session_key,
                model=self.model,
                platform=msg.channel,
            ))

        completed = True
        try:
            return await self._process_message_inner(msg)
        except Exception:
            completed = False
            raise
        finally:
            self.subagents.mark_idle(msg.session_key)
            self.tools.set_active_session("")
            # Clear codex-tool turn state so a concurrent session's
            # codex_session call can't read this turn's stream callback.
            self._codex_active_session_key = ""
            self._codex_stream_callback = None
            await self.hooks.fire_session_end(SessionHookContext(
                session_id=msg.session_key,
                model=self.model,
                platform=msg.channel,
                completed=completed,
            ))

    async def _process_message_inner(self, msg: InboundMessage) -> OutboundMessage | None:
        """Inner message processing (called with session marked busy)."""

        # Handle slash commands from ALL channels
        # Telegram sets is_command metadata; Desktop/Web/iOS send raw text.
        # Parse from content so /compact works everywhere.
        is_command = msg.metadata.get("is_command", False)
        command = msg.metadata.get("command", "")
        command_args = ""

        if not is_command and msg.content.strip().startswith("/"):
            parts = msg.content.strip().split(None, 1)
            parsed_cmd = parts[0][1:].lower()  # Remove leading /
            # Fix #24: Only intercept exact slash commands — "/new" not "/new endpoint discussion"
            # Destructive commands (new, clear) must be standalone (no trailing args)
            if parsed_cmd in ("compact", "help"):
                is_command = True
                command = parsed_cmd
                command_args = parts[1] if len(parts) > 1 else ""
            elif parsed_cmd in ("new", "clear") and len(parts) == 1:
                # Destructive commands only match if no extra text follows
                is_command = True
                command = parsed_cmd
                command_args = parts[1] if len(parts) > 1 else ""
            elif parsed_cmd in ("skills", "whoami", "status"):
                # Read-only introspection commands: accept optional args
                # (``/skills python`` filters by substring) but otherwise
                # behave as pure queries. No destructive side effects so
                # we don't gate on standalone-ness like /new / /clear.
                is_command = True
                command = parsed_cmd
                command_args = parts[1] if len(parts) > 1 else ""
            elif parsed_cmd == "learn":
                # ``/learn [--dry-run] [source]`` is intentionally a normal agent turn:
                # convert the short command into a Flowly-native skill-authoring
                # prompt, then let the live agent inspect sources and call
                # skill_manage. The displayed user turn stays as typed.
                is_command = True
                command = parsed_cmd
                command_args = parts[1] if len(parts) > 1 else ""
            elif parsed_cmd == "codex":
                # ``/codex [status|on|off|sandbox <level>|tools on|off]`` —
                # manage the opt-in Codex runtime live (no restart). Accepts
                # a sub-command arg.
                is_command = True
                command = parsed_cmd
                command_args = parts[1] if len(parts) > 1 else ""
            elif parsed_cmd in ("retry", "undo") and len(parts) == 1:
                # Session-edit commands (standalone, no args). /retry re-runs
                # the last prompt server-side; /undo pops the last turn. Handled
                # here so they work natively on every client that sends raw text
                # (Desktop/Web/iOS) — not just the TUI's own RPC path.
                is_command = True
                command = parsed_cmd
                command_args = ""

        if is_command and command in ("new", "clear"):
            session = self.sessions.get_or_create(msg.session_key)
            msg_count = len(session.messages)
            session.clear()
            session.metadata["persona"] = self.context.persona
            self.sessions.save(session)
            logger.info(f"Session {msg.session_key} cleared via /{command}")
            if msg.channel in ("web", "imessage"):
                # Channels whose adapter has no native command handler get
                # the confirmation from here.
                action = "New conversation started" if command == "new" else f"Cleared {msg_count} messages"
                return OutboundMessage(
                    channel=msg.channel, chat_id=msg.chat_id, content=f"✅ {action}",
                )
            return None  # Telegram handler already sent confirmation

        if is_command and command == "compact":
            session = self.sessions.get_or_create(msg.session_key)
            try:
                result = await self.compact_session(
                    msg.session_key, command_args or None
                )
                if result.get("success"):
                    # Send a compact marker — relay saves to Firestore, client renders as separator
                    return OutboundMessage(
                        channel=msg.channel, chat_id=msg.chat_id,
                        content="[context-optimized]",
                    )
                else:
                    return OutboundMessage(
                        channel=msg.channel, chat_id=msg.chat_id,
                        content=f"⚠️ {result.get('message', 'Compaction failed')}",
                    )
            except Exception as e:
                logger.error(f"Compact command failed: {e}")
                return OutboundMessage(
                    channel=msg.channel, chat_id=msg.chat_id,
                    content=f"❌ Compaction error: {e}",
                )

        if is_command and command == "retry":
            # Server-side re-run: pop the last user+assistant
            # turn, then re-submit that prompt by rewriting msg.content and
            # falling through to the normal agent loop → a FRESH reply. Works on
            # every client that sends raw text (Desktop/Web/iOS).
            session = self.sessions.get_or_create(msg.session_key)
            retry_text = session.drop_last_turn()
            if not retry_text:
                return OutboundMessage(
                    channel=msg.channel, chat_id=msg.chat_id,
                    content="↩️ Nothing to retry yet.",
                )
            self.sessions.save(session)
            msg.content = retry_text
            is_command = False
            command = ""
            # Do NOT return — execution continues to the agent run below.

        if is_command and command == "undo":
            # Pop the last user+assistant turn and hand the prompt back so the
            # user can edit + resend. (Composer pre-fill is a client refinement;
            # for now the removed text is returned inline.)
            session = self.sessions.get_or_create(msg.session_key)
            undo_text = session.drop_last_turn()
            if not undo_text:
                return OutboundMessage(
                    channel=msg.channel, chat_id=msg.chat_id,
                    content="↩️ Nothing to undo yet.",
                )
            self.sessions.save(session)
            return OutboundMessage(
                channel=msg.channel, chat_id=msg.chat_id,
                content=f"↩️ Removed your last turn. Re-send to edit:\n\n{undo_text}",
            )

        if is_command and command == "help":
            help_text = (
                "**Available commands:**\n"
                "• `/compact [instructions]` — Summarize conversation to save tokens\n"
                "• `/clear` — Clear conversation history\n"
                "• `/new` — Start new conversation\n"
                "• `/retry` — Re-run your last message for a fresh reply\n"
                "• `/undo` — Remove the last turn (returns your prompt to edit)\n"
                "• `/skills [filter]` — List available skills\n"
                "• `/learn [--dry-run] [source]` — Create or update a reusable skill from sources you describe\n"
                "• `/whoami` — Show user / server / conversation\n"
                "• `/status` — Session health summary\n"
                "• `/codex [on|off|sandbox <level>|cwd <path>|tools on|off]` — Manage the Codex runtime\n"
                "• `/help` — Show this help"
            )
            # Append plugin-registered slash commands so /help is a
            # complete reference across all channels.
            plugin_commands = []
            mgr = getattr(self, "_plugin_manager", None)
            if mgr is not None:
                for name, entry in sorted(mgr._slash_commands.items()):
                    desc = entry.get("description") or ""
                    plugin_commands.append(f"• `/{name}` — {desc}".rstrip(" —"))
            if plugin_commands:
                help_text += "\n\n**Plugin commands:**\n" + "\n".join(plugin_commands)
            return OutboundMessage(
                channel=msg.channel, chat_id=msg.chat_id, content=help_text,
            )

        if is_command and command == "skills":
            # ``/skills [filter]`` — list everything the skills loader
            # knows about for the active workspace, optionally filtered
            # by a case-insensitive substring against name or
            # description. Read-only: no skill is *loaded* here; that
            # happens when the user invokes ``/<skill-name>`` for a
            # single turn, or when the agent uses skill_view mid-turn.
            try:
                content = self._format_skills_list(command_args)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"/skills failed: {exc}")
                content = f"❌ Could not list skills: {exc}"
            return OutboundMessage(
                channel=msg.channel, chat_id=msg.chat_id, content=content,
            )

        if is_command and command == "learn":
            from flowly.agent.learn_prompt import build_learn_prompt, parse_learn_args

            original_content = msg.content
            dry_run, learn_source = parse_learn_args(command_args)
            msg.content = build_learn_prompt(learn_source, dry_run=dry_run)
            msg.metadata["_display_content"] = original_content
            is_command = False
            command = ""
            command_args = ""
            # Do NOT return — execution continues to the normal agent run below.

        if is_command and command == "whoami":
            return OutboundMessage(
                channel=msg.channel, chat_id=msg.chat_id,
                content=self._format_whoami(msg),
            )

        if is_command and command == "status":
            return OutboundMessage(
                channel=msg.channel, chat_id=msg.chat_id,
                content=self._format_status(msg),
            )

        if is_command and command == "codex":
            content = await self._handle_codex_command(command_args)
            return OutboundMessage(
                channel=msg.channel, chat_id=msg.chat_id, content=content,
            )

        # Plugin-registered slash commands (e.g. /disk-cleanup).
        # Parsed from raw text the same way as the built-ins above so
        # they work across all channels (Telegram, Web, Desktop, iOS).
        if not is_command and msg.content.strip().startswith("/"):
            parts = msg.content.strip().split(None, 1)
            plugin_cmd = parts[0][1:].lower()
            plugin_args = parts[1] if len(parts) > 1 else ""
            mgr = getattr(self, "_plugin_manager", None)
            if mgr is not None:
                handler = mgr.get_slash_handler(plugin_cmd)
                if handler is not None:
                    try:
                        result = handler(plugin_args)
                        if hasattr(result, "__await__"):
                            result = await result
                    except Exception as exc:
                        logger.exception(
                            "plugin slash /%s raised", plugin_cmd,
                        )
                        result = f"⚠️  /{plugin_cmd} failed: {exc}"
                    if result:
                        return OutboundMessage(
                            channel=msg.channel, chat_id=msg.chat_id,
                            content=str(result),
                        )
                    return None

        if not is_command and msg.content.strip().startswith("/"):
            try:
                from flowly.agent.skill_bundles import maybe_expand
                msg.content = maybe_expand(msg.content, workspace=self.workspace)
            except Exception as exc:  # noqa: BLE001
                logger.warning("slash skill/bundle expansion skipped: {}", exc)

        # Get or create session
        session = self.sessions.get_or_create(msg.session_key)
        display_content = str(msg.metadata.get("_display_content") or msg.content)

        # Sync per-session cwd between the in-memory pin (set by chat.send
        # cwd handlers on web/gateway channels) and persisted metadata, so
        # the pin survives bot restarts and channels that don't transport
        # cwd (CLI, Telegram, autonomous wake-ups).
        self._sync_session_cwd_metadata(msg.session_key, session.metadata)

        # Persist early so the conversation surfaces in clients that poll the
        # session store (direct-gateway inbox on desktop/iOS has no Firestore
        # listener — it lists sessions.* on a timer) AND so a mid-turn re-entry
        # sees the just-sent user message even when the agent goes straight to
        # tools (no streamed text yet). The user prompt is written as a PENDING
        # extra line — on disk, but not in session.messages — so it doesn't
        # double up in the history the loop sends to the LLM (the loop adds the
        # prompt itself). The canonical full-turn save at the end rewrites the
        # file with the real user+assistant messages. Best-effort.
        # Seed a PROVISIONAL title from the user's first message so a new chat
        # shows something meaningful immediately — gateways list sessions.* on a
        # timer and otherwise show a raw key suffix until the real auto-title
        # lands after the turn. Marked provisional so the post-turn auto-title
        # still overrides it. (Relay already seeds its own first-message title on
        # Firestore, so this is effectively the gateway's equivalent.)
        self._set_provisional_title(session, display_content)
        try:
            _pending_user: dict[str, Any] = {"role": "user", "content": display_content}
            if msg.media:
                _pending_user["media"] = list(msg.media)
            self.sessions.save(session, extra_messages=[_pending_user])
        except Exception as exc:  # noqa: BLE001
            logger.debug("early session save skipped: {}", exc)

        # Resolve the channel/chat coordinates tools should see. A cron
        # fire's InboundMessage arrives with channel="cron" (derived from
        # session_key="cron:{job.id}"), but that literal must NEVER leak
        # into downstream state — `channel="cron"` isn't deliverable by
        # ChannelManager, so any cron/message/voice job persisted with
        # it would silently drop delivery ("Unknown channel: cron").
        #
        # Gateway's on_cron_job passes the real origin (web/telegram/…)
        # via metadata so tools see the user-facing coordinates. For
        # non-cron turns this is a no-op.
        tool_channel = msg.channel
        tool_chat_id = msg.chat_id
        if msg.channel == "cron":
            tool_channel = (msg.metadata.get("origin_channel") or "").strip() or ""
            tool_chat_id = (msg.metadata.get("origin_chat_id") or "").strip() or msg.chat_id

        # Update tool contexts
        message_tool = self.tools.get("message")
        if isinstance(message_tool, MessageTool):
            message_tool.set_context(tool_channel, tool_chat_id)

        spawn_tool = self.tools.get("spawn")
        if isinstance(spawn_tool, SpawnTool):
            spawn_tool.set_context(tool_channel, tool_chat_id, is_subagent=False)

        builtin_agent_tool = self.tools.get("builtin_agent")
        if builtin_agent_tool and hasattr(builtin_agent_tool, "set_context"):
            builtin_agent_tool.set_context(tool_channel, tool_chat_id)

        cron_tool = self.tools.get("cron")
        if isinstance(cron_tool, CronTool):
            cron_tool.set_context(tool_channel, tool_chat_id)

        # Board tools — capture origin so a card dropped from Telegram knows
        # which channel/chat to report back to.
        for _board_name in ("board_add", "board_list", "board_get", "board_update", "board_run"):
            _bt = self.tools.get(_board_name)
            if _bt is not None and hasattr(_bt, "set_context"):
                _bt.set_context(tool_channel, tool_chat_id)

        # Flowlet tool — capture origin so a flowlet stamps the chat it was
        # created in; an `agent`-action button (e.g. "summarise my week") then
        # replies back into THAT chat instead of an orphan session.
        _flowlet_tool = self.tools.get("flowlet")
        if _flowlet_tool is not None and hasattr(_flowlet_tool, "set_context"):
            _flowlet_tool.set_context(tool_channel, tool_chat_id)

        # Set voice_call tool context for Telegram linking
        voice_tool = self.tools.get("voice_call")
        if voice_tool and hasattr(voice_tool, "set_context"):
            voice_tool.set_context(tool_channel, tool_chat_id)

        # Detect persona change and inject transition marker
        current_persona = self.context.persona
        session_persona = session.metadata.get("persona")
        if session_persona and session_persona != current_persona and session.messages:
            logger.info(f"Persona changed: {session_persona} → {current_persona}")
            session.add_message(
                "system",
                f"[PERSONA CHANGE] The assistant's persona has been changed from "
                f"'{session_persona}' to '{current_persona}'. From this point forward, "
                f"respond strictly as the new persona. Ignore the style/tone of previous "
                f"messages in this conversation."
            )
        session.metadata["persona"] = current_persona

        # Get history and check for compaction
        history = session.get_history(max_messages=self.context_messages)

        # Estimate total context: history + system prompt overhead.
        # Build actual system prompt to get accurate token count (avoids fixed 6K estimate drift).
        # Pass self.model AND msg.channel so the family-aware block
        # and the channel hint are both counted toward the estimate;
        # otherwise GPT/Gemini/Chinese + non-cli channels would
        # under-estimate by ~1-3K tokens and trip compaction late.
        try:
            sys_prompt = self.context.build_system_prompt(
                memory_search_enabled=self._memory_manager is not None,
                model=self.model,
                channel=msg.channel,
            )
            system_prompt_tokens = estimate_tokens(sys_prompt)
        except Exception:
            system_prompt_tokens = 6000  # fallback
        total_tokens = estimate_messages_tokens(history) + system_prompt_tokens

        if self.compaction.should_memory_flush(total_tokens):
            logger.info("Running pre-compaction memory flush")
            await self._run_memory_flush(session, msg.channel, msg.chat_id)
            self.compaction.mark_memory_flush_done()
            # Reload history after flush
            history = session.get_history(max_messages=self.context_messages)
            total_tokens = estimate_messages_tokens(history) + system_prompt_tokens

        # Microcompaction: truncate old tool results to delay full compaction
        history = self.compaction.microcompact(history)

        # Re-estimate after microcompaction
        total_tokens = estimate_messages_tokens(history) + system_prompt_tokens

        # Check if compaction is needed
        if self.compaction.should_compact(total_tokens):
            logger.info(f"Compacting context: {total_tokens} tokens exceeds threshold")
            try:
                result = await self.compaction.compact(history)
            except Exception as e:
                logger.error(f"Compaction failed: {e}")
                self.compaction.record_compaction_failure()
                # Fall through with uncompacted history — better than crashing
                result = None

            if result is None:
                # Compaction failed — trim to last 20 messages as emergency fallback
                system_msgs = [m for m in history if m.get("role") == "system"]
                non_system = [m for m in history if m.get("role") != "system"]
                history = system_msgs + non_system[-20:]
                logger.warning("Compaction failed — emergency trim to last 20 messages")
            else:
                self.compaction.record_compaction_success()
                logger.info(
                    f"Compaction complete: {result.tokens_before} -> {result.tokens_after} tokens, "
                    f"removed {result.messages_removed} messages, "
                    f"kept {len(result.kept_messages)} recent"
                )
                # Persist compaction: clear session, write summary + kept messages.
                # kept_messages may carry assistant_with_tool_calls /
                # tool_result entries the compactor decided to preserve
                # verbatim (recent turns the protect_last_n window
                # covers). We must persist ``tool_calls`` / ``tool_call_id``
                # / ``name`` alongside ``content``; otherwise the
                # post-compaction history ends with broken tool sequences
                # and the next chat call hits a provider 400.
                # Preserve the full pre-compaction history in the append-only
                # display transcript before trimming the LLM context jsonl.
                self.sessions.flush_full(session)
                session.clear()
                summary_msg = f"[Previous conversation summary]\n\n{result.summary}"
                session.add_message("system", summary_msg)
                for kept_msg in result.kept_messages:
                    extras = {
                        k: kept_msg[k]
                        for k in ("tool_calls", "tool_call_id", "name")
                        if k in kept_msg
                    }
                    session.add_message(
                        kept_msg.get("role", "user"),
                        kept_msg.get("content", ""),
                        **extras,
                    )
                self.sessions.mark_full_synced(session)
                session.metadata["last_compaction_summary"] = result.summary
                session.metadata["compaction_count"] = session.metadata.get("compaction_count", 0) + 1
                self.sessions.save(session)
                # Compaction is a snapshot boundary: drop the frozen memory block
                # so post-compaction turns re-inject freshly-written memory.
                self.context.invalidate_memory_snapshot(msg.session_key)
                history = [{"role": "system", "content": summary_msg}] + result.kept_messages

                # Send marker message so relay saves to Firestore (persistent separator)
                try:
                    await self.bus.publish_outbound(OutboundMessage(
                        channel=msg.channel, chat_id=msg.chat_id,
                        content="[context-optimized]",
                    ))
                except Exception:
                    pass

                # Notify connected clients about compaction (real-time event)
                if self._on_compaction:
                    try:
                        await self._on_compaction(
                            msg.session_key,
                            result.tokens_before,
                            result.tokens_after,
                            result.messages_removed,
                            "completed",
                        )
                    except Exception as e:
                        logger.debug(f"Compaction notification error: {e}")

        # Build initial messages. Cron runs pass skip_memory and
        # skip_context_files via metadata so MEMORY.md / AGENTS.md /
        # SOUL.md / USER.md don't leak into scheduled runs. Keeps
        # user's mental-model cues out of cron.
        skip_memory_flag = bool(msg.metadata.get("skip_memory", False))
        skip_context_files_flag = bool(msg.metadata.get("skip_context_files", False))
        voice_mode_flag = bool(msg.metadata.get("voice_mode", False))

        # Resolve effective model BEFORE building messages: cron
        # ``model_override`` lets a scheduled job target a different
        # model than the gateway default (e.g. a research job on
        # Gemini while chat runs on Claude), and the family-aware
        # guidance block must match the model the request actually
        # lands on, not the gateway default.
        effective_model = msg.metadata.get("model_override") or self.model

        messages = self.context.build_messages(
            history=history,
            current_message=msg.content,
            media=msg.media if msg.media else None,
            memory_search_enabled=self._memory_manager is not None,
            skip_memory=skip_memory_flag,
            skip_context_files=skip_context_files_flag,
            voice_mode=voice_mode_flag,
            model=effective_model,
            channel=msg.channel,
            session_key=msg.session_key,
        )
        self._inject_recent_artifacts_hint(
            messages, session_key=msg.session_key,
        )

        action_turn = self._is_action_turn(msg.channel, msg.content)
        if not action_turn and self._should_promote_retry_to_action(msg.content, history):
            action_turn = True
        if not action_turn and self._consume_pending_action_lock(session, msg.content):
            action_turn = True
            logger.info("Pending action lock promoted this turn to action_turn=True")
        live_call_turn = self._is_live_call_turn(msg.content)
        stream_callback = msg.metadata.get("stream_callback")
        on_iteration = msg.metadata.get("on_iteration")
        model_override = msg.metadata.get("model_override")
        disabled_tools = msg.metadata.get("disabled_tools")

        # Mark the boundary between prior history and this turn's
        # additions. ``_run_llm_tool_loop`` mutates ``messages`` in
        # place — appending every assistant_with_tool_calls and
        # tool_result the LLM produces. Snapshotting the length now
        # lets us persist exactly those new entries (and only those)
        # to the session after the loop returns. Prior history is
        # already in session.messages; we don't want to double-save.
        turn_start_idx = len(messages)

        # Files a tool produces for this reply (image_generate, screenshot) land
        # here and ride the OutboundMessage below — no separate ``message`` send.
        reply_media: list[str] = []
        final_content, tool_results, _executed_tools, usage, loop_messages = await self._run_llm_tool_loop(
            messages=messages,
            action_turn=action_turn,
            live_call_turn=live_call_turn,
            turn_content=msg.content,
            stream_callback=stream_callback,
            session_key=msg.session_key,
            model_override=model_override,
            disabled_tools=disabled_tools,
            outbound_channel=msg.channel,
            outbound_chat_id=msg.chat_id,
            outbound_run_id=msg.metadata.get("run_id") or "",
            on_iteration=on_iteration,
            reply_media=reply_media,
        )

        if action_turn:
            successful_tools = [r for r in tool_results if r.get("success")]
            if successful_tools:
                self._clear_pending_action_lock(session)
            else:
                self._set_pending_action_lock(session, display_content)
                logger.warning("Action turn ended without successful tool execution; pending lock armed.")

        # P4 — voice mode response sanitizer. The prompt already tells
        # the model not to emit markdown/emoji/URLs for TTS, but models
        # drift. Strip the obvious TTS-hostile patterns so the client
        # (iOS TTS via ElevenLabs) reads clean text. Runs ONLY when the
        # caller set voice_mode=True (iOS chat.send voiceMode:true);
        # text chat is untouched.
        if voice_mode_flag and final_content and not final_content.startswith("Error"):
            try:
                from flowly.agent.voice_postproc import sanitize_for_tts
                final_content = sanitize_for_tts(final_content)
            except Exception:
                logger.exception("[voice] sanitize_for_tts failed; returning raw text")

        # Save to session — full tool-protocol structure so the next
        # turn's LLM sees its prior tool calls + results, not just
        # the final summary text. See ``Session.extend_with_turn_messages``
        # for the full recipe (user + each loop message + capstone).
        # Slice the RETURNED list, not the input one: mid-turn transforms
        # rebind the loop's local list, so late appends (codex_session on
        # iteration 6+) never reach the input list.
        session.extend_with_turn_messages(
            user_content=display_content,
            new_messages=_drop_ephemeral_nudges(loop_messages[turn_start_idx:]),
            final_content=final_content,
            usage=usage,
            media=msg.media or None,
            reply_media=reply_media or None,
        )
        self.sessions.save(session)

        # Auto-title the session from the first exchange so every
        # client — CLI, desktop, iOS — shows the SAME descriptive name instead
        # of a random session-key suffix. Fire-and-forget; never blocks.
        self._maybe_autotitle_session(session, display_content, final_content)

        # Trajectory export (opt-in via config)
        if self._should_save_trajectories():
            from flowly.agent.trajectory import save_trajectory
            save_trajectory(
                messages=session.messages,
                model=self.model,
                completed=bool(final_content and not final_content.startswith("Error")),
                extra_metadata={
                    "session_key": msg.session_key,
                    "tools_used": _executed_tools,
                },
            )

        # Self-improvement: background review trigger
        self._maybe_spawn_review(session, _executed_tools, msg)

        # Coalesced MEMORY.md regen: ingests during this turn only marked the
        # summary dirty; rewrite it once here (not per-write) so a turn with N
        # memory writes busts the prompt cache at most once instead of N times.
        if self._memory_gov is not None:
            try:
                self._memory_gov.refresh_if_dirty()
            except Exception as exc:
                logger.warning(f"[memory-gov] end-of-turn refresh failed: {exc}")

        # Surface the tool-turn entries (assistant_with_tool_calls +
        # tool_result pairs the loop produced) to the channel layer so
        # the relay can persist each one to a separate ``tool_turns/``
        # Firestore subcollection. The web UI then renders them as
        # collapsible "Used tool X" cards interleaved with the chat
        # bubbles.
        #
        # Why we filter to only tool-related entries:
        #   * The closing plain-text assistant (no tool_calls) is the
        #     "final reply" the user actually sees — it goes through
        #     the existing single-doc ``messages/`` save path. No need
        #     to duplicate it into tool_turns/.
        #   * Tool-call assistants AND tool results carry the
        #     structured data (tool_calls list, tool_call_id linkage)
        #     that powers the collapsible UI. Those are what's new.
        #
        # Empty list ⇒ no tool work happened this turn ⇒ the metadata
        # field is omitted further down in the web channel so the
        # relay sees no ``toolMessages`` and uses its old path.
        # Backward compat is preserved on every layer.
        tool_messages_for_ui: list[dict[str, Any]] = []
        for m in loop_messages[turn_start_idx:]:
            if m.get("tool_calls") or m.get("role") == "tool":
                # Project to JSON-safe shape: content can be a list
                # of multimodal blocks; serialise via json.dumps once
                # the relay needs to forward. Here we just pass through
                # — the channel layer handles serialisation.
                tool_messages_for_ui.append({
                    k: v for k, v in m.items()
                    if k in ("role", "content", "tool_calls",
                             "tool_call_id", "name")
                })

        return OutboundMessage(
            channel=msg.channel,
            chat_id=msg.chat_id,
            content=final_content,
            media=reply_media,
            metadata={
                "tool_results": tool_results,
                "executed_tools": _executed_tools,
                "usage": usage,
                # Effective model for this turn — ``model_override``
                # wins when set (per-session model switches), else the
                # agent's default. The channel layer forwards this to
                # the UI so the context-window indicator can look up
                # the right model's context_length.
                "model": model_override or self.model,
                # Structured tool-turn payload for the relay's
                # tool_turns/ subcollection. Always set (may be empty);
                # the channel layer drops the field from the WS payload
                # when empty so old relays / old desktops see exactly
                # the same wire format they always have.
                "tool_messages": tool_messages_for_ui,
                # ``aborted: true`` lets the channel layer + relay +
                # client UI know this turn was stopped mid-flight by
                # the user. The accumulated text in ``final_content``
                # is still the bot's authoritative output up to the
                # Stop point (partial preservation on abort);
                # the UI is expected to render it with an [Aborted]
                # marker rather than treating it as a normal reply.
                # Read the flag at the moment of emit so a Stop
                # pressed after ``_run_llm_tool_loop`` returned but
                # before we reached this builder is still respected.
                "aborted": bool(
                    msg.metadata.get("run_id")
                    and self.is_run_aborted(msg.metadata["run_id"])
                ),
            },
        )

    async def _process_system_message(self, msg: InboundMessage) -> OutboundMessage | None:
        """
        Process a system message (e.g., subagent announce).
        
        The chat_id field contains "original_channel:original_chat_id" to route
        the response back to the correct destination.
        """
        logger.info(f"Processing system message from {msg.sender_id}")
        
        # Parse origin from chat_id (format: "channel:chat_id")
        if ":" in msg.chat_id:
            parts = msg.chat_id.split(":", 1)
            origin_channel = parts[0]
            origin_chat_id = parts[1]
        else:
            # Fallback
            origin_channel = "cli"
            origin_chat_id = msg.chat_id
        
        # Use the origin session for context
        session_key = f"{origin_channel}:{origin_chat_id}"
        session = self.sessions.get_or_create(session_key)
        
        # Update tool contexts
        message_tool = self.tools.get("message")
        if isinstance(message_tool, MessageTool):
            message_tool.set_context(origin_channel, origin_chat_id)

        spawn_tool = self.tools.get("spawn")
        if isinstance(spawn_tool, SpawnTool):
            spawn_tool.set_context(origin_channel, origin_chat_id)

        builtin_agent_tool = self.tools.get("builtin_agent")
        if builtin_agent_tool and hasattr(builtin_agent_tool, "set_context"):
            builtin_agent_tool.set_context(origin_channel, origin_chat_id)

        cron_tool = self.tools.get("cron")
        if isinstance(cron_tool, CronTool):
            cron_tool.set_context(origin_channel, origin_chat_id)

        # Set voice_call tool context for Telegram linking
        voice_tool = self.tools.get("voice_call")
        if voice_tool and hasattr(voice_tool, "set_context"):
            voice_tool.set_context(origin_channel, origin_chat_id)

        # Build messages with the announce content. This path is the
        # subagent-completion-announce turn — the parent wakes back up
        # to deliver the specialist's result, so it runs on the parent
        # agent's primary model (self.model), not on any per-cron
        # override (subagent runs are out-of-band by then). The
        # announce turn is delivered on the parent's origin channel,
        # so the channel hint must match origin_channel rather than
        # the announce message's transport channel.
        messages = self.context.build_messages(
            history=session.get_history(max_messages=self.context_messages),
            current_message=msg.content,
            memory_search_enabled=self._memory_manager is not None,
            model=self.model,
            channel=origin_channel,
        )
        self._inject_recent_artifacts_hint(
            messages, session_key=f"{origin_channel}:{origin_chat_id}",
        )

        # A system wake-up (subagent announce, board / exec / delegate
        # notification) delivers a RESULT for the parent to synthesize and hand
        # to the user — it is NOT a user action request. Classifying it from the
        # announce CONTENT (the subagent's own report, which routinely contains
        # words like "gönder" / "bildir" / "send") wrongly flips this into
        # action-enforce mode: the parent is then forced to call a tool AFTER
        # the real work is already done, and when it has nothing valid left to
        # call the loop emits the misleading "Tool calls failed, no action was
        # taken." (Observed with multi-subagent research fan-out: each
        # completion woke the parent into a forced-tool turn that failed.)
        #
        # A delivery turn must never enforce tools — the model summarizes (and
        # MAY optionally call message / artifact) without being forced. This
        # mirrors how an async-delegation completion re-enters as a plain,
        # non-enforced message turn rather than an action request.
        action_turn = False
        live_call_turn = self._is_live_call_turn(msg.content)
        # Snapshot the loop-input boundary — same rationale as the
        # main path. Everything appended past this index is what the
        # turn produced and what we need to persist.
        turn_start_idx = len(messages)
        reply_media: list[str] = []
        final_content, tool_results, _executed_tools, system_usage, loop_messages = await self._run_llm_tool_loop(
            messages=messages,
            action_turn=action_turn,
            live_call_turn=live_call_turn,
            turn_content=msg.content,
            session_key=f"{origin_channel}:{origin_chat_id}",
            outbound_channel=origin_channel,
            outbound_chat_id=origin_chat_id,
            outbound_run_id=msg.metadata.get("run_id") or "",
            reply_media=reply_media,
        )

        # No pending-action-lock bookkeeping here: a system/announce turn is a
        # delivery turn (action_turn is always False above), so it neither sets
        # nor clears the lock — that lifecycle belongs to real user action turns.

        # Save to session (mark as system message in history). Full
        # tool-protocol structure is preserved by
        # ``extend_with_turn_messages`` so a subagent's tool work is
        # visible to the parent agent on the next turn.
        session.extend_with_turn_messages(
            user_content=f"[System: {msg.sender_id}] {msg.content}",
            new_messages=_drop_ephemeral_nudges(loop_messages[turn_start_idx:]),
            final_content=final_content,
            usage=system_usage,
            reply_media=reply_media or None,
            # System triggers (subagent/board/memory announces) drive this turn
            # and stay in the LLM context, but must never render in the chat as a
            # user message — only the assistant's summary is user-facing.
            user_display_hidden=True,
        )
        self.sessions.save(session)

        # Local clients (TUI / desktop) have no channel adapter, so a
        # system-triggered reply (board result, subagent announce, …) would
        # be dropped by the outbound dispatcher — those surfaces normally
        # only get request-scoped chat replies. Push it over the gateway WS
        # so the reply actually shows up. Request-scoped replies don't come
        # through here, so there's no double delivery; remote channels
        # (Telegram/…) are delivered by their adapter via the return below.
        if final_content and origin_channel in ("cli", "tui", "desktop"):
            gw = getattr(self, "_gateway_server", None)
            if gw is not None and hasattr(gw, "push_session_message"):
                try:
                    await gw.push_session_message(
                        f"{origin_channel}:{origin_chat_id}", final_content
                    )
                except Exception as exc:
                    logger.warning(f"[board] local push failed: {exc}")

        return OutboundMessage(
            channel=origin_channel,
            chat_id=origin_chat_id,
            content=final_content,
            media=reply_media,
        )

    # ─── Activity tracker (inactivity-based timeout support) ────────────
    def _touch_activity(self, desc: str, tool: str | None = None) -> None:
        """Mark the agent as actively working.

        Called at blocking boundaries (API stream chunk received, tool
        call start/end, retry backoff pulse). The cron inactivity
        poller reads ``get_activity_summary()['seconds_since_activity']``
        — as long as _some_ progress keeps touching this, long-running
        jobs aren't killed by a wall-clock timer.
        """
        import time as _time
        self._last_activity_ts = _time.time()
        self._last_activity_desc = desc
        if tool is not None:
            self._current_tool = tool

    def get_activity_summary(self) -> dict[str, Any]:
        """Return a diagnostic snapshot of the agent's current activity.

        ``seconds_since_activity`` is recomputed fresh on every call
        from wall-clock time. Used by the gateway to detect stuck
        agent loops.
        """
        import time as _time
        elapsed = _time.time() - self._last_activity_ts
        return {
            "last_activity_ts": self._last_activity_ts,
            "last_activity_desc": self._last_activity_desc,
            "seconds_since_activity": round(elapsed, 1),
            "current_tool": self._current_tool,
            "api_call_count": self._api_call_count,
            "max_iterations": self.max_iterations,
        }

    def _inject_recent_artifacts_hint(
        self,
        messages: list[dict[str, Any]],
        session_key: str,
    ) -> None:
        """Prepend an ephemeral system note listing recent subagent
        artifacts for this session, if any exist.

        Conditional on the tracker actually having entries — when no
        subagent has produced an artifact in this session, nothing is
        injected. That's the entire point: the LLM only learns about
        artifacts that were really created, so proactive offers like
        'Tam raporu göstereyim mi?' never promise a file that doesn't
        exist (the risk with a blanket prompt rule).

        The hint lands AFTER the cached system prompt — it's a fresh
        ephemeral message each turn, so prompt-cache hits on the big
        static prompt still work. ~100-200 tokens per turn when there
        ARE artifacts; zero cost otherwise.
        """
        try:
            recent = self.subagents.recent_completed_dispatches(
                parent_session_key=session_key,
                max_age_seconds=600.0,  # 10 min — matches the guard window
            )
        except Exception:
            # Tracker unavailable — fail silent. Hint is best-effort.
            return
        if not recent:
            return
        # Cap to top 5 most recent. More than that just bloats tokens
        # without helping — the user rarely cares about a research from
        # 9 minutes ago when they're talking about something 1 minute old.
        recent = recent[:5]
        lines = [
            "[Session context — persisted artifacts produced earlier in this session]",
        ]
        for r in recent:
            import time as _time
            mins_ago = max(0, int((_time.time() - r["completed_at"]) / 60))
            when = f"{mins_ago} min ago" if mins_ago > 0 else "just now"
            lines.append(
                f"- {r['artifact_id']} — \"{r['title'][:80]}\" "
                f"(from {r['label']}, completed {when})"
            )
        lines.append(
            "\nIf the user asks to view, save, reference, or follow up "
            "on one of these — in any language — use "
            "artifact(action='get', artifact_id=..., offset=0, limit=6000) "
            "to read it, or just reference the artifact_id in your reply "
            "(the client renders it as a preview card). Do NOT re-dispatch "
            "the specialist and do NOT invent artifact IDs outside this "
            "list. When delivering a summary derived from one of these, "
            "you MAY proactively offer the full artifact (in the user's "
            "language, one short sentence) — but only because the "
            "artifact above actually exists."
        )
        hint = "\n".join(lines)
        # Insert AFTER the cached system prompt (messages[0]) so the
        # big prompt can still hit the cache. Skip if messages is
        # unexpectedly empty or first message is not system.
        if messages and messages[0].get("role") == "system":
            messages.insert(1, {"role": "system", "content": hint})
        else:
            messages.insert(0, {"role": "system", "content": hint})

    def interrupt(self, reason: str = "interrupted", session_key: str | None = None) -> None:
        """Request a cooperative interrupt.

        Tool and stream loops check ``_interrupt_requested`` at their
        next blocking boundary and exit cleanly. Not a forceful kill —
        any in-flight HTTP call or tool subprocess completes first.

        If ``session_key`` is provided, the cancellation cascades to any
        background subagents spawned by that session (so the user doesn't
        keep paying for subagent tokens on a turn they've already
        abandoned). Other sessions' subagents are left alone.
        """
        logger.info(
            f"AgentLoop.interrupt({reason!r}) requested"
            + (f" session={session_key}" if session_key else "")
        )
        self._interrupt_requested = True
        self._interrupt_reason = reason
        if session_key:
            try:
                cancelled = self.subagents.cancel_children(session_key)
                if cancelled:
                    logger.info(f"AgentLoop.interrupt: cascaded to {cancelled} subagent(s)")
            except Exception:
                logger.exception("AgentLoop.interrupt: subagent cascade failed")

    def clear_interrupt(self) -> None:
        """Reset interrupt state (call before starting a fresh turn)."""
        self._interrupt_requested = False
        self._interrupt_reason = None

    async def process_direct(
        self,
        content: str,
        session_key: str = "cli:direct",
        stream_callback: Callable[[str], Awaitable[None]] | None = None,
        media: list[str] | None = None,
        return_metadata: bool = False,
        model_override: str | None = None,
        disabled_tools: list[str] | None = None,
        skip_memory: bool = False,
        skip_context_files: bool = False,
        origin_channel: str | None = None,
        origin_chat_id: str | None = None,
        voice_mode: bool = False,
        on_iteration: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> str | tuple[str, dict[str, Any]]:
        """
        Process a message directly (for CLI, voice calls, or desktop WebSocket).

        Args:
            content: The message content.
            session_key: Session identifier in format "channel:chat_id".
            stream_callback: Optional async callback invoked with each text delta
                             for real-time streaming to the client.
            return_metadata: If True, return (content, metadata) tuple instead of just content.
            model_override: Use this model for the LLM turn instead of the agent's
                            default. Intended for cron jobs with a per-job model
                            pinned at creation time. Scoped to this call; does
                            not leak to other in-flight requests.

        Returns:
            The agent's response string, or (response, metadata) if return_metadata=True.
        """
        # Parse session_key to extract channel and chat_id
        if ":" in session_key:
            channel, chat_id = session_key.split(":", 1)
        else:
            channel, chat_id = "cli", session_key

        metadata: dict[str, Any] = {}
        if stream_callback is not None:
            metadata["stream_callback"] = stream_callback
        if on_iteration is not None:
            # Live per-iteration tool-turn events delivered straight to the
            # caller's transport (the direct gateway → iteration_step WS event).
            metadata["on_iteration"] = on_iteration
        if model_override:
            metadata["model_override"] = model_override
        if disabled_tools:
            metadata["disabled_tools"] = list(disabled_tools)
        if skip_memory:
            metadata["skip_memory"] = True
        if skip_context_files:
            metadata["skip_context_files"] = True
        if voice_mode:
            # P4 — iOS voice session sends voiceMode=true on chat.send.
            # Triggers VOICE_MODE_BLOCK in the system prompt + runs
            # sanitize_for_tts on the final response. Default False
            # preserves every text/chat caller's behaviour.
            metadata["voice_mode"] = True
        # Real (user-facing) delivery coordinates. When session_key is
        # "cron:{job_id}" the derived channel/chat_id aren't deliverable —
        # tools that capture context (spawn, cron, builtin_agent, message,
        # voice_call) must use these instead so they never persist the
        # literal "cron" channel into jobs.json / announcements.
        if origin_channel:
            metadata["origin_channel"] = origin_channel
        if origin_chat_id:
            metadata["origin_chat_id"] = origin_chat_id

        msg = InboundMessage(
            channel=channel,
            sender_id="user",
            chat_id=chat_id,
            content=content,
            media=media or [],
            metadata=metadata,
        )

        response = await self._process_message(msg)
        text = response.content if response else ""
        if return_metadata:
            meta = dict(response.metadata) if response and response.metadata else {}
            # Surface reply media (image_generate / screenshot) so the direct
            # gateway WS path can attach it to the final event — same as the
            # channel path does via OutboundMessage.media. Without this, media
            # generated over a remote gateway (iOS/desktop direct WS) is lost.
            if response and response.media:
                meta["media"] = list(response.media)
            return text, meta
        return text

    # ------------------------------------------------------------------ #
    # Slash command formatters
    # ------------------------------------------------------------------ #

    def _format_skills_list(self, filter_arg: str = "") -> str:
        """Render the active workspace's skill catalogue as Markdown.

        Empty filter returns the full list grouped by source
        (workspace / managed / built-in). A non-empty filter narrows
        by case-insensitive substring against either the skill name
        or its frontmatter description.
        """
        from flowly.agent.skills import SkillsLoader

        loader = SkillsLoader(workspace=self.workspace)
        skills = loader.list_skills(filter_unavailable=False)
        if not skills:
            return "No skills found."

        needle = (filter_arg or "").strip().lower()

        # Build rows up front so the filter can match against
        # description, not just the slug.
        rows: list[tuple[str, str, str]] = []  # (source, name, description)
        for skill in skills:
            name = skill["name"]
            description = loader._get_skill_description(name) or ""
            if needle and needle not in name.lower() and needle not in description.lower():
                continue
            rows.append((skill["source"], name, description))

        if not rows:
            return f"No skills match `{filter_arg}`."

        # Group by source, preserving the loader's priority order
        # (workspace > managed > builtin). Display order matches.
        groups: dict[str, list[tuple[str, str]]] = {
            "workspace": [], "managed": [], "builtin": [],
        }
        for source, name, description in rows:
            groups.setdefault(source, []).append((name, description))

        # CommonMark needs blank lines between paragraphs and *before*
        # a list block — without them every line collapses into one
        # paragraph and renders inline. Use real ``- `` bullets (not
        # the bullet glyph) so the markdown parser actually emits a
        # ``<ul>`` instead of a long run of text.
        lines: list[str] = [f"### 📚 Skills ({len(rows)})"]
        if filter_arg:
            lines.append("")
            lines.append(f"_Filtered by `{filter_arg}`_")
        for label, source_key in (
            ("Workspace", "workspace"),
            ("Managed", "managed"),
            ("Built-in", "builtin"),
        ):
            items = groups.get(source_key) or []
            if not items:
                continue
            lines.append("")
            lines.append(f"**{label}**")
            lines.append("")
            for name, description in items:
                if description:
                    # Trim to keep the bot's reply readable in Telegram /
                    # Discord where long single messages get truncated.
                    short = description.strip().split("\n", 1)[0][:140]
                    lines.append(f"- `/{name}` — {short}")
                else:
                    lines.append(f"- `/{name}`")
        lines.append("")
        lines.append("_Type `/<skill-name>` to load it for one turn._")
        return "\n".join(lines)

    def _format_whoami(self, msg: InboundMessage) -> str:
        """Render the caller's identity / context as Markdown."""
        sender = msg.sender_id or "(unknown)"
        chat = msg.chat_id or "(unknown)"
        session_key = msg.session_key or "(none)"
        session = None
        try:
            if msg.session_key:
                session = self.sessions.get_or_create(msg.session_key)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"whoami: session lookup failed: {exc}")

        # Resolve the active model the same way the chat loop does so
        # the answer reflects what the next turn will actually use.
        active_model = ""
        try:
            active_model = (
                getattr(self, "_active_model", None)
                or getattr(self.provider, "model", "")
                or ""
            )
        except Exception:  # noqa: BLE001
            pass

        # Header + blank line before the list so CommonMark renders
        # the ``- `` rows as a real bulleted list. Without the blank
        # line every bullet collapses into one paragraph.
        lines = [
            "### 👤 Identity",
            "",
            f"- **Channel:** `{msg.channel}`",
            f"- **Sender:** `{sender}`",
            f"- **Chat:** `{chat}`",
            f"- **Session key:** `{session_key}`",
        ]
        if active_model:
            lines.append(f"- **Active model:** `{active_model}`")
        if session is not None:
            lines.append(f"- **Persona:** `{session.metadata.get('persona', 'default')}`")
            msg_count = len(session.messages)
            lines.append(f"- **Messages in session:** {msg_count}")
        return "\n".join(lines)

    def _format_status(self, msg: InboundMessage) -> str:
        """Render a session health snapshot — message count, tokens, model."""
        session = None
        try:
            if msg.session_key:
                session = self.sessions.get_or_create(msg.session_key)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"status: session lookup failed: {exc}")

        # ``### `` header + blank line before the list keeps the
        # CommonMark parser happy and forces each bullet onto its own
        # row in the renderer. ``- `` (not ``• ``) so the parser
        # treats the rows as a real ``<ul>``.
        lines: list[str] = ["### 📊 Session status", ""]
        if session is None:
            lines.append("_No session yet._")
            return "\n".join(lines)

        meta = session.metadata or {}
        msg_count = len(session.messages)
        lines.append(f"- **Conversation:** `{session.key}`")
        lines.append(f"- **Messages:** {msg_count}")

        # Token + model context. These two fields live on conversation
        # metadata (written by ``_run_llm_tool_loop`` after each turn),
        # so they exist for any session that has completed at least
        # one round-trip.
        last_model = meta.get("last_model") or meta.get("model")
        if last_model:
            lines.append(f"- **Last model:** `{last_model}`")
        last_turn_tokens = meta.get("last_turn_tokens")
        if isinstance(last_turn_tokens, int) and last_turn_tokens > 0:
            lines.append(f"- **Last turn tokens:** {last_turn_tokens:,}")
        prompt_tokens = meta.get("last_prompt_tokens")
        completion_tokens = meta.get("last_completion_tokens")
        if isinstance(prompt_tokens, int) and isinstance(completion_tokens, int):
            lines.append(
                f"- **Breakdown:** {prompt_tokens:,} prompt · {completion_tokens:,} completion"
            )
        persona = meta.get("persona")
        if persona:
            lines.append(f"- **Persona:** `{persona}`")

        # Subagent activity, if the registry is wired into this loop.
        try:
            registry = getattr(self, "_subagent_registry", None) or getattr(self, "subagent_registry", None)
            if registry is not None and hasattr(registry, "all"):
                records = list(registry.all() or [])
                running = sum(1 for r in records if getattr(r, "ended_at", None) is None)
                completed = sum(
                    1 for r in records
                    if getattr(r, "outcome", None) == "ok"
                )
                if running or completed:
                    lines.append(f"- **Subagents:** {running} running · {completed} completed")
        except Exception:  # noqa: BLE001
            pass

        return "\n".join(lines)

    async def compact_session(
        self,
        session_key: str,
        custom_instructions: str | None = None,
    ) -> dict[str, Any]:
        """
        Manually compact a session's history.

        Args:
            session_key: Session identifier.
            custom_instructions: Optional instructions for summarization.

        Returns:
            Dict with compaction results.
        """
        session = self.sessions.get_or_create(session_key)
        history = session.get_history(max_messages=self.context_messages)

        if not history:
            return {
                "success": False,
                "message": "No history to compact.",
                "tokens_before": 0,
                "tokens_after": 0,
            }

        tokens_before = estimate_messages_tokens(history)

        # Check if already compacted (first message is a compaction summary)
        is_already_compacted = (
            len(history) == 1
            and history[0].get("role") == "system"
            and "[Compacted conversation summary]" in history[0].get("content", "")
        )

        if is_already_compacted:
            return {
                "success": False,
                "message": "Already compacted. Send more messages first.",
                "tokens_before": tokens_before,
                "tokens_after": tokens_before,
            }

        # Check if too few messages to compact (need at least 3 messages)
        # Filter out system messages for this count
        user_assistant_messages = [m for m in history if m.get("role") in ("user", "assistant")]
        if len(user_assistant_messages) < 3:
            return {
                "success": False,
                "message": f"Not enough messages to compact ({len(user_assistant_messages)} messages). Need at least 3.",
                "tokens_before": tokens_before,
                "tokens_after": tokens_before,
            }

        # Check if token count is too low to bother compacting (< 1000 tokens)
        if tokens_before < 1000:
            return {
                "success": False,
                "message": f"History too small to compact ({tokens_before} tokens). Need at least 1000.",
                "tokens_before": tokens_before,
                "tokens_after": tokens_before,
            }

        # Notify clients that compaction is starting
        if self._on_compaction:
            try:
                await self._on_compaction(
                    session_key, tokens_before, 0, 0, "started",
                )
            except Exception:
                pass

        # Run compaction
        result = await self.compaction.compact(
            history,
            custom_instructions=custom_instructions,
        )

        # Clear session and add summary + kept recent messages.
        # Same tool-field preservation as the auto-compaction path:
        # kept_messages from the protect_last_n window may include
        # tool-call assistants + tool results that must keep their
        # protocol fields (``tool_calls`` / ``tool_call_id`` / ``name``)
        # or the next chat call rejects the malformed sequence.
        # Preserve the FULL pre-compaction history in the append-only display
        # transcript before we trim the context jsonl, so the chat UI keeps every
        # early message (compaction only shrinks the LLM working context).
        self.sessions.flush_full(session)
        session.clear()
        session.add_message(
            "system",
            f"[Compacted conversation summary]\n\n{result.summary}"
        )
        for kept_msg in result.kept_messages:
            extras = {
                k: kept_msg[k]
                for k in ("tool_calls", "tool_call_id", "name")
                if k in kept_msg
            }
            session.add_message(
                kept_msg.get("role", "user"),
                kept_msg.get("content", ""),
                **extras,
            )
        # The summary + kept turns are already in (or excluded from) the display
        # log; declare them synced so save() doesn't mirror the summary.
        self.sessions.mark_full_synced(session)
        session.metadata["last_compaction_summary"] = result.summary
        session.metadata["compaction_count"] = session.metadata.get("compaction_count", 0) + 1
        self.sessions.save(session)

        # Notify connected clients — compaction completed
        if self._on_compaction:
            try:
                await self._on_compaction(
                    session_key,
                    result.tokens_before,
                    result.tokens_after,
                    result.messages_removed,
                    "completed",
                )
            except Exception:
                pass

        return {
            "success": True,
            "message": f"Compacted {result.messages_removed} messages",
            "tokens_before": result.tokens_before,
            "tokens_after": result.tokens_after,
            "summary_preview": result.summary[:200] + "..." if len(result.summary) > 200 else result.summary,
        }
