"""Compaction system for context management."""

from flowly.compaction.estimator import estimate_tokens, estimate_messages_tokens
from flowly.compaction.summarizer import (
    summarize_messages,
    summarize_in_stages,
    summarize_with_fallback,
)
from flowly.compaction.pruning import (
    prune_history_for_context_share,
    split_messages_by_token_share,
    chunk_messages_by_max_tokens,
)
from flowly.compaction.service import CompactionService
from flowly.compaction.types import (
    CompactionConfig,
    CompactionResult,
    MemoryFlushConfig,
)

__all__ = [
    # Estimator
    "estimate_tokens",
    "estimate_messages_tokens",
    # Summarizer
    "summarize_messages",
    "summarize_in_stages",
    "summarize_with_fallback",
    # Pruning
    "prune_history_for_context_share",
    "split_messages_by_token_share",
    "chunk_messages_by_max_tokens",
    # Service
    "CompactionService",
    # Types
    "CompactionConfig",
    "CompactionResult",
    "MemoryFlushConfig",
]
