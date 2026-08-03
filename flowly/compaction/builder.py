"""Translate the on-disk compaction settings into the runtime config.

Kept in one place because the gateway and the one-shot CLI both build an
AgentLoop, and when they built this config separately they drifted — the
runtime dataclass and the Pydantic schema carried different defaults, so
which one applied depended on which entry point you started.
"""

from typing import Any

from flowly.compaction.types import (
    CompactionConfig,
    KeepRecentConfig,
    MemoryFlushConfig,
    MicrocompactConfig,
)


def build_compaction_config(settings: Any) -> CompactionConfig:
    """Build the runtime :class:`CompactionConfig` from user settings.

    ``settings`` is the Pydantic ``agents.defaults.compaction`` section.
    Fields the user actually wrote in config.json are marked explicit so
    auto-detection can defer to them.
    """
    explicit = getattr(settings, "model_fields_set", set())

    flush = settings.memory_flush
    micro = getattr(settings, "microcompact", None)
    keep = getattr(settings, "keep_recent", None)

    return CompactionConfig(
        mode=settings.mode,
        reserve_tokens_floor=settings.reserve_tokens_floor,
        max_history_share=settings.max_history_share,
        context_window=settings.context_window,
        context_window_explicit="context_window" in explicit,
        memory_flush=MemoryFlushConfig(
            enabled=flush.enabled,
            soft_threshold_tokens=flush.soft_threshold_tokens,
            prompt=flush.prompt,
            system_prompt=flush.system_prompt,
        ),
        microcompact=(
            MicrocompactConfig(
                enabled=micro.enabled,
                keep_recent_full=micro.keep_recent_full,
                truncate_chars=micro.truncate_chars,
            )
            if micro is not None
            else MicrocompactConfig()
        ),
        keep_recent=(
            KeepRecentConfig(
                enabled=keep.enabled,
                min_tokens=keep.min_tokens,
                min_messages=keep.min_messages,
                max_tokens=keep.max_tokens,
            )
            if keep is not None
            else KeepRecentConfig()
        ),
    )
